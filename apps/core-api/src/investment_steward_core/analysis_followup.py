"""研报追问与二次分析（v32 起，2026-09-19 质量路线图 P0 升级）。

定位（2026-09-13 方案 §三 + 2026-09-19 路线图 §3）：
- **原报告不可变**：追问结果一律生成为追加的「分析附录」（analysis turn），绝不改写
  也不覆盖原报告 payload；每次追问记录父报告 id、基础版本、证据快照、模型与提示词版本，
  允许回看「当时为什么改变判断」；
- **先回答用户的问题**（Q02）：契约里单列 `direct_answer / answerability / key_conditions /
  evidence_gaps`，让「直接回答 → 数据截至 → 关键条件 → 补证缺口」成为固定顺序，
  审计信息（判断影响表、模型与提示词版本）折叠在后，而不是让 C1—C5 表格占满首屏；
- **三条状态轴互不替身**（Q03）：`revision_status`（原判断是否修订）、
  `answerability`（本次问题可回答程度）、`data_basis`（是否使用新增数据）分别成立，
  无新增证据时如实写「沿用原报告判断，未更新行情」，不暗示做过最新复核；
- **外部信息两条路径**（§3.2）：系统事件（带事件 id/来源/日期快照）与用户补充材料
  （原文 + 来源名 + URL + 事件日期）。用户补充**默认未独立验证**，不能直接成为公司事实，
  必须先经事实归一化（主体/事件/日期/原文完整性/来源可信度/与标的关系），再做影响分析；
- **结果契约**（§3.3）：受影响 claim（supports/weakens/irrelevant/cannot_judge）、
  结论变化（unchanged/strengthened/weakened/changed/undetermined）、情景变化、
  新增验证点、回答正文与局限。claim_id 只能取原报告已列出的编号——闸门确定性校验，
  模型自造的编号一律剔除并留痕。

设计铁律（与全站一致）：本模块纯函数、无 IO；提示词构造与归一化在此，调用、取数与落库由
app.py 决定。交易日历由调用方以 `trading_calendar.TradingCalendar` 传入；无日历时只做
「日期依据」闸门，不自行推算交易日。
"""

from __future__ import annotations

import re
from typing import Any

from investment_steward_core import followup_research, report_quality, trading_calendar

# 追问分析提示词版本：改模板或契约必须递增（与研报提示词同一约定）。
# 1.1：新增方向研判追问骨架（core_judgments / board_judgments / next_verification）。
# 1.2：P0 契约升级——直接回答与三条状态轴、判断分组、估值三态、价位时点与验证日期闸门。
FOLLOWUP_PROMPT_VERSION = "1.2"

# 单次追问最多带几条补充材料（成本与上下文保护；超过部分由调用方拒绝）。
MAX_SUPPLEMENTS = 3
MAX_SUPPLEMENT_CHARS = 4000
MAX_QUESTION_CHARS = 500

# 受影响 claim 的四态（§3.3 契约）；模型输出越界一律归 cannot_judge（不猜）。
EFFECT_VOCAB = ("supports", "weakens", "irrelevant", "cannot_judge")
EFFECT_LABELS: dict[str, str] = {
    "supports": "支持原判断",
    "weakens": "削弱原判断",
    "irrelevant": "与本判断无关",
    "cannot_judge": "证据不足，无法判断",
}
# 展开态只给真正改变了证据格局的判断（Q04）；其余合并成一行共同说明，不逐条刷屏。
EXPANDED_EFFECTS = ("supports", "weakens")

# 结论变化五态（§3.3 契约）。
CONCLUSION_CHANGES = ("unchanged", "strengthened", "weakened", "changed", "undetermined")
CONCLUSION_CHANGE_LABELS: dict[str, str] = {
    "unchanged": "结论不变",
    "strengthened": "结论增强",
    "weakened": "结论减弱",
    "changed": "结论改变",
    "undetermined": "无法判定",
}

# —— Q03：三条状态轴（原判断是否修订 / 本次问题能否回答 / 是否使用新增数据）——
REVISION_VOCAB = ("unrevised", "revised", "undetermined")
REVISION_LABELS: dict[str, str] = {
    "unrevised": "原判断未修订",
    "revised": "原判断需修订",
    "undetermined": "修订状态待定",
}
ANSWERABILITY_VOCAB = ("answered", "partially_answered", "not_answerable")
ANSWERABILITY_LABELS: dict[str, str] = {
    "answered": "本次问题已回答",
    "partially_answered": "部分回答（存在未证实环节）",
    "not_answerable": "以现有证据无法回答",
}
DATA_BASIS_VOCAB = ("report_only", "user_supplement", "refreshed_data")
DATA_BASIS_LABELS: dict[str, str] = {
    "report_only": "沿用原报告判断，未更新行情",
    "user_supplement": "使用用户补充材料（未独立验证），未更新行情",
    "refreshed_data": "使用本次刷新获取的数据",
}

# 追问入口两种模式（Q10）：解读本报告 / 补充研究。
MODE_INTERPRET = "interpret"
MODE_RESEARCH = "supplement_research"
MODE_LABELS: dict[str, str] = {
    MODE_INTERPRET: "解读本报告（只依据原报告与你的补充材料，不获取新事实）",
    MODE_RESEARCH: "补充研究（本次重新获取行情/公告等数据，结果如实标注来源与失败项）",
}

# 用户补充材料的固定信任标签（§3.2：默认未独立验证，必须显著标注）。
USER_SUPPLEMENT_TRUST = "用户补充材料/未独立验证"
SYSTEM_EVENT_TRUST = "系统事件快照（未独立核验，仅作线索）"

# —— Q07：量价口径必须拆开描述，不能把「单票收入不大幅下降」当成「量价齐升」——
VOLUME_PRICE_TERMS: dict[str, str] = {
    "量价齐升": "业务量上升且单票收入上升",
    "量增价稳": "业务量上升且单票收入基本持平",
    "量增价降": "业务量上升但单票收入下降",
}
_VOLUME_PRICE_PATTERN = re.compile("|".join(VOLUME_PRICE_TERMS))
_REVENUE_PER_PARCEL_FLAT = re.compile(r"单票收入[^。；;]{0,12}?(?:不大幅下降|未大幅下滑|基本持平|稳定)")

# —— Q07：验证点日期的依据来源（无可靠依据的日期一律降级为事件锚定）——
DATE_BASIS_VOCAB = ("trading_calendar", "disclosed_schedule", "input_evidence")
DATE_BASIS_LABELS: dict[str, str] = {
    "trading_calendar": "交易日历推算",
    "disclosed_schedule": "已披露的披露/事件日程",
    "input_evidence": "输入材料给出的日期",
    "": "无依据（模型自述）",
}
# 预计时间词：与事实时间分开标注（Q07）。
_ESTIMATED_DATE_WORDS = re.compile(r"预计|预期|大约|约")

_SUPPLEMENT_SYSTEM = (
    "你是研报复核分析员：基于一份**已完成且不可变**的研究报告与用户补充的外部信息做二次分析。"
    "只依据原报告内容与给定补充材料，绝不引入两者之外的事实；补充材料默认未经独立验证，"
    "引用时必须注明「用户补充材料/未独立验证」；证据不足就如实说无法判断。"
    "你的输出是追加的分析附录，不改写原报告的任何结论。输出必须且只能是单个合法 JSON 对象。"
)

