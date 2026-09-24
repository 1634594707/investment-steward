"""跨日披露事实标注器（youzi-radar 二期 Y2-01..06，纯函数，可离线测试）。

输入只有三类（方案 §六 架构图）：

1. 已标准化的席位披露记录（含身份分类与统计周期分类）；
2. 显式给出的交易日序列（**不查日历、不读系统时钟**）；
3. 逐日覆盖状态（complete/not_published/fetch_failed/truncated/unknown）。

本模块不联网、不读系统时钟、不读取任何后续价格；相同输入永远得到相同输出。
只输出方案 §四 的三类可验证披露事实，不推断持仓、清仓、收益；输出结构中
没有任何绩效字段（无涨跌、胜率、平均收益）。

契约备注：标准化记录的字段集在 Y0 契约冻结（路线图 Y0-11）前为拟定版；
本模块只依赖下列显式字段，扩展字段不影响算法。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import date

#: 统计周期分类（Y0-03 冻结前为拟定枚举；多日累计行不参与三类跨日指纹）。
PERIOD_SINGLE_DAY = "single_day"
PERIOD_MULTI_DAY = "multi_day"
PERIOD_UNKNOWN = "unknown"

#: 席位身份分类：只有可靠具体营业部才可跨日匹配。
IDENTITY_BRANCH = "branch"
IDENTITY_BUCKET = "bucket"
IDENTITY_UNKNOWN = "unknown"

#: 逐日覆盖状态（方案 §5.3；unknown 表示无法区分「未披露」与「无记录」）。
COVERAGE_COMPLETE = "complete"
COVERAGE_NOT_PUBLISHED = "not_published"
COVERAGE_FETCH_FAILED = "fetch_failed"
COVERAGE_TRUNCATED = "truncated"
COVERAGE_UNKNOWN = "unknown"
KNOWN_COVERAGES = frozenset(
    {
        COVERAGE_COMPLETE,
        COVERAGE_NOT_PUBLISHED,
        COVERAGE_FETCH_FAILED,
        COVERAGE_TRUNCATED,
        COVERAGE_UNKNOWN,
    }
)

#: 三类事实类型（方案 §四；不使用 overnight_exit 等已废弃命名）。
FACT_ADJACENT_BUY_SELL = "adjacent_buy_sell"
FACT_CONSECUTIVE_BUY = "consecutive_buy"
FACT_REPEATED_PRESENCE = "repeated_seat_presence"

#: 固定边界文案（路线图 §2 铁律 3）：所有展示方必须原样携带。
BOUNDARY_NOTICE = (
    "榜外席位不可见，未检出未发生；同一营业部不等于同一账户，无法据此确认持仓、清仓或同一笔交易。"
)


class AnnotatorInputError(ValueError):
    """输入结构不合法（日期格式、未知覆盖状态、方向非法等）。"""


@dataclass(frozen=True)
class NormalizedSeatRecord:
    """已标准化的单条席位披露（由取数层负责身份/周期/金额分类后再传入）。"""

    trading_day: str  # YYYYMMDD
    security_code: str
    operatedept_code: str
    operatedept_name: str
    direction: str  # "buy" | "sell"
    amount: float | None  # 方向对应金额：买榜取 BUY，卖榜取 SELL（元）
    period: str  # PERIOD_*
    identity_kind: str  # IDENTITY_*
    explanation: str  # 上榜原因原文
    source_report: str  # 来源报告名，如 RPT_BILLBOARD_DAILYDETAILSBUY
    source_record_id: str = ""  # 已核验的来源记录 ID；为空时按字段规范派生


@dataclass(frozen=True)
class FactTag:
    """一条跨日事实标注（可回链参与证据；不含量价后果）。"""

    fact_type: str
    security_code: str
    operatedept_code: str
    operatedept_name: str
    days: tuple[str, ...]
    #: 逐日证据：{"trading_day", "direction", "amount", "evidence_id", "explanation"}
    evidence: tuple[dict[str, object], ...]
    fact_id: str


@dataclass(frozen=True)
class AnnotationResult:
    """标注结果：窗口完整性、三类事实、金额冲突与排除摘要。"""

    security_code: str
    window_days: tuple[str, ...]
    coverage: tuple[tuple[str, str], ...]
    window_complete: bool
    incomplete_reason: str  # 完整时为空串
    facts: tuple[FactTag, ...]
    conflicts: tuple[dict[str, object], ...]
    excluded: tuple[tuple[str, int], ...]


#: 单日口径的显式措辞标记。只有这些**具体**说法才判单日——不能用「含『日』」这种
#: 宽泛规则（中文里几乎每句都含「日」，会把「最近三个交易日」误判成单日）。
_SINGLE_DAY_MARKERS = (
    "日涨幅",
    "日跌幅",
    "日换手",
    "日振幅",
    "日收盘价",
    "日价格",
    "当日换手",
    "当日收盘价",
)
#: 多日累计的显式措辞标记。
_MULTI_DAY_MARKERS = ("连续", "累计")
#: 「N 个交易日」这类跨日表述（含中文数字），没有「连续/累计」也必须判多日。
_MULTI_DAY_SPAN = re.compile(r"[一二三四五六七八九十两\d]+\s*个?\s*交易日内")


def classify_period_from_explanation(explanation: str) -> str:
    """按原因原文判定统计周期（Y0-03 冻结；2026-09-16 真实样本核验）。

    冻结依据：`RPT_BILLBOARD_DAILYDETAILSBUY/SELL` 在 2026-09-16 上共出现 **20** 种
    不同原因文本（买 365 行 / 卖 365 行，两侧原因集合完全相同）。按交易所披露口径，
    其中 **6 种**为多日累计（「连续N个交易日内…累计…」），**14 种**为单日
    （「日…」「当日…」或「非上市首日,当日…」）。逐条预期分类见
    `docs/游资手法学习数据契约与探针记录-2026-09-17.zh-CN.md` 附表与
    `tests/test_tactic_annotator.py::test_frozen_gold_sample_classification`。

    判定顺序（保守，命中即返回，绝不猜测）：

    1. 空文本 → `unknown`。
    2. 出现多日标记（「连续」/「累计」）或「N 个交易日内」跨度表述 → `multi_day`。
       多日累计行**不拆成日流水**，也不参与连续买榜（路线图 §2 铁律 2）。
    3. 出现显式单日标记（日涨幅/日跌幅/日换手/日振幅/日收盘价/日价格/当日…）
       → `single_day`。
    4. 其余 → `unknown`（宁缺勿假；未知措辞不冒充已核验单日）。

    注意：判定 2 优先于 3，因为「连续三个交易日内，涨幅偏离值累计达到20%」同时含
    「交易日内」与「涨幅」，必须判多日。
    """
    text = (explanation or "").strip()
    if not text:
        return PERIOD_UNKNOWN
    if any(marker in text for marker in _MULTI_DAY_MARKERS) or _MULTI_DAY_SPAN.search(text):
        return PERIOD_MULTI_DAY
    if any(marker in text for marker in _SINGLE_DAY_MARKERS):
        return PERIOD_SINGLE_DAY
    return PERIOD_UNKNOWN


def evidence_id_of(record: NormalizedSeatRecord) -> str:
    """稳定证据 ID：来源报告+证券+日期+周期+原因+席位+方向+金额（方案 §5.1）。

    实测（Y0-04，2026-09-16 夹具，按实际 BUY/SELL 字段）：合并四元组 590 个、
    重复 98 组、方向金额冲突 95 组、含 ≥2 种 EXPLANATION 63 组；
    `TRADE_ID` 730 行仅 73 个唯一值——都不能单独当主键。
    因此 ID 由全部规范化字段派生；来源记录 ID 可用时优先拼接。
    """
    amount = None if record.amount is None else float(record.amount)
    raw = json.dumps(
        (
            record.source_report,
            record.security_code,
            record.trading_day,
            record.period,
            record.explanation,
            record.operatedept_code,
            record.direction,
            amount,
            record.source_record_id,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_amount(amount: object) -> bool:
    """金额必须为有限正数；None/0/负数/NaN/inf 都不合格（方案 §四 统一约束）。"""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return False
    value = float(amount)
    return math.isfinite(value) and value > 0


def _normalize_window(trading_days: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """交易日序列去重升序；格式不合法直接抛错（不猜）。"""
    seen: set[str] = set()
    for day in trading_days:
        if not isinstance(day, str) or len(day) != 8 or not day.isdigit():
            raise AnnotatorInputError(f"交易日格式不合法（应为 YYYYMMDD）：{day!r}")
        try:
            date(int(day[:4]), int(day[4:6]), int(day[6:8]))
        except ValueError as exc:
            raise AnnotatorInputError(f"无效日期：{day!r}") from exc
        seen.add(day)
    return tuple(sorted(seen))


def _normalize_coverage(
    coverage: dict[str, str], window: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    """覆盖状态补全为窗口逐日（缺省 unknown），未知状态名直接抛错。"""
    out: list[tuple[str, str]] = []
    for day in window:
        state = coverage.get(day, COVERAGE_UNKNOWN)
        if state not in KNOWN_COVERAGES:
            raise AnnotatorInputError(f"未知覆盖状态：{state!r}（day={day}）")
        out.append((day, state))
    return tuple(out)


def annotate(
    security_code: str,
    records: list[NormalizedSeatRecord],
    trading_days: list[str] | tuple[str, ...],
    coverage: dict[str, str],
    *,
    as_of: str | None = None,
) -> AnnotationResult:
    """对一只证券计算三类跨日事实（纯函数；as_of 截断防未来信息泄漏）。

    - `trading_days` 是**显式**交易日序列（含查询窗口及其前序最多四个交易日）；
      序列为空或某日覆盖非 complete 时，窗口不完整 → `facts` 为空并给出理由，
      不用不完整输入生成「未检出」结论。
    - `as_of` 之后（不含）的记录与窗口日一律忽略；追加未来记录不改变过去截至日结果。
    """
    code = (security_code or "").strip()
    if not code:
        raise AnnotatorInputError("证券代码不能为空")
    window = _normalize_window(trading_days)
    if as_of is not None:
        if not (isinstance(as_of, str) and len(as_of) == 8 and as_of.isdigit()):
            raise AnnotatorInputError(f"as_of 格式不合法（应为 YYYYMMDD）：{as_of!r}")
        _normalize_window([as_of])
        window = tuple(day for day in window if day <= as_of)
    # 本调用仅计算截至日最近五个显式交易日；不以自然日差值猜测休市。
    window = window[-5:]
    coverage_pairs = _normalize_coverage(coverage, window)

    excluded: dict[str, int] = {}
    if not window:
        return AnnotationResult(
            security_code=code,
            window_days=(),
            coverage=(),
            window_complete=False,
            incomplete_reason="交易日序列为空（无可信日历或未提供窗口）",
            facts=(),
            conflicts=(),
            excluded=(),
        )

    bad_states = [f"{day}:{state}" for day, state in coverage_pairs if state != COVERAGE_COMPLETE]
    if bad_states:
        return AnnotationResult(
            security_code=code,
            window_days=window,
            coverage=coverage_pairs,
            window_complete=False,
            incomplete_reason=f"窗口覆盖不完整：{','.join(bad_states)}",
            facts=(),
            conflicts=(),
            excluded=(),
        )

    if len(window) < 5:
        return AnnotationResult(
            security_code=code,
            window_days=window,
            coverage=coverage_pairs,
            window_complete=False,
            incomplete_reason="五交易日上下文不足",
            facts=(),
            conflicts=(),
            excluded=(),
        )

    window_set = set(window)
    names_by_evidence: dict[str, set[str]] = {}
    # 合格行过滤（Y2-02）：可靠身份 + 已核验单日 + 方向金额有效为正 + 窗口内。
    qualified: dict[str, dict[str, dict[str, list[tuple[float, str, str]]]]] = {}
    for record in records:
        if record.security_code != code:
            continue
        if record.trading_day not in window_set:
            continue  # 窗口外记录不改变历史结果，包括排除摘要。
        if not record.operatedept_code.strip() or record.operatedept_code.strip() == "0":
            excluded["identity_unknown"] = excluded.get("identity_unknown", 0) + 1
            continue
        if record.identity_kind != IDENTITY_BRANCH:
            key = (
                "identity_bucket" if record.identity_kind == IDENTITY_BUCKET else "identity_unknown"
            )
            excluded[key] = excluded.get(key, 0) + 1
            continue
        if record.period != PERIOD_SINGLE_DAY:
            key = "period_multi_day" if record.period == PERIOD_MULTI_DAY else "period_unknown"
            excluded[key] = excluded.get(key, 0) + 1
            continue
        if record.direction not in ("buy", "sell"):
            excluded["direction_invalid"] = excluded.get("direction_invalid", 0) + 1
            continue
        if not _valid_amount(record.amount):
            excluded["amount_invalid"] = excluded.get("amount_invalid", 0) + 1
            continue
        assert record.amount is not None
        evidence_id = evidence_id_of(record)
        names_by_evidence.setdefault(evidence_id, set()).add(record.operatedept_name)
        seat = qualified.setdefault(record.operatedept_code, {})
        by_day = seat.setdefault(record.trading_day, {})
        by_day.setdefault(record.direction, []).append(
            (float(record.amount), evidence_id, record.explanation)
        )

    facts: list[FactTag] = []
    conflicts: list[dict[str, object]] = []
    # 预检所有组，不依赖标签匹配及 any() 短路，首日卖侧冲突也必须可见。
    for seat_code, seat_days in sorted(qualified.items()):
        for day, sides in sorted(seat_days.items()):
            for direction in sorted(sides):
                _unique_entry(seat_days, day, direction, conflicts, seat_code)

    # —— adjacent_buy_sell：D 日买榜、下一交易日卖榜（方案 Y2-03；反向不算） ——
    for index in range(len(window) - 1):
        day_buy, day_sell = window[index], window[index + 1]
        for seat_code, seat_days in sorted(qualified.items()):
            buy_entry = _unique_entry(seat_days, day_buy, "buy", conflicts, seat_code)
            sell_entry = _unique_entry(seat_days, day_sell, "sell", conflicts, seat_code)
            if buy_entry is None or sell_entry is None:
                continue
            evidence = (
                _evidence_item(day_buy, "buy", buy_entry),
                _evidence_item(day_sell, "sell", sell_entry),
            )
            facts.append(
                FactTag(
                    fact_type=FACT_ADJACENT_BUY_SELL,
                    security_code=code,
                    operatedept_code=seat_code,
                    operatedept_name="",
                    days=(day_buy, day_sell),
                    evidence=evidence,
                    fact_id=_fact_id(
                        FACT_ADJACENT_BUY_SELL, code, seat_code, (day_buy, day_sell), evidence
                    ),
                )
            )

    # —— consecutive_buy：连续交易日买榜（最大连续区间，不生成子区间） ——
    for seat_code, seat_days in sorted(qualified.items()):
        run: list[str] = []
        runs: list[tuple[str, ...]] = []
        for day in window:
            if _unique_entry(seat_days, day, "buy", conflicts, seat_code) is not None:
                run.append(day)
            else:
                if len(run) >= 2:
                    runs.append(tuple(run))
                run = []
        if len(run) >= 2:
            runs.append(tuple(run))
        for run_days in runs:
            evidence = tuple(
                _evidence_item(
                    day, "buy", _unique_entry(seat_days, day, "buy", conflicts, seat_code)
                )
                for day in run_days
            )
            facts.append(
                FactTag(
                    fact_type=FACT_CONSECUTIVE_BUY,
                    security_code=code,
                    operatedept_code=seat_code,
                    operatedept_name="",
                    days=run_days,
                    evidence=evidence,
                    fact_id=_fact_id(FACT_CONSECUTIVE_BUY, code, seat_code, run_days, evidence),
                )
            )

    # —— repeated_seat_presence：窗口内至少两个不同交易日出现（买或卖） ——
    for seat_code, seat_days in sorted(qualified.items()):
        presence_days = tuple(
            day
            for day in window
            if any(
                _unique_entry(seat_days, day, direction, conflicts, seat_code) is not None
                for direction in ("buy", "sell")
            )
        )
        if len(presence_days) < 2:
            continue
        evidence: list[dict[str, object]] = []
        for day in presence_days:
            for direction in ("buy", "sell"):
                entry = _unique_entry(seat_days, day, direction, conflicts, seat_code)
                if entry is not None:
                    evidence.append(_evidence_item(day, direction, entry))
        facts.append(
            FactTag(
                fact_type=FACT_REPEATED_PRESENCE,
                security_code=code,
                operatedept_code=seat_code,
                operatedept_name="",
                days=presence_days,
                evidence=tuple(evidence),
                fact_id=_fact_id(
                    FACT_REPEATED_PRESENCE, code, seat_code, presence_days, tuple(evidence)
                ),
            )
        )

    # 展开每个参与日/方向的全部不同来源证据；不把多原因金额相加。
    expanded: list[FactTag] = []
    for fact in facts:
        evidence_by_id: dict[str, dict[str, object]] = {}
        for item in fact.evidence:
            day, direction = str(item["trading_day"]), str(item["direction"])
            for entry in qualified[fact.operatedept_code][day][direction]:
                evidence_by_id[entry[1]] = _evidence_item(day, direction, entry)
        evidence = tuple(
            sorted(
                evidence_by_id.values(),
                key=lambda item: (
                    str(item["trading_day"]),
                    str(item["direction"]),
                    str(item["evidence_id"]),
                ),
            )
        )
        names = sorted(
            {name for evidence_id in evidence_by_id for name in names_by_evidence[evidence_id]}
        )
        expanded.append(
            replace(
                fact,
                evidence=evidence,
                operatedept_name=names[0] if len(names) == 1 else "",
                fact_id=_fact_id(fact.fact_type, code, fact.operatedept_code, fact.days, evidence),
            )
        )
    facts = sorted(expanded, key=lambda item: (item.fact_type, item.operatedept_code, item.days))
    conflicts.sort(
        key=lambda item: (
            str(item["operatedept_code"]),
            str(item["trading_day"]),
            str(item["direction"]),
        )
    )
    if conflicts:
        excluded["amount_conflict_groups"] = len(conflicts)
    excluded_pairs = tuple(sorted(excluded.items()))
    return AnnotationResult(
        security_code=code,
        window_days=window,
        coverage=coverage_pairs,
        window_complete=True,
        incomplete_reason="",
        facts=tuple(facts),
        conflicts=tuple(conflicts),
        excluded=excluded_pairs,
    )


def _unique_entry(
    seat_days: dict[str, dict[str, list[tuple[float, str, str]]]],
    day: str,
    direction: str,
    conflicts: list[dict[str, object]],
    seat_code: str,
) -> tuple[float, str, str] | None:
    """同日同方向唯一金额入口；多原因重复且金额一致视为同源，冲突则不生成金额对照。"""
    entries = seat_days.get(day, {}).get(direction, [])
    if not entries:
        return None
    amounts = {item[0] for item in entries}
    if len(amounts) > 1:
        conflict = {
            "operatedept_code": seat_code,
            "trading_day": day,
            "direction": direction,
            "amounts": sorted(amounts),
            "evidence_ids": sorted({item[1] for item in entries}),
            "note": "同日同方向金额冲突，不任取一条生成对照",
        }
        if conflict not in conflicts:
            conflicts.append(conflict)
        return None
    return min(entries, key=lambda item: item[1])


def _evidence_item(day: str, direction: str, entry: tuple[float, str, str]) -> dict[str, object]:
    amount, evidence_id, explanation = entry
    return {
        "trading_day": day,
        "direction": direction,
        "amount": amount,
        "evidence_id": evidence_id,
        "explanation": explanation,
    }


def _fact_id(
    fact_type: str,
    security_code: str,
    seat_code: str,
    days: tuple[str, ...],
    evidence: tuple[dict[str, object], ...],
) -> str:
    """稳定事实 ID：类型+证券+席位+日期序列+排序后证据集合（乱序输入结果一致）。"""
    ids = ",".join(sorted(str(item["evidence_id"]) for item in evidence))
    raw = "|".join((fact_type, security_code, seat_code, ";".join(days), ids))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