_RESEARCH_SYSTEM = (
    "你是研报复核分析员：在一份**已完成且不可变**的研究报告之外，本轮可使用系统**新获取**的"
    "行情、公告、经营或财务数据（见「本次新增数据」块）做补充研究。"
    "新增数据必须逐条注明来源与数据日期；获取失败或缺失的项要如实说明，不得用推测填补；"
    "用户补充材料仍默认未独立验证。你的输出是追加的分析附录，不改写原报告的任何结论。"
    "输出必须且只能是单个合法 JSON 对象。"
)

# 契约 JSON：以「先回答」为第一字段，审计字段一律后置（Q02 的顺序由契约保证）。
_ANSWER_CONTRACT = (
    '{"direct_answer": "对用户问题的直接回答，≤100 字：先说能/不能、可/不可观察，'
    '再点出最关键的一条限制；不许用背景铺垫开头，不许写成整段分析", '
    '"answerability": "answered|partially_answered|not_answerable", '
    '"key_conditions": ["直接回答成立所依赖的条件，≤3 条，每条自带时点或价位口径"], '
    '"evidence_gaps": ["还缺什么证据才能补齐现在无法回答的部分，≤3 条，写明可获取的来源类型"], '
    '"affected_claims": [{"claim_id": "C1", "effect": "supports|weakens|irrelevant|cannot_judge", '
    '"reason": "40 字内：新信息如何影响该判断"}], '
    '"conclusion_change": "unchanged|strengthened|weakened|changed|undetermined", '
    '"scenario_changes": [{"name": "受影响的情景名", "change": "如何变化（40 字内）"}], '
    '"new_watchpoints": [{"signal": "需要新增跟踪的信号", '
    '"verify_by": "YYYY-MM-DD 或锚定事件（如「11 月经营简报披露后」）", '
    '"date_basis": "trading_calendar|disclosed_schedule|input_evidence（日期从哪来，给不出就留空）", '
    '"event_anchor": "触发这条验证的事件（无日期时的表述）", '
    '"expected_if_true": "成立后倾向何结论"}], '
    '"price_refs": [{"level": 数字, "role": "close|ma|support|resistance|other", '
    '"as_of": "该价位的行情截至日 YYYY-MM-DD", "window": "适用窗口（如「未来 5-10 个交易日」）"}], '
    '"answer": "面向用户问题的展开说明（markdown，250-700 字：先给依据路径与口径，'
    '不要重复 direct_answer；引用补充材料必须注明「用户补充材料/未独立验证」；'
    '无法判断的部分如实说明缺什么证据）", '
    '"limitations": ["1-3 条本次二次分析的口径限制"]}'
)

_ANSWER_RULES = (
    "铁律：\n"
    "1. claim_id 只能取上面核心判断清单里已有的编号；清单没有的不许编；\n"
    "2. effect 证据不足时必须用 cannot_judge，不许硬选支持或削弱；\n"
    "3. conclusion_change 描述的是「原判断是否需要修订」，不是「本次问题能否回答」，"
    "也不是「数据是否更新」——三件事各说各的；不确定就 undetermined；\n"
    "4. 没有新增证据时，conclusion_change 用 unchanged，并在 limitations 写明"
    "「本轮未获取新数据，结论沿用原报告」，不得暗示已复核最新行情；\n"
    "5. new_watchpoints 的 verify_by 要么给**有依据的具体日期**（填 date_basis 说明依据），"
    "要么给可识别事件（填 event_anchor）；两者都给不出时留空，禁止「下月/后续」这类相对词，"
    "也禁止把「双十一」直接换算成 11-30 这类无依据截止日；\n"
    "6. 行情、均线与结构价位只能引用「行情快照时点」块里给出的截至日；"
    "跨期问题（如问 11 月）不得把当前均线当未来实时阈值，须写明是历史快照；\n"
    "7. 「量价齐升」「量增价稳」「量增价降」是三件不同的事，按业务量与单票收入分别描述，"
    "不得用「单票收入不大幅下降」支撑「量价齐升」；\n"
    "8. 未经统计校准不得给上涨/下跌概率；历史高点、历史低点只是参考位，"
    "没有推导过程不得写进未来目标区间；\n"
    "9. 原报告不可变：你的全部输出都是追加的分析附录，不是对原报告的修改。"
)


def normalize_supplements(items: Any, *, input_at: str) -> list[dict[str, Any]]:
    """补充材料归一（§3.2）：用户材料默认 `verified=False` + 固定信任标签；系统事件带快照来源。

    输入 item：{text, source_name?, url?, event_date?, origin?: "user"|"system_event",
    event_id?, source_provider?}。origin 缺省按 user 处理（宁按低信任，不冒充系统数据）。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for index, item in enumerate(items[:MAX_SUPPLEMENTS]):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:MAX_SUPPLEMENT_CHARS]
        if not text:
            continue
        origin = "system_event" if str(item.get("origin", "")).strip() == "system_event" else "user"
        out.append({
            "supplement_id": f"U{index + 1}",
            "origin": origin,
            "text": text,
            "source_name": str(item.get("source_name", "")).strip()[:120],
            "url": str(item.get("url", "")).strip()[:500],
            "event_date": str(item.get("event_date", "")).strip()[:10],
            "input_at": input_at,
            # §3.2 铁律：用户补充与系统事件都**不是已验证的公司事实**，只作待核验线索。
            "verified": False,
            "trust_label": SYSTEM_EVENT_TRUST if origin == "system_event" else USER_SUPPLEMENT_TRUST,
            "event_id": str(item.get("event_id", "")).strip()[:120],
            "source_provider": str(item.get("source_provider", "")).strip()[:120],
        })
    return out


def normalize_facts(supplements: list[dict[str, Any]], *, subject: str) -> list[dict[str, Any]]:
    """事实归一化（§3.2）：每条补充材料输出 主体/事件/日期/原文完整性/来源可信度/与标的关系。

    全部为**确定性**归一（不做语义判断）：
    - 完整性 = 原文是否同时给出「数字 + 事件描述」（缺数字的线索按不完整标注）；
    - 来源可信度 = 有来源名/URL 记「用户声明来源」，否则「无来源声明」（更低）；
    - 与标的关系 = 原文是否出现标的代码（出现记 direct_hint，否则 unknown，由影响分析判断）。
    """
    has_digit = re.compile(r"\d")
    out: list[dict[str, Any]] = []
    for item in supplements:
        text = str(item.get("text", ""))
        out.append({
            "supplement_id": item.get("supplement_id"),
            "subject": subject,
            "event": text[:120],
            "date": str(item.get("event_date") or item.get("input_at") or "")[:10],
            "date_is_input_time": not bool(str(item.get("event_date") or "").strip()),
            "complete": bool(has_digit.search(text)) and len(text.strip()) >= 20,
            "source_credibility": (
                "用户声明来源" if (item.get("source_name") or item.get("url")) else "无来源声明"
            ),
            "relation": "direct_hint" if subject and subject in text else "unknown",
            "trust_label": item.get("trust_label"),
        })
    return out


# ---------------------------------------------------------------------------
# Q02/Q03/Q04/Q06/Q07/Q08：确定性归一工具（纯函数，供 normalize_followup 与旧记录回显复用）
# ---------------------------------------------------------------------------

def split_volume_price_terms(text: str) -> list[str]:
    """把混用的量价口径拆成可分别验证的假设（Q07）。

    返回展开后的口径行；原文没提到量价组合时返回空列表（不造假设）。
    「单票收入不大幅下降/基本持平」属**量增价稳**，与「量价齐升」支持不同结论，
    因此两者同时出现时按各自口径分列，不做合并。
    """
    body = str(text or "")
    if not body:
        return []
    found: list[str] = []
    for term in _VOLUME_PRICE_PATTERN.findall(body):
        line = f"{term}＝{VOLUME_PRICE_TERMS[term]}"
        if line not in found:
            found.append(line)
    if _REVENUE_PER_PARCEL_FLAT.search(body) and not any(
        term in found for term in VOLUME_PRICE_TERMS["量增价稳"].split("且")
    ):
        found.append(
            "「单票收入不大幅下降/基本持平」属量增价稳口径，不等于量价齐升（后者要求单票收入上升）"
        )
    return found


def gate_verify_by(
    item: dict[str, Any],
    *,
    calendar: trading_calendar.TradingCalendar | None,
    today: str = "",
) -> dict[str, Any]:
    """验证点日期闸门（Q07）：没有可靠依据的日期一律降级为事件锚定。

    - `date_basis` 不在词表内（模型没说日期从哪来）→ 丢弃日期，改用 `event_anchor`，
      原值留痕在 `date_demoted_from`（如样例里的 2026-11-30）；
    - 有依据且传入交易日历时再核一次：周末/节假日顺延到下一交易日，落在行情覆盖期外
      （数据没刷到那天）同样丢弃日期改事件锚定；
    - `disclosed_schedule` / `input_evidence` 属**事实时间**，`trading_calendar` 属**预计时间**，
      由 `date_kind` 分开表达，前端不得混写。
    """
    raw = str(item.get("verify_by", "") or "").strip()
    basis = str(item.get("date_basis", "") or "").strip().lower()
    anchor = str(item.get("event_anchor", "") or "").strip()
    out: dict[str, Any] = {
        "date_basis": basis if basis in DATE_BASIS_VOCAB else "",
        "date_basis_label": DATE_BASIS_LABELS.get(basis if basis in DATE_BASIS_VOCAB else "", ""),
        "date_kind": "",
        "date_note": "",
        "date_demoted_from": "",
    }
    parsed = trading_calendar.parse_date(raw)
    if parsed is None:
        out["date_kind"] = trading_calendar.DATE_KIND_EVENT if raw else ""
        out["date_note"] = "验证时点为事件表述。" if raw else "未给出验证时点。"
        return out
    if out["date_basis"] == "":
        out["date_demoted_from"] = trading_calendar.format_day(parsed)
        out["date_kind"] = trading_calendar.DATE_KIND_DEMOTED
        # 降级后的时点文本里**不能再出现那个日期**：否则调用方按 `verify_by` 再解析一次就把
        # 凭空生成的截止日又请回来了（due_on 会重新填上）。原日期只留在 date_demoted_from 供审计。
        out["verify_by"] = anchor or "时点待定（缺可靠日期依据，须人工补）"
        out["date_note"] = (
            "模型给出的日期没有依据（未说明来自交易日历、已披露日程还是输入材料），"
            "已降级为事件锚定表述。"
        )
        return out
    if calendar is None or not calendar.available:
        out["date_kind"] = (
            trading_calendar.DATE_KIND_FACTUAL
            if basis in ("disclosed_schedule", "input_evidence")
            else trading_calendar.DATE_KIND_ESTIMATED
        )
        out["date_note"] = "未取得交易日历，日期未做交易日校验，按输入依据采信并标注。"
        return out
    resolved = trading_calendar.resolve_date(
        raw,
        calendar=calendar,
        today=today,
        factual=basis in ("disclosed_schedule", "input_evidence"),
    )
    if not resolved["resolved"]:
        out["date_demoted_from"] = resolved["input"]
        out["date_kind"] = trading_calendar.DATE_KIND_DEMOTED
        out["verify_by"] = anchor or "时点待定（超出行情日历覆盖期，须到点重新取数）"
        out["date_note"] = resolved["note"]
        return out
    out["date_kind"] = (
        trading_calendar.DATE_KIND_FACTUAL
        if basis in ("disclosed_schedule", "input_evidence")
        else trading_calendar.DATE_KIND_ESTIMATED
    )
    out["date_note"] = resolved["note"]
    out["verify_by"] = resolved["resolved"]
    return out


def normalize_price_refs(
    value: Any,
    *,
    snapshot_levels: list[dict[str, Any]],
    data_as_of: str,
    calendar: trading_calendar.TradingCalendar | None,
    refreshed: bool,
) -> list[dict[str, Any]]:
    """价位时点口径（Q06）：每个被引用的价位都带截至日期、适用窗口与「是否只是历史快照」。

    规则（确定性，不信任模型自报）：
    - 非数值价位剔除；`as_of` 缺失或解析不出 → 补原报告数据截至日并留痕；
    - 本轮未刷新行情（`refreshed=False`）→ `snapshot_only=True`，标注只能引用历史快照；
    - 适用窗口写到未来（含「未来/后续/N 个交易日」）而未刷新 → 明确「非实时阈值」；
    - 价位与原报告快照不一致（±0.5% 之外）→ 标注「原报告未给出该价位」，供人工复核。
    """
    rows = value if isinstance(value, list) else []
    known = [row.get("price") for row in snapshot_levels if isinstance(row.get("price"), (int, float))]
    out: list[dict[str, Any]] = []
    for item in rows[:6]:
        if not isinstance(item, dict):
            continue
        try:
            level = float(item.get("level"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        role = str(item.get("role", "")).strip().lower() or "other"
        as_of = str(item.get("as_of", "")).strip()
        parsed_as_of = trading_calendar.parse_date(as_of)
        filled = ""
        if parsed_as_of is None:
            as_of = data_as_of
            filled = "模型未给截至日期，已回填原报告数据截至日。"
        window = str(item.get("window", "")).strip()[:60]
        forward_looking = bool(re.search(r"未来|后续|下周|下月|\d+\s*个?交易日", window))
        notes: list[str] = []
        if filled:
            notes.append(filled)
        snapshot_only = not refreshed
        if snapshot_only:
            notes.append("本轮未刷新行情，该价位为历史快照，不作为未来时点的实时阈值。")
        if forward_looking and snapshot_only:
            notes.append("适用窗口指向未来，但数据未刷新：到点须重新取数后再判断。")
        if parsed_as_of is not None and (calendar is not None and calendar.available):
            kind = calendar.day_kind(parsed_as_of)
            if kind in (trading_calendar.DAY_KIND_WEEKEND, trading_calendar.DAY_KIND_HOLIDAY):
                notes.append(f"截至日 {as_of} 非交易日，实际快照日为区间内最近一根有效 K 线。")
            elif kind == trading_calendar.DAY_KIND_OUTSIDE:
                notes.append("截至日超出行情日历覆盖期，按历史快照引用。")
        matches_snapshot = any(abs(level - float(known_price)) <= max(0.005 * float(known_price), 1e-6)
                               for known_price in known)
        if known and not matches_snapshot:
            notes.append("该价位不在原报告快照价位清单内，须回查来源后再使用。")
        out.append({
            "level": round(level, 4),
            "role": role if role in ("close", "ma", "support", "resistance", "other") else "other",
            "as_of": as_of,
            "window": window,
            "snapshot_only": snapshot_only,
            "forward_looking": forward_looking,
            "note": "；".join(notes),
        })
    return out


def group_claims(
    affected: list[dict[str, Any]],
    claims_by_id: dict[str, str],
    *,
    data_basis: str,
) -> dict[str, Any]:
    """受影响判断分组（Q04）：只展开与问题真正相关的判断，共同限制说明一次。

    不改变任何一条的 effect 取值（无法判断仍是无法判断），只改变**展示层级**。
    """
    expanded: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for row in affected:
        named = dict(row)
        named["claim_name"] = str(claims_by_id.get(row.get("claim_id", "")) or "").strip()
        if row.get("effect") in EXPANDED_EFFECTS:
            expanded.append(named)
        else:
            unresolved.append(named)
    shared = ""
    if unresolved:
        ids = "、".join(str(row.get("claim_id") or "—") for row in unresolved)
        reason = DATA_BASIS_LABELS.get(data_basis, "本轮无新增证据")
        shared = (
            f"{ids}：本轮{reason}——未取得可改变这些判断的新材料，"
            "原结论按未复核处理（不因此转为支持或削弱）。"
        )
    return {"expanded_claims": expanded, "unresolved_claims": unresolved, "shared_limitation": shared}


def valuation_view(valuation: Any) -> dict[str, Any]:
    """估值三态视图（Q05）：指标缺失 / 指标已取得但合理价值未评估 / 合理价值已评估。

    直接复用 `report_quality.derive_valuation_evidence_state`，让追问、研报页与导出同口径。
    """
    return report_quality.derive_valuation_evidence_state(
        valuation if isinstance(valuation, dict) else {}
    )


def review_watchpoint_consistency(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """验证点的重复识别与方向一致性核对（Q07 验收后半句）。

    样例里两条验证点都挂在同一个 `2026-11-30` 上，一条说「量价齐升→强化」、一条说
    「放量跌破→削弱」——读者看到的是「同一时点有两条相反预期」，而不是两条独立检查。
    确定性规则（不做语义猜测）：

    - **重复**：信号文本去标点取前 40 字作键，与前一条相同则标 `duplicate_of`（指向被合并条目的
      序号），并说明合并理由；不改写、不删除任何一条（是否删由人判断）；
    - **同时点相反预期**：`due_on`（或事件锚定文本）相同的两条，若一条预期偏强、一条预期偏弱，
      两条各得一条 `direction_note`，提示按触发条件分开限定；
    - **触发与预期自相矛盾**：同一条里信号含「跌破/失守/下拐」而预期写「强化/看涨/成立」，
      或信号含「站上/突破/抬高」而预期写「削弱/转弱」→ 标 `direction_note`。
    """
    def _key(text: str) -> str:
        return re.sub(r"[\s，。、；;：:（）()「」《》\-—…]", "", str(text or ""))[:40]

    bullish = ("强化", "偏多", "看涨", "成立", "增强", "转强")
    bearish = ("削弱", "偏空", "看跌", "转弱", "下修", "不成立")
    down_triggers = ("跌破", "失守", "下拐", "跌破位", "破位")
    up_triggers = ("站上", "突破", "抬高", "放量越过", "企稳")

    by_when: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        when = str(row.get("due_on") or row.get("verify_by") or "").strip()
        if when:
            by_when.setdefault(when, []).append(index)

    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        key = _key(row.get("signal"))
        notes: list[str] = []
        if key and key in seen:
            row["duplicate_of"] = seen[key] + 1
            notes.append(f"与第 {seen[key] + 1} 条为同一信号（文本去标点后一致），合并看待即可，不必重复占位。")
        elif key:
            seen[key] = index

        signal_text = str(row.get("signal") or "")
        expected_text = f"{row.get('expected_if_true') or ''}"
        if any(word in signal_text for word in down_triggers) and any(word in expected_text for word in bullish):
            notes.append("触发条件写的是下行破位，预期却写「强化/成立」——方向自相矛盾，请按实际含义二选一。")
        if any(word in signal_text for word in up_triggers) and any(word in expected_text for word in bearish):
            notes.append("触发条件写的是站上/突破，预期却写「削弱/转弱」——方向自相矛盾，请按实际含义二选一。")

        when = str(row.get("due_on") or row.get("verify_by") or "").strip()
        peers = [i for i in by_when.get(when, []) if i != index] if when else []
        this_bullish = any(word in expected_text for word in bullish)
        this_bearish = any(word in expected_text for word in bearish)
        for peer in peers:
            peer_expected = str(rows[peer].get("expected_if_true") or "")
            peer_bullish = any(word in peer_expected for word in bullish)
            peer_bearish = any(word in peer_expected for word in bearish)
            if (this_bullish and peer_bearish) or (this_bearish and peer_bullish):
                notes.append(f"与第 {peer + 1} 条同一时点但预期相反（一条偏强化、一条偏削弱）："
                             "两条都要成立需各自限定触发条件，不能合成一句结论。")
                break
        row["duplicate_of"] = row.get("duplicate_of") or 0
        row["direction_note"] = "；".join(notes)
    return rows


def status_axes(
    *,
    conclusion_change: str,
    answerability: str,
    affected: list[dict[str, Any]],
    has_supplements: bool,
    has_refreshed_data: bool,
) -> dict[str, Any]:
    """三条状态轴（Q03）+ 自相矛盾留痕。

    无新材料却声称结论增强/改变 → 状态轴冲突，如实标 `status_conflict` 并把修订轴
    归为 undetermined，而不是替模型选一个好看的口径。
    """
    data_basis = (
        "refreshed_data" if has_refreshed_data
        else "user_supplement" if has_supplements
        else "report_only"
    )
    revised = conclusion_change in ("strengthened", "weakened", "changed")
    supporting = [row for row in affected if row.get("effect") in EXPANDED_EFFECTS]
    conflict = ""
    revision = "revised" if revised else (
        "undetermined" if conclusion_change == "undetermined" else "unrevised"
    )
    if revised and not supporting and not has_refreshed_data and not has_supplements:
        conflict = (
            f"模型声称「{CONCLUSION_CHANGE_LABELS.get(conclusion_change, conclusion_change)}」，"
            "但本轮既无新增数据、也没有任何判断获得支持/削弱证据——修订状态不予采信，按未修订处理。"
        )
        revision = "unrevised"
    if revised and not supporting and (has_refreshed_data or has_supplements):
        conflict = "本轮有新增材料，但没有任何一条原判断被标明受影响，修订依据须回查证据。"
    as_of_note = ""
    if data_basis == "report_only":
        as_of_note = "（不代表已复核最新数据）"
    return {
        "revision_status": revision,
        "revision_status_label": REVISION_LABELS[revision],
        "answerability": answerability,
        "answerability_label": ANSWERABILITY_LABELS[answerability],
        "data_basis": data_basis,
        "data_basis_label": DATA_BASIS_LABELS[data_basis],
        "status_conflict": conflict,
        "state_line": f"{DATA_BASIS_LABELS[data_basis]}{as_of_note}",
    }


def status_view(turn: dict[str, Any]) -> dict[str, Any]:
    """旧追问记录的状态回显（Q03 验收「历史记录可兼容显示」）。

    1.1 版契约没有三条轴，这里按当时可得的信息**确定性补全**：无补充材料即
    `report_only`，修订轴由 `conclusion_change` 推导，可回答程度一律标 `unknown`
    （不替老记录宣称「已回答」）。
    """
    if not isinstance(turn, dict):
        return {}
    if str(turn.get("revision_status") or "").strip():
        return {
            "revision_status": turn.get("revision_status"),
            "revision_status_label": turn.get("revision_status_label") or REVISION_LABELS.get(
                str(turn.get("revision_status")), ""
            ),
            "answerability": turn.get("answerability") or "",
            "answerability_label": turn.get("answerability_label")
            or ANSWERABILITY_LABELS.get(str(turn.get("answerability")), ""),
            "data_basis": turn.get("data_basis") or "report_only",
            "data_basis_label": turn.get("data_basis_label")
            or DATA_BASIS_LABELS.get(str(turn.get("data_basis") or "report_only"), ""),
            "status_conflict": turn.get("status_conflict") or "",
            "state_line": turn.get("state_line")
            or DATA_BASIS_LABELS.get(str(turn.get("data_basis") or "report_only"), ""),
            "legacy": False,
        }
    change = str(turn.get("conclusion_change") or "").strip().lower()
    has_supps = bool(turn.get("supplement_evidence"))
    axes = status_axes(
        conclusion_change=change if change in CONCLUSION_CHANGES else "undetermined",
        answerability="answered" if str(turn.get("answer") or "").strip() else "not_answerable",
        affected=[],
        has_supplements=has_supps,
        has_refreshed_data=False,
    )
    axes["answerability"] = ""
    axes["answerability_label"] = "旧版记录未评估（1.1 契约无该字段）"
    axes["legacy"] = True
    return axes


def _snapshot_block(base_report: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """行情快照块（Q06）：把原报告的价位/均线连同**数据截至日**一起给模型，堵住「9 月 MA20 当 11 月阈值」。"""
    source_asof = base_report.get("source_asof") if isinstance(base_report.get("source_asof"), dict) else {}
    data_as_of = str(
        base_report.get("data_as_of")
        or base_report.get("market_as_of")
        or source_asof.get("S1")
        or str(base_report.get("generated_at") or base_report.get("created_at") or "")[:10]
        or base_report.get("generated_on")
        or ""
    ).strip()[:10]
    levels = base_report.get("levels") if isinstance(base_report.get("levels"), dict) else {}
    rows: list[dict[str, Any]] = []
    for group in ("support", "resistance"):
        for row in levels.get(group) or []:
            if isinstance(row, dict):
                rows.append({**row, "kind": group, "as_of": str(row.get("as_of") or data_as_of or "")[:10]})
    lines = [
        f"- {row.get('price')}（{row.get('kind') or '—'}，依据 {row.get('basis') or '—'}，"
        f"截至 {row.get('as_of') or '未标注'}）"
        for row in rows[:8]
    ]
    conclusion = base_report.get("conclusion") if isinstance(base_report.get("conclusion"), dict) else {}
    price = base_report.get("latest_price") or conclusion.get("price")
    if price is not None:
        lines.insert(0, f"- 现价 {price}（行情截至 {data_as_of or '未标注'}）")
    block = "\n".join(lines) or "- （原报告未给出结构化价位）"
    return block, data_as_of, rows


def _claims_lines(claims: list[dict[str, Any]], limit: int = 8) -> tuple[list[str], dict[str, str]]:
    lines: list[str] = []
    names: dict[str, str] = {}
    for item in claims[:limit]:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id", "")).strip()
        text = str(item.get("text", "")).strip()
        if claim_id:
            names[claim_id] = text
        lines.append(
            f"- [{claim_id}] {text}（引 {'/'.join(item.get('sources') or [])}，"
            f"支撑 {item.get('support') or '未判定'}）"
        )
    return lines, names


def followup_messages(
    *,
    base_report: dict[str, Any],
    question: str,
    supplements: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    mode: str = MODE_INTERPRET,
    new_data: list[dict[str, Any]] | None = None,
    prior_turns: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """构造追问分析的 (system, user) 消息对。

    原报告侧只送**结构化骨架**（结论/claims/估值/情景/验证点 + 执行摘要 + 行情快照时点），
    不整篇重灌，保证追问围绕「判断与证据」而不是复述正文。

    - `mode`（Q10）：`interpret` 只解读，`supplement_research` 才允许使用 `new_data`；
    - `new_data`（Q11）：本轮系统新获取的数据块，逐条带来源与截至日期，失败项如实标出；
    - `prior_turns`（Q14）：同一报告的前几轮问答摘要，只带问题/直接回答/结论与证据编号，
      用于「那等突破呢」这类承接，控制上下文大小。
    """
    symbol = str(base_report.get("symbol") or base_report.get("subject") or "")
    title = str(base_report.get("title") or "")
    conclusion = base_report.get("conclusion") if isinstance(base_report.get("conclusion"), dict) else {}
    claims = base_report.get("claims") if isinstance(base_report.get("claims"), list) else []
    if not claims:
        findings = base_report.get("claim_findings")
        claims = findings.get("claims") if isinstance(findings, dict) else []
    claim_lines, _ = _claims_lines(claims)
    valuation = base_report.get("valuation") if isinstance(base_report.get("valuation"), dict) else {}
    view = valuation_view(valuation)
    valuation_brief = view["display"]
    scenarios = base_report.get("scenarios") if isinstance(base_report.get("scenarios"), list) else []
    snapshot_block, data_as_of, _ = _snapshot_block(base_report)
    # Q08：提示词里的情景区间与页面上是同一份视图——历史高点没推导就标成参考位，别让它当目标价。
    bounds_of = {
        str(row.get("name") or ""): {str(b.get("side")): b for b in row.get("bounds") or []}
        for row in report_quality.scenario_probability_view(
            scenarios, levels=base_report.get("levels")
        )["scenarios"]
    }

    def _bound(key: str, side: str) -> str:
        info = bounds_of.get(key) or {}
        bound = info.get(side) or {}
        if bound.get("kind") == "reference" and "未给出推导" in str(bound.get("note")):
            return "（参考位，非目标价）"
        return ""

    scenario_lines = []
    for item in scenarios[:3]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "情景")
        rng = item.get("expected_range") if isinstance(item.get("expected_range"), dict) else {}
        span = "—"
        if rng.get("low") is not None or rng.get("high") is not None:
            span = f"{rng.get('low') if rng.get('low') is not None else '?'} ~ {rng.get('high') if rng.get('high') is not None else '?'}"
        level = str(item.get("probability_level") or "").strip()
        scenario_lines.append(
            f"- {name}：触发 {item.get('trigger') or '—'}；失效 {item.get('invalidates') or '—'}"
            f"；预期区间 {span}（{item.get('range_status_label') or '未标注'}）{_bound(name, 'high')}"
            f"；倾向 {level or '未给出'}（未统计校准，不是上涨概率）；窗口 {item.get('horizon') or '—'}"
        )
    scenario_lines = scenario_lines or ["- （无）"]
    watchpoints = base_report.get("watchpoints") if isinstance(base_report.get("watchpoints"), list) else []
    watch_lines = [
        f"- {item.get('signal') or '—'}（验证时点 {item.get('verify_by') or '—'}"
        f"{'，' + str(item.get('date_note')) if item.get('date_note') else ''}）"
        for item in watchpoints[:5]
        if isinstance(item, dict)
    ] or ["- （无）"]

    if supplements:
        supplement_blocks = []
        for supp, fact in zip(supplements, facts):
            supplement_blocks.append(
                f"{supp.get('supplement_id')}【{supp.get('trust_label')}】"
                f"来源：{supp.get('source_name') or '（未填）'}"
                f"{(' · URL：' + str(supp.get('url'))) if supp.get('url') else ''}"
                f" · 事件日期：{supp.get('event_date') or '（未填）'}\n"
                f"原文：{supp.get('text')}\n"
                f"事实归一化：主体={fact.get('subject')}；日期={fact.get('date')}"
                f"{'（为录入时间，非事件日期）' if fact.get('date_is_input_time') else ''}；"
                f"原文{'含' if fact.get('complete') else '缺'}数字细节；"
                f"来源可信度={fact.get('source_credibility')}；与标的的关系={fact.get('relation')}"
            )
        supplement_text = "\n\n".join(supplement_blocks)
    else:
        supplement_text = "（本次无补充材料——仅围绕原报告自身的证据与逻辑追问）"

    new_data_text = ""
    if mode == MODE_RESEARCH:
        rows = [row for row in (new_data or []) if isinstance(row, dict)]
        ok_rows = [row for row in rows if row.get("ok")]
        failed = [row for row in rows if not row.get("ok")]
        blocks = [
            f"- {row.get('label') or row.get('kind')}：{str(row.get('summary') or '')[:300]}"
            f"（来源 {row.get('source_name') or '—'}｜数据截至 {row.get('as_of') or '—'}"
            f"｜获取于 {row.get('retrieved_at') or '—'}）"
            for row in ok_rows
        ]
        blocks += [
            f"- {row.get('label') or row.get('kind')}：**获取失败**（{row.get('error') or '原因未记录'}）"
            for row in failed
        ]
        new_data_text = (
            "\n【本次新增数据（补充研究模式，系统实时获取）】\n"
            + (f"成功 {len(ok_rows)} 项 / 失败 {len(failed)} 项" if rows else "本轮未取到任何新数据")
            + "\n"
            + ("\n".join(blocks) or "- （无）")
            + "\n新增数据只覆盖上面列出的项：没取到的必须承认缺口，不得当作已知事实。\n"
        )

    prior_text = ""
    if prior_turns:
        lines = []
        for row in prior_turns[-3:]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- 问：{str(row.get('question') or '')[:80]}\n"
                f"  答：{str(row.get('direct_answer') or row.get('answer') or '')[:160]}\n"
                f"  当时依据：{row.get('data_basis_label') or '未标注'}"
                f"（附录 #{row.get('turn_index') or '?'}）"
            )
        prior_text = "\n【同一报告的历史追问（承接用，不重复回答）】\n" + "\n".join(lines) + "\n"

    mode_note = MODE_LABELS.get(mode, MODE_LABELS[MODE_INTERPRET])
    user = (
        f"【原报告（不可变，只可引用不可改写）】{symbol} · {title}\n"
        f"本轮口径：{mode_note}\n"
        f"执行摘要：{str(base_report.get('executive_summary') or '') or '（无）'}\n"
        f"一句话结论：{conclusion.get('statement') or '（无）'}（方向 {conclusion.get('direction') or '—'}，"
        f"核心矛盾 {conclusion.get('core_conflict') or '—'}）\n"
        f"核心判断清单：\n" + ("\n".join(claim_lines) or "- （无）") + "\n"
        f"估值：{valuation_brief}\n"
        f"情景：\n" + "\n".join(scenario_lines) + "\n"
        "验证点：\n" + "\n".join(watch_lines) + "\n"
        f"行情快照时点（所有价位只能用这里的截至日表达）：数据截至 {data_as_of or '未标注'}\n"
        + snapshot_block + "\n"
        + prior_text
        + new_data_text
        + f"\n【用户问题】{question}\n\n"
        f"【补充材料（全部默认未独立验证）】\n{supplement_text}\n\n"
        "请输出严格 JSON（无 markdown 围栏）：\n"
        + _ANSWER_CONTRACT + "\n"
        + _ANSWER_RULES
    )
    return (_RESEARCH_SYSTEM if mode == MODE_RESEARCH else _SUPPLEMENT_SYSTEM), user


def direction_followup_messages(
    *,
    base_report: dict[str, Any],
    question: str,
    supplements: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    mode: str = MODE_INTERPRET,
    new_data: list[dict[str, Any]] | None = None,
    prior_turns: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """构造**方向研判**追问分析的 (system, user) 消息对。

    与 `followup_messages`（个股研报）的差异仅在骨架装配：方向研判没有 `claims/valuation/
    scenarios`，对应结构是 `core_judgments`（分层判断清单，编号 J#）、`board_judgments`
    （板块四维分类）、`next_verification`（下一验证动作）与 `data_gaps`（数据缺口）。
    归一化与闸门完全复用 `normalize_followup`——`allowed_claim_ids` 传方向判断编号集合即可，
    不新增任何一套契约。
    """
    subject = str(base_report.get("subject") or base_report.get("symbol") or "")
    title = str(base_report.get("title") or "")
    topic = str(base_report.get("topic") or subject or "")
    judgments = base_report.get("core_judgments")
    if not isinstance(judgments, list):
        judgments = []
    judgment_lines = []
    judgment_names: dict[str, str] = {}
    for item in judgments[:10]:
        if not isinstance(item, dict):
            continue
        refs = "/".join(item.get("support_refs") or []) or "无引用"
        conf = item.get("confidence") or "未标注"
        missing = "；".join(item.get("missing") or []) or "—"
        judgment_id = str(item.get("id", "")).strip()
        if judgment_id:
            judgment_names[judgment_id] = str(item.get("text") or "").strip()
        judgment_lines.append(
            f"- [{judgment_id}]（{item.get('kind_label') or item.get('kind') or '未分类'} · "
            f"置信 {conf}）{item.get('text')}（引 {refs}；缺口 {missing}）"
        )
    board_judgments = base_report.get("board_judgments")
    if isinstance(board_judgments, dict) and board_judgments:
        board_lines = [
            f"- {board}：{label}"
            for board, label in list(board_judgments.items())[:12]
            if str(label or "").strip()
        ]
    else:
        board_lines = ["- （本期无板块四维分类——风格/概念类主题未命中行业板块）"]
    gaps = base_report.get("data_gaps")
    gap_lines = [
        f"- {str(item).strip()}"
        for item in (gaps if isinstance(gaps, list) else [])[:8]
        if str(item).strip()
    ] or ["- （无）"]
    next_verification = str(base_report.get("next_verification") or "").strip()
    executive = str(base_report.get("executive_summary") or "").strip()
    research_mode = str(base_report.get("research_mode") or "")
    mode_label = str(base_report.get("mode_label") or base_report.get("mode") or "")
    evidence_rows = [row for row in (base_report.get("evidence_snapshot") or []) if isinstance(row, dict)]
    evidence_lines = [
        f"- {row.get('label') or row.get('kind') or '证据'}：{str(row.get('summary') or '')[:160]}"
        f"（截至 {row.get('trade_date') or row.get('retrieved_at') or '—'}）"
        for row in evidence_rows[:6]
    ]

    if supplements:
        supplement_blocks = []
        for supp, fact in zip(supplements, facts):
            supplement_blocks.append(
                f"{supp.get('supplement_id')}【{supp.get('trust_label')}】"
                f"来源：{supp.get('source_name') or '（未填）'}"
                f"{(' · URL：' + str(supp.get('url'))) if supp.get('url') else ''}"
                f" · 事件日期：{supp.get('event_date') or '（未填）'}\n"
                f"原文：{supp.get('text')}\n"
                f"事实归一化：主体={fact.get('subject')}；日期={fact.get('date')}"
                f"{'（为录入时间，非事件日期）' if fact.get('date_is_input_time') else ''}；"
                f"原文{'含' if fact.get('complete') else '缺'}数字细节；"
                f"来源可信度={fact.get('source_credibility')}；与主题的关系={fact.get('relation')}"
            )
        supplement_text = "\n\n".join(supplement_blocks)
    else:
        supplement_text = "（本次无补充材料——仅围绕原报告自身的证据与逻辑追问）"

    new_data_text = ""
    if mode == MODE_RESEARCH:
        rows = [row for row in (new_data or []) if isinstance(row, dict)]
        ok_rows = [row for row in rows if row.get("ok")]
        failed = [row for row in rows if not row.get("ok")]
        blocks = [
            f"- {row.get('label') or row.get('kind')}：{str(row.get('summary') or '')[:300]}"
            f"（来源 {row.get('source_name') or '—'}｜数据截至 {row.get('as_of') or '—'}）"
            for row in ok_rows
        ] + [
            f"- {row.get('label') or row.get('kind')}：**获取失败**（{row.get('error') or '原因未记录'}）"
            for row in failed
        ]
        new_data_text = (
            "\n【本次新增数据（补充研究模式，系统实时获取）】\n"
            + ("\n".join(blocks) or "- 本轮未取到任何新数据")
            + "\n新增数据只覆盖上面列出的项：没取到的必须承认缺口。\n"
        )

    prior_text = ""
    if prior_turns:
        lines = [
            f"- 问：{str(row.get('question') or '')[:80]}\n"
            f"  答：{str(row.get('direct_answer') or row.get('answer') or '')[:160]}"
            for row in prior_turns[-3:]
            if isinstance(row, dict)
        ]
        prior_text = "\n【同一方向研判的历史追问（承接用）】\n" + "\n".join(lines) + "\n"

    user = (
        f"【原方向研判报告（不可变，只可引用不可改写）】主题：{topic}\n"
        f"标题：{title}\n"
        f"本轮口径：{MODE_LABELS.get(mode, MODE_LABELS[MODE_INTERPRET])}\n"
        f"输出口径：{research_mode or '（未标注）'} · 模式 {mode_label or '（未标注）'} · "
        f"证据条目 {len(evidence_rows)} 条\n"
        + ("\n".join(evidence_lines) + "\n" if evidence_lines else "")
        + f"执行摘要：{executive or '（无）'}\n"
        f"核心判断清单（编号 J#）：\n" + ("\n".join(judgment_lines) or "- （无）") + "\n"
        "板块四维分类（系统参考分类，模型可推翻）：\n" + "\n".join(board_lines) + "\n"
        f"下一验证动作：{next_verification or '（无）'}\n"
        f"数据缺口：\n" + "\n".join(gap_lines) + "\n" + prior_text + new_data_text + "\n"
        f"【用户问题】{question}\n\n"
        f"【补充材料（全部默认未独立验证）】\n{supplement_text}\n\n"
        "请输出严格 JSON（无 markdown 围栏）：\n"
        + _ANSWER_CONTRACT.replace('"role": "close|ma|support|resistance|other"',
                                   '"role": "index|sector|other"')
                          .replace('{"claim_id": "C1"', '{"claim_id": "J1"')
        + "\n"
        + _ANSWER_RULES.replace("核心判断清单里已有的编号；清单没有的不许编；",
                                "核心判断清单里已有的编号（J 开头）；清单没有的不许编；")
        + "10. 方向研判的宏观前提（如「美国加息后」）若在本次新增数据或补充材料中被证实或证伪，"
        "必须显式落到 affected_claims 的对应判断上，不得只在 answer 里口头提及。\n"
        "11. 方向研判没有个股均线口径，price_refs 只填指数/板块的相对位置，给不出就留空。"
    )
    return (_RESEARCH_SYSTEM if mode == MODE_RESEARCH else _SUPPLEMENT_SYSTEM), user


def normalize_followup(
    parsed: Any,
    *,
    allowed_claim_ids: set[str],
    claims_by_id: dict[str, str] | None = None,
    supplements: list[dict[str, Any]] | None = None,
    new_data: list[dict[str, Any]] | None = None,
    base_report: dict[str, Any] | None = None,
    calendar: trading_calendar.TradingCalendar | None = None,
    today: str = "",
    catalyst: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """归一化追问输出（§3.3 契约 + 确定性闸门）。

    - `answer` 缺失即视为无效（追问的全部价值在回答正文）→ 返回 None，调用方按
      「本次追问未能完成」如实标注，不编造；
    - `affected_claims` 只保留原报告已有的 claim_id；模型自造编号剔除进 `dropped_claim_ids`；
      effect 越界归 cannot_judge（原值留痕在 `effect_raw`）；
    - `conclusion_change` 越界归 undetermined（原值留痕）；
    - `new_watchpoints` 复用研报验证点归一（due_on/状态口径与原报告一致）+ Q07 日期闸门；
    - 新增：三条状态轴（Q03）、判断分组（Q04）、估值三态（Q05）、价位时点（Q06）、
      参考位与未校准概率（Q08）。
    """
    if not isinstance(parsed, dict):
        return None
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        return None

    dropped_claim_ids: list[str] = []
    affected: list[dict[str, Any]] = []
    raw_claims = parsed.get("affected_claims")
    if isinstance(raw_claims, list):
        for item in raw_claims[:8]:
            if not isinstance(item, dict):
                continue
            claim_id = str(item.get("claim_id", "")).strip()
            if not claim_id:
                continue
            if allowed_claim_ids and claim_id not in allowed_claim_ids:
                dropped_claim_ids.append(claim_id)
                continue
            effect_raw = str(item.get("effect", "")).strip().lower()
            affected.append({
                "claim_id": claim_id,
                "effect": effect_raw if effect_raw in EFFECT_VOCAB else "cannot_judge",
                "effect_raw": effect_raw,
                "reason": str(item.get("reason", "")).strip()[:160],
            })

    change_raw = str(parsed.get("conclusion_change", "")).strip().lower()
    conclusion_change = change_raw if change_raw in CONCLUSION_CHANGES else "undetermined"
    scenario_changes: list[dict[str, str]] = []
    raw_scenarios = parsed.get("scenario_changes")
    if isinstance(raw_scenarios, list):
        for item in raw_scenarios[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()[:80]
            change = str(item.get("change", "")).strip()[:160]
            if name or change:
                scenario_changes.append({"name": name, "change": change})

    limitations = [str(item).strip()[:160] for item in parsed.get("limitations", []) or [] if str(item).strip()][:3]

    # —— Q02：直接回答与关键条件/补证缺口（缺失时可从 answer 首段确定性兜底）——
    direct_answer = str(parsed.get("direct_answer") or "").strip()[:400]
    if not direct_answer:
        first_paragraph = next(
            (line.strip() for line in answer.splitlines() if line.strip() and not line.startswith("#")),
            "",
        )
        direct_answer = first_paragraph[:160]
        if direct_answer:
            direct_answer += "（摘自回答首段：模型未按 1.2 契约单列直接回答）"
    answerability_raw = str(parsed.get("answerability", "")).strip().lower()
    answerability = answerability_raw if answerability_raw in ANSWERABILITY_VOCAB else "partially_answered"
    key_conditions = [
        str(item).strip()[:200] for item in (parsed.get("key_conditions") or []) if str(item).strip()
    ][:3]
    evidence_gaps = [
        str(item).strip()[:200] for item in (parsed.get("evidence_gaps") or []) if str(item).strip()
    ][:3]

    # —— Q03：三条状态轴（先定数据轴，再据它分组判断，避免用错口径）——
    named_by_id = dict(claims_by_id or {})
    if not named_by_id and isinstance(base_report, dict):
        _, named_by_id = _claims_from_report(base_report)
    has_refreshed = any(bool(row.get("ok")) for row in (new_data or []) if isinstance(row, dict))
    axes = status_axes(
        conclusion_change=conclusion_change,
        answerability=answerability,
        affected=affected,
        has_supplements=bool(supplements),
        has_refreshed_data=has_refreshed,
    )
    grouped = group_claims(affected, named_by_id, data_basis=axes["data_basis"])

    # —— Q06/Q07：验证点日期与价位时点口径 ——
    # `normalize_watchpoints` 只保留研报口径字段，日期依据（date_basis/event_anchor）在原始项里，
    # 因此闸门必须读**归一前的原项**——否则模型给出的依据会被丢掉，有依据的日期也被降级。
    raw_points = [item for item in (parsed.get("new_watchpoints") or []) if isinstance(item, dict)][:5]
    watchpoints = report_quality.normalize_watchpoints(parsed.get("new_watchpoints"))
    base_valuation = base_report.get("valuation") if isinstance(base_report, dict) else {}
    data_as_of = ""
    snapshot_levels: list[dict[str, Any]] = []
    if isinstance(base_report, dict):
        _, data_as_of, snapshot_levels = _snapshot_block(base_report)
    for row, raw in zip(watchpoints, raw_points):
        gated = gate_verify_by({**raw, **row}, calendar=calendar, today=today)
        row.update(gated)
        # 降级/无日期一律清空 due_on：待复盘队列（G01）按 due_on 落库，留着日期等于让凭空日期复活。
        row["due_on"] = (
            "" if gated.get("date_kind") in (trading_calendar.DATE_KIND_DEMOTED, trading_calendar.DATE_KIND_UNKNOWN)
            else report_quality.extract_due_date(str(row.get("verify_by") or ""))
        )
        row["assumptions"] = split_volume_price_terms(
            f"{row.get('signal', '')} {row.get('expected_if_true', '')}"
        )
    review_watchpoint_consistency(watchpoints)

    price_refs = normalize_price_refs(
        parsed.get("price_refs"),
        snapshot_levels=snapshot_levels,
        data_as_of=data_as_of,
        calendar=calendar,
        refreshed=axes["data_basis"] == "refreshed_data",
    )

    # —— Q05/Q08：估值三态与未校准概率/参考位 ——
    # 方向研判没有 `valuation/scenarios` 结构：这里给 None 而不是给一个「估值指标缺失」的空壳，
    # 否则方向追问会出现「本次估值口径：估值指标缺失」这种把「不适用」说成「没取到」的噪声。
    base_valuation_block = base_valuation if isinstance(base_valuation, dict) and base_valuation else None
    valuation = valuation_view(base_valuation_block) if base_valuation_block else None
    base_scenarios = base_report.get("scenarios") if isinstance(base_report, dict) else None
    probability_view = (
        report_quality.scenario_probability_view(
            base_scenarios, levels=base_report.get("levels") if isinstance(base_report, dict) else None
        )
        if isinstance(base_scenarios, list) and base_scenarios else None
    )
    # —— Q13：证据链越界断言的如实标注（不改写正文，只把越过缺口的话说清楚）——
    chain = catalyst if isinstance(catalyst, dict) else {}
    answer_flags = followup_research.catalyst_chain_overreach_flags(answer, chain) if chain else []

    return {
        "affected_claims": affected,
        "dropped_claim_ids": dropped_claim_ids,
        "conclusion_change": conclusion_change,
        "conclusion_change_raw": change_raw,
        "scenario_changes": scenario_changes,
        "new_watchpoints": watchpoints,
        "answer": answer,
        "limitations": limitations,
        "prompt_version": FOLLOWUP_PROMPT_VERSION,
        # —— 1.2 契约新增字段 ——
        "direct_answer": direct_answer,
        "answerability": axes["answerability"],
        "answerability_label": axes["answerability_label"],
        "key_conditions": key_conditions,
        "evidence_gaps": evidence_gaps,
        "revision_status": axes["revision_status"],
        "revision_status_label": axes["revision_status_label"],
        "data_basis": axes["data_basis"],
        "data_basis_label": axes["data_basis_label"],
        "state_line": axes["state_line"],
        "status_conflict": axes["status_conflict"],
        "expanded_claims": grouped["expanded_claims"],
        "unresolved_claims": grouped["unresolved_claims"],
        "shared_limitation": grouped["shared_limitation"],
        "valuation_view": valuation,
        "probability_view": probability_view,
        "price_refs": price_refs,
        "data_as_of": data_as_of,
        "catalyst_chain": chain,
        "answer_flags": answer_flags,
    }


def _claims_from_report(base_report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """从原报告取 claim 清单与编号→名称映射（展示时「编号配合判断名称」）。"""
    claims = base_report.get("claims") if isinstance(base_report.get("claims"), list) else []
    if not claims:
        findings = base_report.get("claim_findings")
        claims = findings.get("claims") if isinstance(findings, dict) else []
    names: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for item in claims if isinstance(claims, list) else []:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id", "")).strip()
        text = str(item.get("text", "")).strip()
        if claim_id:
            names[claim_id] = text
        rows.append(item)
    return rows, names


def source_catalog(sources: Any) -> list[dict[str, Any]]:
    """来源目录（Q09）：编号、名称、可用原始链接、数据日期、获取时间与口径限制。

    缺字段一律留空并由调用方如实展示「未提供」，**绝不伪造链接**；`cited` 用于区分
    「引用过」与「可得未引用」，`used_by` 回指引用它的判断编号，供「2/3」这类统计回溯。
    """
    rows = sources if isinstance(sources, list) else []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows[:12]):
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or item.get("id") or f"S{index + 1}").strip()[:12]
        url = str(item.get("url") or item.get("source_uri") or item.get("link") or "").strip()
        out.append({
            "source_id": source_id,
            "name": str(item.get("name") or item.get("title") or item.get("source_name") or "").strip()[:120],
            "url": url[:500],
            "url_note": "" if url else "来源未提供可访问链接（如实标注，不猜测）",
            "data_date": str(item.get("data_date") or item.get("trade_date") or item.get("published_at") or "")[:10],
            "retrieved_at": str(item.get("retrieved_at") or item.get("fetched_at") or "")[:32],
            "quality": str(item.get("quality") or item.get("evidence_quality") or "").strip()[:4],
            "scope_note": str(item.get("scope_note") or item.get("note") or "").strip()[:200],
            "cited": bool(item.get("cited", True)),
            "used_by": [str(x).strip()[:12] for x in (item.get("used_by") or []) if str(x).strip()][:12],
        })
    return out
