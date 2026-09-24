"""研报质量闸门（v24 起；v27 增证据质量分级与 claim→source 覆盖检查）。

背景（2026-09-10 / 2026-09-12 两轮用户视角分析发现）：
- v23 的个股研报只校验 `citations` 是**非空列表**，不校验引用 id 是否属于本次真实取到的来源。
  新闻/公告/财报取数失败时对应 S 编号并不存在，模型仍可产出指向缺失来源的假 `[S#]` 引用并落库；
- 正文长度、必需小节、`confidence` 取值一律无校验，半成品会被当成品交付；
- v24 之后引用「真实存在」已能保证，但**引用是否支撑得住那条判断**仍未校验：
  拿一条新闻稿去支撑「主业减亏」在看板上与拿财报支撑长得一模一样。

本模块把这三件事收敛成同一口径，供两条链路共用：
- `app.py` 个股研报端点（`/evidence/stock-research-report`）；
- `collab.py` 协同流水线（draft / revise 阶段）。

设计铁律：
- **引用真实性是硬校验**：不属于真实来源的引用一律剔除并如实记录；剔除后为空 → 调用方按
  「无引用不发布」（ADR-0006）处理，绝不放行虚构引用；
- **形式质量是软校验**：长度 / 小节 / confidence / limitations 偏差只产出 `warnings` 如实标注，
  不阻断交付（避免把可用的报告因格式问题丢弃）；
- **claim 覆盖是软校验 + 显式降级**：判断所需的覆盖字段若不被所引来源覆盖，或只有 C/D 级证据，
  则该判断被如实标注为「未独立验证」并降级，**不阻断交付、也不改写模型原话**；
- 结论不写回模型产出本身：本模块只返回校验结果，由调用方决定如何使用，不伪造、不改写事实。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from investment_steward_core import jev_client

# 技术面（S1）由 K 线快照构成，始终存在；S2/S3/S4/S5 分别对应新闻/公告/财务/估值，可能取数失败。
#
# S6（P2 席位证据 / 龙虎榜）与它们有一个**本质差别**：它不是「取数失败」，而是
# 「该票没上榜就压根不该存在」。因此它必须可缺省，且缺省**不得**被当作缺口告警——
# 否则每只没上榜的股票都会背一条假缺口。把它放进 OPTIONAL_SOURCE_IDS 是必要的，
# 但读取方必须按「未上榜 = 正常」处理，不能按「缺失 = 有问题」处理。
TECH_SOURCE_ID = "S1"
SEAT_SOURCE_ID = "S6"
OPTIONAL_SOURCE_IDS = ("S2", "S3", "S4", "S5", SEAT_SOURCE_ID)

#: S6 只在确有上榜记录时出现，缺省即正常；质量闸门不得据此报「来源缺失」。
CONDITIONAL_SOURCE_IDS = (SEAT_SOURCE_ID,)

# 形式质量口径（与提示词中的要求一致，仅作告警）。
# 2026-09-12 方案 §2 下调口径：正文 1,500～2,500 去空白字符、摘要 ≤180 字、局限 3～5 条。
MIN_REPORT_CHARS = 1500
MAX_REPORT_CHARS = 2500
MAX_SUMMARY_CHARS = 180
MIN_LIMITATIONS = 3
MAX_LIMITATIONS = 5

# ---------------------------------------------------------------------------
# v29（三次升级方案 §二/§十）：字数从单一上限改为**按模式分层预算**，且全部为
# **软告警**——不以字数不足或超出单独阻断发布；硬阻断只保留给「缺引用/缺必需小节」。
#   快速版 1500～2500 / 标准版 2000～3500 / 深研版 3500～6000（数据附录不计正文预算）。
# 边界（§十「全文审查不能被截断」）：深研版上限 6000 与 collab.py 的
# `_REPORT_BODY_MAX=6000` 审查输入截断线重合，故**深研模式本期只接入单股研报端点**，
# 协同流水线固定 standard；分章审查落地前不放宽 collab 的截断口径。
# ---------------------------------------------------------------------------
REPORT_MODE_VERSION = "v1"
REPORT_MODES: dict[str, dict[str, Any]] = {
    "quick": {"label": "快速版", "min": 1500, "max": 2500},
    "standard": {"label": "标准版", "min": 2000, "max": 3500},
    "deep": {"label": "深研版", "min": 3500, "max": 6000},
}
DEFAULT_REPORT_MODE = "standard"


def report_mode_budget(mode: str | None) -> tuple[str, str, int, int]:
    """模式 → (归一化模式名, 中文标签, 字数下限, 字数上限)；未知模式报错，不静默回落。"""
    normalized = str(mode or DEFAULT_REPORT_MODE).strip().lower()
    entry = REPORT_MODES.get(normalized)
    if entry is None:
        raise ValueError(f"未知研报模式：{mode or '（空）'}（支持：{'/'.join(REPORT_MODES)}）")
    return normalized, str(entry["label"]), int(entry["min"]), int(entry["max"])
REQUIRED_SECTIONS = ("技术面", "消息面", "基本面", "综合判断")
ALLOWED_CONFIDENCE = ("low", "medium", "high")

_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")

# v2.2「验证时点」粒度判定（2026-09-10 用户反馈：5 条全是「2026-10 前」= 无法据以跟踪）。
# 含「年+月」但不含具体日 → 视为粒度不足；锚定事件的表述（如「三季报披露后」）不含年+月，不误伤。
_YEAR_MONTH = re.compile(r"(?:19|20)\d{2}\s*[-/年]\s*\d{1,2}")
# D03 补充（2026-09-15 真实 G03 重跑暴露）：**不带年份的裸月份**（如「9月生猪销售简报披露后」）
# 同样定不出到期日。旧口径只认「4 位年份 + 月」，这类写法既不告警也不补窗口，等于从闸门漏过。
# 前置否定断言排除「20月均线」「120月均线」这类技术指标写法被误判为验证时点。
_BARE_MONTH = re.compile(r"(?<![\d:])(?:1[0-2]|[1-9])\s*月(?!\s*均线)")
_HAS_DAY = re.compile(r"\d{1,2}\s*[日号]|(?:19|20)\d{2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}")


def month_only_verify_by(value: str) -> bool:
    """验证时点是否「只写到月份」——笼统到无法据此安排跟踪（如「2026-10 前」「9月…披露后」）。

    D03（M3）：带**到期窗口**的锚定写法（「…简报披露后 5 个交易日内」）不算粒度不足——
    它已可据以安排跟踪，只是到期由事件触发（`due_on` 仍留空，服务端不猜日期）。
    """
    text = value or ""
    if anchor_window_verify_by(text):
        return False
    if _HAS_DAY.search(text):
        return False
    return bool(_YEAR_MONTH.search(text) or _BARE_MONTH.search(text))


# v27（方案 §3.5「验证点必须绑定日期或明确事件」）：相对时间词既不是日期、也不是可触发的事件，
# 因此永远进不了待复盘队列。这类写法只告警、不改写模型原话（与全站软校验口径一致）。
_VAGUE_VERIFY_BY = re.compile(
    r"下月|下个月|下季度|下个季度|本季度末?后|后续|未来|近期|择机|待定|稍后|不晚于|合适(?:的)?(?:时机|时候)|另行(?:通知|安排)"
)


def vague_verify_by(value: str) -> bool:
    """验证时点是否用了**相对时间词**（如「下月」「后续」「择机」）。

    这类写法两头不靠：解析不出确切日期（`due_on` 为空），又不像「三季报披露后」那样有明确的
    事件触发点，因此无法进入待复盘队列。判定为软校验：只告警，保留原话。
    """
    return bool(_VAGUE_VERIFY_BY.search(value or ""))


def available_citations(source_keys: Iterable[str], *, tech_available: bool) -> set[str]:
    """本次**真实取到**的来源 id 集合（技术面存在时含 S1）。

    source_keys 为证据包中实际成功的来源键（如 {"S2","S3","S4"}）；取数失败的来源不在其中，
    因此其编号不可被引用——这是「假引用」的唯一判据。
    """
    allowed: set[str] = {str(key) for key in source_keys}
    if tech_available:
        allowed.add(TECH_SOURCE_ID)
    return allowed


def filter_citations(citations: Iterable[Any], allowed: set[str]) -> tuple[list[str], list[str]]:
    """返回（保留的真实引用, 剔除的不存在引用），保持原顺序并去重。"""
    kept: list[str] = []
    dropped: list[str] = []
    for item in citations:
        value = str(item).strip()
        if not value:
            continue
        target = kept if value in allowed else dropped
        if value not in target:
            target.append(value)
    return kept, dropped


def _sections(report: str) -> list[str]:
    """从正文中抽取小节标题文本（去掉加粗标记与首尾标点），用于必需小节存在性检查。"""
    found: list[str] = []
    for raw in _HEADING.findall(report or ""):
        name = raw.replace("*", "").strip().strip("：:").strip()
        if name and name not in found:
            found.append(name)
    return found


def report_char_count(report: str) -> int:
    """正文字数（去掉全部空白）；中文场景下等价于「字」的近似口径，可复算。"""
    return len(_WHITESPACE.sub("", report or ""))


# ---------------------------------------------------------------------------
# v31 研报质量恢复（2026-09-13 方案 §2.3/§八.4）：三态质量状态 + 必需小节实质检查。
#
# 口径（§八.4 修正案优先于 §2.1 原建议）：
# - **取消「低于长度阈值即硬失败」**：字数不足/超出只产生软告警（上文 v29 已如此）；
# - 完成状态依据**实质章节、证据与必需字段**判定：缺必需小节、无正文、无真实引用、
#   全部核心判断无来源支撑 → `incomplete`；
# - 四节可包含「有解释的数据不足说明」（如「本期无公告，消息面无数据」），只要标题后
#   有实质文字就算该节存在——不为过检补标题，也不把如实说明误判为缺席；
# - `incomplete` 报告仍可作为**草稿**落库回看（记录缺失项与原因），但前端/统计必须
#   如实标注，不混入正式报告数量（§八.4）。
# ---------------------------------------------------------------------------

QUALITY_STATUS_VERSION = "v1"
QUALITY_STATUS_COMPLETE = "complete"
QUALITY_STATUS_NEEDS_REVIEW = "needs_review"
QUALITY_STATUS_INCOMPLETE = "incomplete"
QUALITY_STATUS_LABELS: dict[str, str] = {
    QUALITY_STATUS_COMPLETE: "完整",
    QUALITY_STATUS_NEEDS_REVIEW: "待复核",
    QUALITY_STATUS_INCOMPLETE: "不完整",
}

# 小节「实质内容」门槛：标题之后到下一标题之前，去空白字符数 ≥ 该值才算「真的写了这一节」。
# 这是**结构性判据**（区分「只有标题」与「有正文」），不是篇幅质量阈值——
# 一句如实的「本期无公告数据，消息面无数据」也远超此门槛。
MIN_SECTION_BODY_CHARS = 20
# 「无正文」判定下限：正文去空白字数低于该值视为模型未返回正文（结构性缺失，非篇幅告警）。
EMPTY_REPORT_CHARS = 50

# 正文/摘要/估值依据里的 [S#] 行内引用（方案 §八.3：引用清单一致性检查的对象）。
_CITE_REF = re.compile(r"\[(S\d+)\]")


def section_bodies(report: str) -> list[dict[str, Any]]:
    """正文的每个小节标题与该节去空白字数（同名小节合并计数；标题行本身不计入）。"""
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    current_name: str | None = None
    for line in (report or "").splitlines():
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading is not None:
            name = heading.group(1).replace("*", "").strip().strip("：:").strip()
            current_name = name or None
            if current_name is not None and current_name not in entries:
                entries[current_name] = {"name": current_name, "chars": 0}
                order.append(current_name)
            continue
        if current_name is not None:
            entries[current_name]["chars"] += len(_WHITESPACE.sub("", stripped))
    return [entries[name] for name in order]


def required_section_state(report: str) -> list[dict[str, Any]]:
    """四大必需小节的实质存在性（§八.4：完成状态依据实质章节判定，不看字数预算）。

    `present` = 标题匹配到该节，且标题之后有 ≥ MIN_SECTION_BODY_CHARS 字实质内容。
    写「本期无公告，消息面无数据」这类如实说明也算存在；只有标题没有正文算缺席。
    """
    bodies = section_bodies(report)
    state: list[dict[str, Any]] = []
    for name in REQUIRED_SECTIONS:
        entry = next((item for item in bodies if name in item["name"]), None)
        chars = int(entry["chars"]) if entry else 0
        state.append({
            "section": name,
            "chars": chars,
            "present": entry is not None and chars >= MIN_SECTION_BODY_CHARS,
        })
    return state


def citation_consistency(payload: dict[str, Any], *, allowed: set[str]) -> dict[str, Any]:
    """正文/摘要/估值依据的行内 [S#] 引用 vs 引用清单 vs 本次真实来源（§八.3）。

    背景：501 字示例的引用清单只有 S1/S2，摘要与估值却引用 S4/S5——S4/S5 属于可用来源，
    因此是**引用清单不一致（漏登记）**，不是虚构引用；虚构（引用了本次不存在的来源）由
    `filter_citations` 对 citations 清单处理，正文行内残留这里如实标注、不改写正文。
    """
    report_text = payload.get("report") if isinstance(payload.get("report"), str) else ""
    summary_text = str(payload.get("executive_summary", "") or "")
    valuation = payload.get("valuation")
    valuation_text = str(valuation.get("basis", "")) + str(valuation.get("peer_position", "")) if isinstance(valuation, dict) else ""
    text_refs: list[str] = sorted(set(_CITE_REF.findall(f"{report_text}\n{summary_text}\n{valuation_text}")))
    declared_raw = payload.get("citations")
    declared = {str(item).strip() for item in declared_raw} if isinstance(declared_raw, list) else set()
    unavailable_used = [ref for ref in text_refs if ref not in allowed]
    undeclared_used = [ref for ref in text_refs if ref in allowed and ref not in declared]
    return {
        "text_refs": text_refs,
        "declared": sorted(declared),
        "undeclared_used": undeclared_used,
        "unavailable_used": unavailable_used,
    }


def quality_status_of(
    *,
    blockers: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    """三态判定（§2.3）：有阻断 → incomplete；仅有软告警 → needs_review；否则 complete。"""
    if blockers:
        return QUALITY_STATUS_INCOMPLETE
    if warnings:
        return QUALITY_STATUS_NEEDS_REVIEW
    return QUALITY_STATUS_COMPLETE


# ---------------------------------------------------------------------------
# v31 §2.4 低成本修复策略（2026-09-13 方案）：按失败类型定向修复，禁止「摘要修复器补全文」。
#   缺摘要 → 只生成摘要；缺一节 → 只重新生成该章节并交一次一致性检查（重跑同一闸门）；
#   缺两节及以上 → 重跑完整 draft；risk/revise 阶段失败 → 保留已完成阶段（collab 既有口径）。
# 每次修复必须升 report_revision，保存原稿、修订原因和结果；修复后仍不完整则如实保留草稿，
# 绝不为过检补标题、重复内容或编造事实（§八.4）。
# ---------------------------------------------------------------------------

REPAIR_STRATEGY_FULL_RERUN = "full_rerun"
REPAIR_STRATEGY_SECTION = "section_only"
REPAIR_STRATEGY_SUMMARY = "summary_only"
REPAIR_STRATEGY_LABELS: dict[str, str] = {
    REPAIR_STRATEGY_FULL_RERUN: "重跑完整初稿",
    REPAIR_STRATEGY_SECTION: "只重新生成缺失小节",
    REPAIR_STRATEGY_SUMMARY: "只生成执行摘要",
}


def repair_strategy_of(
    *,
    missing_sections: list[str],
    summary_missing: bool,
) -> str | None:
    """按失败类型选择修复策略；无需修复（或无需修复的失败类型）返回 None。

    - 只缺摘要 → 只生成摘要；
    - 只缺一节（摘要在）→ 只重新生成该节；
    - 缺两节及以上，或缺一节且同时缺摘要 → 重跑完整 draft（成本低于多次拼接且一致性好）。
    """
    if len(missing_sections) >= 2 or (missing_sections and summary_missing):
        return REPAIR_STRATEGY_FULL_RERUN
    if missing_sections:
        return REPAIR_STRATEGY_SECTION
    if summary_missing:
        return REPAIR_STRATEGY_SUMMARY
    return None


def merge_section(report: str, section: str, content: str) -> str:
    """把重新生成的「section」小节合并回正文：已有同名标题（往往只有标题没内容）时**替换**该节，
    否则追加到正文末尾；content 为空原样返回（不产出空节）。"""
    content = (content or "").strip()
    if not content:
        return report or ""
    lines = (report or "").splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading is not None and section in heading.group(1).replace("*", ""):
            start = index
            break
    if start is None:
        return (report.rstrip() + "\n\n" + content).strip()
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _HEADING.match(lines[index].strip()) is not None:
            end = index
            break
    return "\n".join(lines[:start] + content.splitlines() + lines[end:]).strip()


def validate_report(
    payload: dict[str, Any],
    *,
    allowed: set[str],
    source_asof: dict[str, str] | None = None,
    today: date | None = None,
    mode: str | None = None,
    source_texts: Mapping[str, str] | None = None,
    jev_judge: JevJudge | None = None,
    jev_note: str = "",
) -> dict[str, Any]:
    """校验模型产出：引用真实性（硬）+ 形式质量（软）+ 证据支撑链（v28 软校验 + 显式降级）。

    `source_asof` / `today` 为可选：两者都给出时才做「来源数据是否过期」判定；
    缺任一时**不做该项判定**并在结果里标注 `staleness_checked=False`（不拿未知当通过）。
    两者都不涉及模型调用，`today` 由调用方按站点既有口径（`datetime.now().astimezone()`）
    传入，保证本模块保持纯函数、不读系统时间。

    `mode`（v29）：字数预算模式（quick/standard/deep），只影响**软告警**的阈值与文案；
    未知模式直接抛 ValueError（不静默回落，避免「以为在深研、实际按标准判」）。

    `source_texts` + `jev_judge`（JV04）：**可选**注入的语义支撑层。`source_texts` 是
    「来源 id → 该来源的实际内容」（供比对）；`jev_judge` 是调用方注入的出网回调
    `(state, questions) -> {"answers": ..., "model": ...}`。**不传 `jev_judge` 时整层不运行**，
    结果与接入 Jev 前逐字节一致；传了但回调失败时判定原样不动、只加一条如实告警
    （JV00 铁律 6：降级必须留痕，绝不静默）。本模块因此仍是纯函数——出网在回调里。

    返回：
      citations            —— 剔除虚构引用后的真实引用列表
      dropped_citations    —— 被剔除的引用（不存在于本次来源）
      available_citations  —— 本次可引用的来源 id
      warnings             —— 形式质量告警（逐条可读，用于前端如实展示）
      sections / report_chars / limitations_count / confidence
      conclusion           —— 模型输出的一句话结论（v28）
      claims / claim_findings —— 证据支撑链（v27 覆盖检查 + v28 支撑强度/时效/口径跨度）
      research_completeness —— Q19 四项关键研究项完成度（缺失项同时进 warnings，软校验不阻断）
    """
    report = payload.get("report")
    report_text = report if isinstance(report, str) else ""

    citations = payload.get("citations")
    citations = citations if isinstance(citations, list) else []
    kept, dropped = filter_citations(citations, allowed)

    warnings: list[str] = []
    if dropped:
        warnings.append(
            f"剔除 {len(dropped)} 条模型引用的、本次未真实取到的来源 id（{'、'.join(dropped)}）——防虚构引用。"
        )

    chars = report_char_count(report_text)
    mode_name, mode_label, mode_min, mode_max = report_mode_budget(mode)
    if chars < mode_min:
        warnings.append(
            f"正文 {chars} 字，低于{mode_label}建议下限 {mode_min} 字（软告警，不阻断发布；篇幅不足可能论证深度不够）。"
        )
    elif chars > mode_max:
        warnings.append(
            f"正文 {chars} 字，超过{mode_label}建议上限 {mode_max} 字，需要压缩重复论述（软告警；新增篇幅应用于解释数据变化与交叉验证，不用于重复指标）。"
        )

    # v31 引用清单一致性（§八.3）：正文/摘要/估值依据的行内引用 vs citations 清单 vs 本次真实来源。
    consistency = citation_consistency(payload, allowed=allowed)
    if consistency["undeclared_used"]:
        warnings.append(
            f"引用清单不一致：正文/摘要/估值依据引用了 {'、'.join(consistency['undeclared_used'])}，"
            "但 citations 清单未登记（来源真实可用，属漏登记而非虚构引用）。"
        )
    if consistency["unavailable_used"]:
        warnings.append(
            f"正文引用了本次不可用的来源 {'、'.join(consistency['unavailable_used'])}，"
            "正文残留不实引用（系统不改写正文），相关内容按未证实对待。"
        )

    summary = str(payload.get("executive_summary", "") or "")
    summary_chars = report_char_count(summary)
    # v30（申通方案 §二.1）：目标 150–165 字，180 为绝对软上限——超限只重写摘要、不重跑全文。
    if summary_chars > SUMMARY_HARD_MAX:
        warnings.append(
            f"执行摘要 {summary_chars}/{SUMMARY_HARD_MAX} 字，超过绝对软上限，"
            "应只重写摘要（四句：结论/最强基本面证据/最强技术反证/下一验证动作），不重跑全文。"
        )
    elif summary_chars > SUMMARY_TARGET_MAX:
        warnings.append(
            f"执行摘要 {summary_chars} 字，超出目标区间 {SUMMARY_TARGET_MIN}–{SUMMARY_TARGET_MAX} 字"
            f"（上限 {SUMMARY_HARD_MAX}，形式告警）。"
        )
    elif summary_chars and summary_chars < SUMMARY_TARGET_MIN:
        warnings.append(
            f"执行摘要 {summary_chars} 字，低于目标区间 {SUMMARY_TARGET_MIN}–{SUMMARY_TARGET_MAX} 字，"
            "可能缺少结论/证据/反证/验证动作四要素之一。"
        )

    sections = _sections(report_text)
    # v31：必需小节按「实质内容」判定（标题 + 标题后有实质正文）；只有标题没有内容算缺席。
    section_states = required_section_state(report_text)
    missing = [item["section"] for item in section_states if not item["present"]]
    if missing:
        warnings.append(f"缺少必需小节：{'、'.join(missing)}（未生成，或仅有标题没有实质内容）。")

    limitations = payload.get("limitations")
    limitations = limitations if isinstance(limitations, list) else []
    if len(limitations) > MAX_LIMITATIONS:
        warnings.append(f"局限声明 {len(limitations)} 条，超过约定的 {MAX_LIMITATIONS} 条上限。")
    elif len(limitations) < MIN_LIMITATIONS:
        warnings.append(f"局限声明 {len(limitations)} 条，少于约定的 {MIN_LIMITATIONS} 条下限。")

    confidence = str(payload.get("confidence", "")).strip()
    if confidence not in ALLOWED_CONFIDENCE:
        warnings.append(
            f"置信度取值 {confidence or '（缺失）'} 不在 {('/'.join(ALLOWED_CONFIDENCE))} 之内，已如实保留原值。"
        )

    # 估值结构化块（S5 引入后）：verdict 越界如实告警；整块缺失提示「估值结论不可机读」。
    valuation = normalize_valuation(payload.get("valuation"))
    verdict = valuation["verdict"]
    if verdict and verdict not in ALLOWED_VALUATION_VERDICTS:
        warnings.append(
            f"估值判断 verdict 取值「{verdict}」不在 {'/'.join(ALLOWED_VALUATION_VERDICTS)} 之内，已如实保留原值。"
        )
    if not any(str(valuation.get(key, "")).strip() for key in VALUATION_KEYS):
        warnings.append("模型未输出结构化估值块（valuation），估值结论只存在于正文，无法机读比对。")
    # v32 正常化估值（方案 §二.3）：状态越界 / 未经验证被降级 / 缺正常化盈利 → 如实告警。
    valuation_status = str(valuation.get("valuation_status", ""))
    if valuation_status and valuation_status not in ALLOWED_VALUATION_STATUS:
        warnings.append(
            f"估值状态 valuation_status 取值「{valuation_status}」不在 "
            f"{'/'.join(ALLOWED_VALUATION_STATUS)} 之内，已如实保留原值。"
        )
    status_note = str(valuation.get("valuation_status_note", ""))
    if status_note:
        warnings.append(f"估值状态降级：{status_note}")
    # J01：结构化 `verdict` 与四态状态冲突时同样进告警集（软告警 → needs_review）。
    verdict_note = str(valuation.get("verdict_note", ""))
    if verdict_note:
        warnings.append(f"估值结论与估值状态冲突：{verdict_note}")
    if str(valuation.get("valuation_status_raw", "")) in ("undervalued", "fair", "expensive") and not valuation["normalized_earnings"]["value"]:
        warnings.append(
            "估值结论未附正常化盈利数值（仅凭分位/倍数比较），安全边际与「低估」结论需人工复核利润可持续性。"
        )
    # —— M3（D01/D02）：正文估值结论必须与结构化口径一致 ——
    # 实例证据（2026-09-15 东瑞 001201）：驾驶舱估值「无法判断」，正文仍写「PS 处于历史低位
    # 也提供了一定的交易安全垫」；S5 显示 PS 高于同业中位数，与「安全垫」表述相反。
    valuation_conflicts = valuation_conclusion_hits(
        report_text, valuation_status, valuation.get("peer_position") or ""
    )
    for item in valuation_conflicts:
        warnings.append(
            f"正文估值表述与结构化口径相反（{'；'.join(item['reasons'])}）：「{item['sentence'][:60]}」"
            "——D01/D02 口径：估值状态为「无法判断」时不得写低估/安全垫结论，"
            "同业位置为「高于同业」时不得反向表述，须改写或删除该句。"
        )
    if valuation_conflicts:
        warnings.append(
            f"共 {len(valuation_conflicts)} 处正文估值表述与驾驶舱口径相反，"
            "正文与结构化结论自相矛盾，需人工复核后才能采信估值段。"
        )

    # v32 首屏驾驶舱三行分项判断（方案 §二.1）：取值越界如实告警；整块缺失提示（旧版提示词无此字段）。
    pillar_verdicts = normalize_pillar_verdicts(payload.get("pillar_verdicts"))
    cockpit_problems = pillar_verdicts_valid(pillar_verdicts)
    if cockpit_problems:
        warnings.append(
            f"驾驶舱分项判断取值越界（{'、'.join(cockpit_problems)}），已如实保留原值；"
            f"词表：基本面 {'/'.join(PILLAR_VERDICT_VOCAB['fundamental'])}，"
            f"价值估值 {'/'.join(PILLAR_VERDICT_VOCAB['valuation'])}，"
            f"技术状态 {'/'.join(PILLAR_VERDICT_VOCAB['technical'])}。"
        )
    if not any(pillar_verdicts[key] for key in PILLAR_VERDICT_KEYS):
        warnings.append("模型未输出首屏驾驶舱三行分项（pillar_verdicts，提示词 2.8 起要求），首屏退回支撑计数展示。")

    # 验证点清单（v2.2）：时点粒度与重复性如实告警。
    # 实例证据（2026-09-10）：报告 5 条验证点的验证时点全部为「2026-10 前」，无一条可据以安排跟踪。
    watchpoints = normalize_watchpoints(payload.get("watchpoints"))
    if not watchpoints:
        warnings.append("模型未输出结构化验证点清单（watchpoints），验证时点无法机读比对。")
    else:
        verify_bys = [item["verify_by"] for item in watchpoints]
        empty_count = sum(1 for value in verify_bys if not value)
        if empty_count:
            warnings.append(f"验证点清单有 {empty_count} 条的验证时点为空，无法据以安排跟踪。")
        filled = [value for value in verify_bys if value]
        if len(filled) > 1 and len(set(filled)) == 1:
            warnings.append(
                f"验证点清单 {len(filled)} 条的验证时点完全相同（「{filled[0]}」），"
                "未按各信号分别给出，参考价值有限。"
            )
        coarse = [value for value in filled if month_only_verify_by(value)]
        if coarse:
            warnings.append(
                f"验证点清单有 {len(coarse)} 条的验证时点只写到月份（如「{coarse[0]}」），"
                "未具体到日或锚定事件。"
            )
        # v27：相对时间词（「下月」「后续」「择机」）既解析不出到期日、也不是可触发事件，
        # 永远进不了待复盘队列 —— 按方案 §3.5「必须绑定日期或明确事件」如实告警。
        vague = [value for value in filled if vague_verify_by(value)]
        if vague:
            warnings.append(
                f"验证点清单有 {len(vague)} 条的验证时点用了相对时间词（如「{vague[0]}」），"
                "既非具体日期也非可触发事件，无法进入待复盘队列。"
            )

    # v27 claim→source 覆盖检查：引用「真实存在」之后，再校验引用是否**支撑得住**该判断。
    # v28：再补支撑强度（full/partial/none）、数据时效与口径跨度（详见 check_claims）。
    claims = normalize_claims(payload.get("claims"))
    claim_findings = check_claims(claims, allowed=allowed, source_asof=source_asof, today=today)
    warnings.extend(claim_findings["warnings"])
    if not claims:
        warnings.append("模型未输出结构化核心判断清单（claims），无法校验「引用是否支撑该判断」。")
    if not claim_findings["staleness_checked"]:
        warnings.append(
            "未做「来源数据是否过期」判定（缺少来源数据日期或当前日期），该项按未校验如实标注。"
        )

    # —— JV04（Jev 决策模型接入路线图 2026-09-21）：逐 claim 引用支撑校验（语义层）——
    # 上面 `check_claims` 是**确定性**口径（引用是否存在 / 是否覆盖所需字段 / 证据等级 / 时效 /
    # 口径跨度），它回答不了「引用的这段内容到底支不支撑这条判断」——拿新闻稿支撑财务结论，
    # 在确定性口径下与拿财报支撑长得一模一样。语义比对交给 Jev（只回类型化答案，无 JSON 解析失败）。
    # 本模块仍是纯函数：出网在调用方注入的 `jev_judge` 回调里（与 `tactics_ai` 同一分工）。
    if jev_judge is not None and claims:
        plan = jev_claim_state(claims, source_texts or {})
        questions = jev_claim_questions(plan["evaluated"])
        answers: Mapping[str, Any] | None = None
        judge_model = ""
        judge_note = ""
        if questions:
            try:
                outcome = jev_judge(plan["state"], questions)
            except Exception as exc:  # noqa: BLE001 - 语义层尽力而为，绝不阻断报告交付
                judge_note = f"语义支撑层调用失败（{type(exc).__name__}）：{exc}"
            else:
                if isinstance(outcome, Mapping):
                    answers = outcome.get("answers") or None
                    judge_model = str(outcome.get("model") or "")
                if not answers:
                    judge_note = "语义支撑层未取得任何应答，判定按确定性口径如实保留。"
        else:
            judge_note = (
                "本次没有判断同时具备「原句 + 已取到的所引证据」，语义支撑层未发起请求。"
            )
        claim_findings = apply_jev_claim_findings(
            claim_findings,
            evaluated=plan["evaluated"],
            answers=answers,
            skipped=plan["skipped"],
            model=judge_model,
            note=jev_note or judge_note,
        )
        warnings.extend(claim_findings.get("jev_warnings") or [])

    # v28 一句话结论（首屏结论卡的第一行）；direction 越界只告警、不改写。
    conclusion = normalize_conclusion(payload.get("conclusion"))
    direction = conclusion["direction"]
    if direction and direction not in ALLOWED_CONCLUSION_DIRECTIONS:
        warnings.append(
            f"结论方向取值「{direction}」不在 {'/'.join(ALLOWED_CONCLUSION_DIRECTIONS)} 之内，已如实保留原值。"
        )
    if not conclusion["statement"]:
        warnings.append("模型未输出一句话结论（conclusion.statement），首屏结论卡只能留空。")

    # —— Q19（P2，2026-09-19 路线图 §5）：深研「研究完成度」进质量闸门 ——
    # 症状：申通深研版样报 1793 字 < 3500 字只回一条字数告警，关键拆解缺失完全看不见。
    # 完成度是**软校验**（不阻断发布），但缺失项必须逐条出现在 warnings 里，
    # 每条写清「缺哪一项 + 用户可读说明 + 下一步补什么」，不拿模块名充数。
    completeness = research_completeness({**payload, "report_mode": mode_name})
    warnings.extend(completeness["warnings"])

    # —— v31 阻断项（§八.4：依据实质章节/证据/必需字段判定，不含字数阈值）——
    blockers = quality_blockers_of(
        missing_sections=missing,
        report_chars=chars,
        kept_citations=kept,
        claim_findings=claim_findings,
    )
    quality_status = quality_status_of(blockers=blockers, warnings=warnings)

    return {
        "citations": kept,
        "dropped_citations": dropped,
        "available_citations": sorted(allowed),
        "warnings": warnings,
        # v30：四级分组（blocking/evidence/computation/format），每条带影响与建议动作（§2.2）。
        "categorized_warnings": categorize_warnings(warnings),
        "categorized_warnings_version": WARN_CATEGORY_VERSION,
        # —— v31 质量恢复（§2.3/§八.4）：三态状态 + 阻断项 + 小节实质状态 + 引用清单一致性 ——
        "quality_status": quality_status,
        "quality_status_label": QUALITY_STATUS_LABELS[quality_status],
        "quality_status_version": QUALITY_STATUS_VERSION,
        "quality_blockers": blockers,
        # Q19（P2）：四项关键研究项完成度（含逐条缺口与只作辅助的字数提示）。
        "research_completeness": completeness,
        "missing_sections": missing,
        "section_states": section_states,
        "citation_consistency": consistency,
        "sections": sections,
        "report_chars": chars,
        "summary_chars": summary_chars,
        # v30：摘要预算（150–165 目标 / 180 绝对软上限；超限只重写摘要，见端点层）。
        "summary_budget": {
            "target_min": SUMMARY_TARGET_MIN,
            "target_max": SUMMARY_TARGET_MAX,
            "hard_max": SUMMARY_HARD_MAX,
            "version": SUMMARY_BUDGET_VERSION,
        },
        "limitations_count": len(limitations),
        "confidence": confidence,
        "valuation": valuation,
        # M3（D01/D02）：正文估值表述与结构化口径相反的句（对齐后应为空）。
        "valuation_language_conflicts": valuation_conflicts,
        # v32 首屏驾驶舱三行分项（结论词口径；与 pillar_rows 支撑计数并列，见 decision_card）。
        "pillar_verdicts": pillar_verdicts,
        # —— v27 ——
        "evidence_quality": evidence_quality_report(allowed),
        "claims": claim_findings["claims"],
        "claim_findings": claim_findings,
        # —— JV04 语义支撑层：未注入 `jev_judge` 时为 None（行为与接入前一致）——
        "jev": claim_findings.get("jev"),
        # —— v28 证据支撑链 ——
        "conclusion": conclusion,
        "summary_downgrade_notes": summary_downgrade_notes(claim_findings),
        # —— v29 模式字数预算（软告警口径随结果留痕）——
        "report_mode": mode_name,
        "report_mode_label": mode_label,
        "report_mode_budget": {"min": mode_min, "max": mode_max},
        "report_mode_version": REPORT_MODE_VERSION,
    }


# ---------------------------------------------------------------------------
# v24 结构化判断字段：多空情景 / 关键价位 / 验证点清单。
# 正文（report）保持 markdown 不变，这三项并行输出，让「关键判断」可机读，
# 为后续复盘与后验评估（路线图 E2）提供可比对的数字与时间口径。
#
# v28（二次方案 §2.4）：情景概率的**表达口径**收敛到本模块——
# 「给得出 0.35 这种数字」的前提是存在同类事件、同市场状态与足够样本的历史基准；
# 本期没有后验统计，因此一律降级为等级表达并显式标注「无历史基准」。
# ---------------------------------------------------------------------------

SCENARIO_PROBABILITY_LEVELS = ("低", "中", "高")
PROBABILITY_BASIS_NONE = "none"
# 只有后验样本数与市场状态同时达标时才会把 basis 改为 "posterior"（P1 接入后回填）。
PROBABILITY_NOTE = "无历史基准：本期尚无同类事件/同市场状态的后验统计，故只用低/中/高表达，不给出小数概率。"


def probability_level_from_value(value: float | None) -> str:
    """主观数值 → 等级（低 <1/3 ≤ 中 <2/3 ≤ 高）；无值返回空串（不猜一个等级出来）。"""
    if value is None:
        return ""
    if value < 1.0 / 3.0:
        return SCENARIO_PROBABILITY_LEVELS[0]
    if value < 2.0 / 3.0:
        return SCENARIO_PROBABILITY_LEVELS[1]
    return SCENARIO_PROBABILITY_LEVELS[2]


def derive_range_status(
    low: float | None,
    high: float | None,
    *,
    declared: str = "",
    basis: str = "",
) -> tuple[str, str]:
    """预期区间五态的**确定性推导**（四次方案 §4）：数值校验不信任模型自报。

    返回 (status, note)。规则：
    - 双端有效但 low >= high 或含负值 → invalid（字段校验失败，前端显示「未生成」+原因）；
    - 双端有效 → estimated；
    - 仅单端有效 → technical_range 当且仅当模型声明 technical_range 或 basis 提及技术位，
      否则 estimated（只给了单边预期值，note 说明「单边」）；
    - 双端缺失 → 模型声明在词表内则采信（not_applicable/unavailable/technical_range）；
      无声明时按 basis 关键词判：事件词 → not_applicable、技术词 → technical_range（仅有技术位）、
      其余 → unavailable（缺可靠基准）。
    """
    text_declared = str(declared or "").strip().lower()
    basis_text = str(basis or "")
    declared_valid = text_declared in RANGE_STATUSES

    if low is not None and high is not None:
        if low >= high:
            return "invalid", f"区间下界 {low} 不小于上界 {high}，字段校验失败，不予展示。"
        if low < 0 or high < 0:
            return "invalid", f"区间含负值（{low}～{high}），价格区间不应为负，字段校验失败。"
        return "estimated", ""

    if low is not None or high is not None:
        single = low if low is not None else high
        if declared_valid and text_declared == "technical_range":
            return "technical_range", "仅有单边技术位推得的预期值。"
        if any(word in basis_text for word in _TECH_BASIS_WORDS):
            return "technical_range", f"仅有技术位推得的单边预期（{single}）。"
        return "estimated", "仅有单边预期值，无对侧边界。"

    # 双端缺失
    if declared_valid and text_declared in ("not_applicable", "unavailable", "technical_range", "invalid"):
        return text_declared, ""
    if any(word in basis_text for word in _EVENT_BASIS_WORDS):
        return "not_applicable", "情景由公告/财报类事件验证，无合理价格目标（不强行补价格）。"
    if any(word in basis_text for word in _TECH_BASIS_WORDS):
        return "technical_range", "仅有支撑/压力等技术位，无收益分布可算区间。"
    return "unavailable", "模型未给出可靠范围（缺历史基准或有效输入）。"


def normalize_scenarios(value: Any) -> list[dict[str, Any]]:
    """[{name, trigger, invalidates, horizon, probability, expected_range, source}]。

    v27 补 `invalidates`（失效条件）、`horizon`（持有/验证窗口）、`probability`（主观概率）
    与 `expected_range`（预期区间）——原方案只要求「触发条件」，缺了「什么条件会让它失效」，
    导致情景无法被证伪。字段缺失一律如实留空/置 None，绝不编造。

    v28（二次方案 §2.4「概率和价格区间的使用边界」）：**概率只有在具备历史基准时才显示数值**。
    本期尚无同类事件/同市场状态的后验统计（后验闭环属 P1），因此 `probability` 一律置 None，
    改由 `probability_level`（低/中/高）表达，并显式标注 `probability_basis="none"`（无历史基准）；
    模型若给了原值，原样保留在 `probability_raw` 供审计——**降级表达不抹掉模型原话**。
    将来接入后验统计后，只有样本数与市场状态同时达标才会把 `probability` 填回数值。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        raw = item.get("probability")
        probability_raw: float | None = None
        try:
            probability_raw = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            probability_raw = None
        if probability_raw is not None and not 0.0 <= probability_raw <= 1.0:
            # 模型可能按百分数给出（35 而非 0.35）：**如实换算并保留原值**，不静默改写。
            if 1.0 < probability_raw <= 100.0:
                probability_raw = round(probability_raw / 100.0, 4)
            else:
                probability_raw = None

        # 模型也可能直接输出「低/中/高」：优先采信，不再折算。
        declared_level = str(item.get("probability_level", "")).strip()
        if declared_level in SCENARIO_PROBABILITY_LEVELS:
            level = declared_level
        else:
            level = probability_level_from_value(probability_raw)

        # v30：预期区间五态（数值校验不信任模型自报；声明只作无值时的采信输入）。
        range_low = (
            _as_float_or_none(item.get("expected_range", {}).get("low"))
            if isinstance(item.get("expected_range"), dict) else None
        )
        range_high = (
            _as_float_or_none(item.get("expected_range", {}).get("high"))
            if isinstance(item.get("expected_range"), dict) else None
        )
        range_basis = str(item.get("range_basis", "")).strip()
        status, status_note = derive_range_status(
            range_low, range_high,
            declared=str(item.get("range_status", "")),
            basis=range_basis,
        )

        out.append({
            "name": str(item.get("name", "")).strip(),
            "trigger": str(item.get("trigger", "")).strip(),
            "invalidates": str(item.get("invalidates", "")).strip(),
            "horizon": str(item.get("horizon", "")).strip(),
            # v28：无历史基准 → 不展示数值，只用等级；原值留在 probability_raw。
            "probability": None,
            "probability_level": level,
            "probability_basis": PROBABILITY_BASIS_NONE,
            "probability_note": PROBABILITY_NOTE,
            "probability_raw": probability_raw,
            "expected_range": {
                "low": range_low,
                "high": range_high,
            },
            # v30：区间五态——空值有语义，前端禁止把所有空区间显示成同一个破折号。
            "range_status": status,
            "range_status_label": RANGE_STATUS_LABELS[status],
            "range_status_note": status_note,
            "range_basis": range_basis,
            "source": str(item.get("source", "")).strip(),
        })
    return out


def normalize_levels(value: Any) -> dict[str, list[dict[str, Any]]]:
    """{support: [{price, basis}], resistance: [{price, basis}]}；price 非数值时如实置 None。"""
    out: dict[str, list[dict[str, Any]]] = {"support": [], "resistance": []}
    if not isinstance(value, dict):
        return out
    for key in ("support", "resistance"):
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows[:3]:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            try:
                price_value: float | None = float(price)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                price_value = None
            out[key].append({"price": price_value, "basis": str(item.get("basis", "")).strip()})
    return out


WATCHPOINT_STATUS_PENDING = "pending_review"


def normalize_watchpoints(value: Any) -> list[dict[str, Any]]:
    """[{signal, verify_by, expected_if_true, due_on, status}]；verify_by 为验证时点（具体到日或锚定事件，v2.2）。

    v27：每条验证点带 `status`（初始一律 `pending_review`，即「待复盘」）与 `due_on`
    （能从 verify_by 解析出**具体日期**时填写 ISO 日期，否则留空——锚定事件的到期由事件触发，
    不猜日期）。待复盘队列（`GET /ai-research/review-queue`）据此派生出「已到期/待到期」清单。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        verify_by = str(item.get("verify_by", "")).strip()
        out.append({
            "signal": str(item.get("signal", "")).strip(),
            "verify_by": verify_by,
            "expected_if_true": str(item.get("expected_if_true", "")).strip(),
            "due_on": extract_due_date(verify_by),
            "status": WATCHPOINT_STATUS_PENDING,
        })
    return out


# ---------------------------------------------------------------------------
# v24 档 A/B/C：估值结构化块（市盈率一类）。
# 证据包新增 S5 估值来源后，把模型的估值判断也收敛成可机读字段，供前端估值卡与复盘使用；
# verdict 只接受四个既定取值，越界如实告警但不改写模型原话。
#
# v32 价值投资升级（2026-09-13 方案 §二.3）：估值块扩展五组字段——
#   normalized_earnings（正常化盈利：剔除一次性/周期因素后的可持续口径）
#   valuation_cases（悲观/基准/乐观三情景的盈利×倍数→公允价值）
#   margin_of_safety（当前价相对基准公允价值的安全边际）
#   valuation_status（undervalued|fair|expensive|undetermined）
#   valuation_limitations（估值口径限制）
# 核心铁律：「低 PE 分位 ≠ 低估」——`valuation_status` 的取值**不信任模型自报**，
# 与情景区间五态同一口径做确定性推导：声称低估/合理/偏贵但拿不出正常化盈利值或依据时，
# 服务端降级为 `undetermined`（原声明保留在 `valuation_status_raw`，note 说明降级原因），
# 防止「PE 低分位 + 利润周期高点」被误读成价值低估。
# ---------------------------------------------------------------------------

VALUATION_KEYS = ("pe_ttm", "pe_percentile", "peer_position", "verdict", "basis")
ALLOWED_VALUATION_VERDICTS = ("偏贵", "合理", "偏便宜", "无数据")

ALLOWED_VALUATION_STATUS = ("undervalued", "fair", "expensive", "undetermined")
VALUATION_STATUS_LABELS: dict[str, str] = {
    "undervalued": "低估",
    "fair": "合理",
    "expensive": "偏贵",
    "undetermined": "无法判断",
}
VALUATION_STATUS_VERSION = "v1"

# ---------------------------------------------------------------------------
# M3（D01/D02/D03/D04，2026-09-15 第二轮路线图）：个股研报口径对齐（确定性）
# ---------------------------------------------------------------------------
# 根因（M0 基线）：估值闸门已把东瑞 001201 降成 `undetermined`（驾驶舱「无法判断」），
# 正文却仍写「PS 处于历史低位也提供了一定的交易安全垫」；反方自陈「S5 显示 PS 高于同业
# 中位数」——**正文结论与结构化口径相反**。个股反方约定是「只指软肋、不改写正文」，所以
# 这层必须由服务端确定性对齐：不代写结论、不编数字，只把不成立的结论句标明「不成立」。
VALUATION_STATUS_UNDETERMINED = "undetermined"

# J01（2026-09-18 桌面端升级路线图）：与「无法判断」互斥的**结论词**。
# D01 只校正文句子里的禁词，结构化 `verdict` 一直游离在一致性之外，于是出现过
# 同一张估值表里「结论=偏便宜」与「估值状态=无法判断」并排呈现的研报。
VALUATION_CONCLUSION_VERDICTS: tuple[str, ...] = ("偏贵", "合理", "偏便宜")
VALUATION_VERDICT_NO_DATA = "无数据"

# —— D01：估值结论禁词（仅当 valuation_status=undetermined 时生效）——
VALUATION_CONCLUSION_BAN_PHRASES: tuple[str, ...] = (
    "安全垫", "低估", "被低估", "偏便宜", "估值保护", "提供保护", "估值低位提供保护", "错杀",
)
# 句内已自我否认的不算结论（如「低分位不等于低估」「不构成安全垫」）。
# 注意：「无法判断」**不在**此列——它交给下面的转折感知句式处理，
# 否则「无法判断当前估值，但 PS 提供安全垫」会被整句豁免，把真结论放过去。
VALUATION_DENIAL_MARKERS: tuple[str, ...] = (
    "不等于低估", "不代表低估", "并非低估", "不构成低估", "不是低估",
    "不构成安全垫", "不提供安全垫", "无安全垫", "不能推出低估", "不作为结论依据",
)
# 实测假阳性（M0 东瑞真实正文，2026-09-15 回归）：
# 「…当前无法简单判定估值为偏贵、合理或偏便宜，需等待盈利趋势验证」——否定词与禁词同处一个
# 枚举句式，句子本身**已自我否认**，却因禁词字面命中被误判为结论句。加一条紧约束模式：
# 否定词 → 12 字内判定动词 → 其后 24 字内出现禁词；且**否定只覆盖到第一个转折词之前**，
# 这样「无法判断估值，但 PS 提供安全垫」这类转折句仍会被正常命中（不放过真结论）。
#
# 2026-09-15 G03 真实重跑（001201 同一指标口径）又暴露同族假阳性三条，正文原话：
#   「…叠加公司连续亏损，低PB不能单独证明低估」
#   「…正常化盈利未验证前无法得出低估结论」
#       → 推理动词不止 判定/判断/给出/确定，还含 得出/推出/证明/说明/等同/支持/结论…；
#   「…估值结论应为undetermined而非偏便宜/偏贵」
#       → 该句没有「否定+动词」结构，靠的是对比否定：禁词**紧跟在「而非」之后**。
# 故判定改为「**逐次出现**」粒度：句内每一处禁词要么紧跟对比否定词，要么落在否定窗口内，
# 全部满足才算自我否认。这样「…应为undetermined而非偏便宜，但 PS 提供安全垫」这类
# **半否认半结论**的句子不会被整句豁免（仍会命中「安全垫」）。
# 2026-09-15 G03 最终重跑（4f2599b2）再补一枚：正文原话
#   「…正常化盈利未验证，低PB/PS分位不能推断低估」
#       → 动词表缺「推断」，该否定句被加了不必要的行内标注（不影响结论正确性，属标注噪音）。
_VALUATION_DENIAL_RE = re.compile(
    r"(?:无法|不能|不足以|不宜|不应|不可|难以|没有|不构成|并非|不是)"
    r"[^。；;]{0,12}"
    r"(?:判定|判断|给出|确定|得出|推出|推断|推定|断言|证明|说明|等同|定义|认定|视为|当作|看作|作为"
    r"|成立|支持|构成|称为|依据|结论)"
)
# 对比否定：禁词紧跟在下列词之后 → 该处禁词本身即被否认（「而非偏便宜」= 不说它偏便宜）。
_VALUATION_CONTRAST_WORDS: tuple[str, ...] = (
    "而非", "而不是", "绝非", "不等于", "不代表", "不是",
)
VALUATION_CONTRAST_LOOKBEHIND = 6
_VALUATION_TURN_WORDS: tuple[str, ...] = ("但是", "但", "然而", "不过", "却", "反而")
VALUATION_DENIAL_WINDOW = 24


def _ban_matches(text: str) -> list[tuple[int, str]]:
    """句内每一处禁词的 `(起始下标, 命中词)`（按出现顺序取**最长且不重叠**的匹配）。

    禁词表里存在包含关系（「被低估」含「低估」、「估值保护」与「提供保护」部分重叠），
    若逐字符收集会把同一处禁词数成两处、而它们的前缀否定上下文不同，导致误判；
    故取最长匹配并跳过已覆盖区间。
    """
    matches: list[tuple[int, str]] = []
    index = 0
    while index < len(text):
        longest = ""
        for word in VALUATION_CONCLUSION_BAN_PHRASES:
            if len(word) > len(longest) and text.startswith(word, index):
                longest = word
        if longest:
            matches.append((index, longest))
            index += len(longest)
        else:
            index += 1
    return matches


def undoubted_ban_phrases(text: str) -> list[str]:
    """句内**未经否认**的禁词（逐处判定；一处被否认不影响其他处）。

    与 `is_valuation_denial()` 是同一判定的两种用法：后者回答「整句是否自我否认」，
    前者给出「还剩哪些结论词没被撤回」——标注与告警都只该针对后者。
    """
    return [word for pos, word in _ban_matches(text) if not _denial_covers(text, pos)]


def _denial_covers(text: str, pos: int) -> bool:
    """`text[pos:]` 处的禁词是否被否认（对比否定紧邻，或落在某个否定窗口内）。"""
    head = text[max(0, pos - VALUATION_CONTRAST_LOOKBEHIND) : pos]
    if any(head.endswith(word) for word in _VALUATION_CONTRAST_WORDS):
        return True
    for match in _VALUATION_DENIAL_RE.finditer(text, 0, pos):
        if pos - match.end() > VALUATION_DENIAL_WINDOW:
            continue
        # 转折词前的否定覆盖不到转折词后的禁词：「无法判断估值，但 PS 提供安全垫」不豁免。
        if any(turn in text[match.start() : match.end()] for turn in _VALUATION_TURN_WORDS):
            continue
        if any(turn in text[match.end() : pos] for turn in _VALUATION_TURN_WORDS):
            continue
        return True
    return False


def is_valuation_denial(probe: str) -> bool:
    """该句是否**自我否认**了估值结论（D01/D02 的豁免条件）。

    先查固定否认词组；再**逐处**判定句内禁词：每处都被否认才算自我否认——转折词之后出现的
    禁词不算被否认，避免把「无法判断估值，但 PS 提供安全垫」当作合规句放过。
    """
    text = str(probe or "")
    if any(marker in text for marker in VALUATION_DENIAL_MARKERS):
        return True
    matches = _ban_matches(text)
    if not matches:
        return False
    return all(_denial_covers(text, pos) for pos, _ in matches)

VALUATION_ALIGN_NOTE = "（估值状态为「无法判断」：本句不成立低估/安全垫结论，已按 D01 口径标注）"

# —— D02：S5 比较句不得与来源相反 ——
PEER_POSITION_ABOVE = "高于同业"
PEER_POSITION_BELOW_WORDS: tuple[str, ...] = (
    "低于同业", "低于行业中位数", "低于同业中位数", "低于行业",
)
# R01（2026-09-18 排版与研报呈现一致性路线图）：D02 必须**认准指标**。
# 旧实现把「peer_position 里出现『高于』」一律当作「PS 高于同业」，于是当 S5 的
# peer_position 实际讲的是 **PE** 高于同业（样报 B `:57`「高于工业金属同业PE(TTM)中位数8.76」）、
# 而正文写「PB/PS 低于同业中位数」（真话）时，被误判为矛盾并加注，与同段 `:106` 自相打架。
# 修法：按指标归属校验——只有正文某分句点名的指标**同时**被 S5 判为「高于同业」时，才计矛盾；
# 注记文案据 S5 高于的指标动态生成（写明是哪个指标）。分句里没有点名指标的「高于」仍归给 PS
# （D02 的原始默认，保持向后兼容）。`valuation_evidence` 的 PE/PB/PS 同业中位数虽分开可得，
# 但流入 D02 的 `peer_position` 是模型自写的合并串（见 prompting.py:73），故以「点名指标」为准，
# 不凭合并串臆测未点名指标的归属。
PEER_METRIC_LABELS: dict[str, str] = {"pe": "PE", "pb": "PB", "ps": "PS"}
_PEER_METRIC_RE = re.compile(r"P(?:E|B|S)", re.IGNORECASE)
# 所有 D02 注记共享的收尾标记，用于幂等检测（重复执行不再叠加标注）。
_D02_NOTE_MARKER = "，已按 D02 口径标注）"
_CLAUSE_SPLIT = re.compile(r"[，,、；;。！？\n]")


def peer_contradict_note(metrics: list[str]) -> str:
    """按「S5 判为高于同业」的指标集合生成 D02 矛盾注记（R01：写明是哪个指标）。"""
    labels = "、".join(PEER_METRIC_LABELS.get(m, m.upper()) for m in metrics) or "PS"
    return (
        f"（S5 显示 {labels} 高于同业中位数，与「{labels} 低于同业/提供安全垫」的表述相反"
        f"{_D02_NOTE_MARKER}"
    )


# 向后兼容 / 测试引用：PS 单指标的默认注记文案。
PEER_CONTRADICT_NOTE = peer_contradict_note(["ps"])


def peer_above_metrics(value: Any) -> set[str]:
    """S5 同业位置里被判为「高于同业」的指标集合（pe/pb/ps 的小写键）。

    按分句解析合并串：含「高于」（且非「不高于」）的分句，其点名的指标计入；该分句未点名
    任何指标时归给 PS（D02 的原始默认，保持向后兼容）。
    """
    text = str(value or "")
    metrics: set[str] = set()
    for clause in _CLAUSE_SPLIT.split(text):
        if "高于" not in clause or "不高于" in clause:
            continue
        named = {match.group(0).lower() for match in _PEER_METRIC_RE.finditer(clause)}
        metrics |= named if named else {"ps"}
    return metrics


def peer_contradiction_words(probe: str, above_metrics: set[str] | None = None) -> list[str]:
    """D02：该句里与 S5「高于同业」相反的表述。

    - 传入 `above_metrics`（R01 主路径）时，**仅当分句点名的指标 ∈ above_metrics** 才计
      「低于同业」类矛盾——避免「PE 高于」误伤正文的「PS/PB 低于」真话；
    - `above_metrics=None`（历史单参调用）时退回旧口径：只有点名 PS 的分句才算矛盾；
    - 「安全垫」不受指标邻近限制（它本身就是结论禁词）。
    """
    text = str(probe or "")
    words: list[str] = []
    if "安全垫" in text:
        words.append("安全垫")
    for clause in _CLAUSE_SPLIT.split(text):
        if not any(word in clause for word in PEER_POSITION_BELOW_WORDS):
            continue
        if above_metrics is None:
            if "PS" not in clause.upper():
                continue
        else:
            named = {match.group(0).lower() for match in _PEER_METRIC_RE.finditer(clause)}
            if not (named & above_metrics):
                continue
        for word in PEER_POSITION_BELOW_WORDS:
            if word in clause:
                words.append(word)
    return sorted(set(words))


# —— D03：验证点必须落到日或锚定事件 ——
# 实测（M0）：东瑞报告一条验证点为「2026年9月生猪销售简报披露后」——只到月份、无到期窗口，
# 既解析不出日期，也进不了待复盘队列。口径：锚定事件写法必须带**到期窗口**；定不出锚点事件
# 的月份/相对时间写法一律标「时点未知」，**绝不编假日期**。
VERIFY_BY_ANCHOR_WORDS: tuple[str, ...] = (
    "披露", "公告", "简报", "月报", "季报", "年报", "财报", "发布", "统计", "决议", "会议", "数据",
)
VERIFY_BY_WINDOW_DAYS = 5
VERIFY_BY_WINDOW_TEXT = f"{VERIFY_BY_WINDOW_DAYS} 个交易日内"
VERIFY_BY_UNKNOWN_TEXT = "时点未知（未确定到日或可触发事件，须人工补）"

_SENTENCE_SPLIT_KEEP = re.compile(r"([。！？；;\n])")


def peer_position_is_above(value: Any) -> bool:
    """S5 同业位置是否「高于同业」（确定性：认「高于」且不认「不高于」）。"""
    text = str(value or "")
    if not text or "不高于" in text:
        return False
    return "高于" in text


def _sentence_fragments(text: str) -> list[str]:
    """按句号/分号/换行切句（保留顺序；调用方自行 strip）。"""
    return [part for part in _SENTENCE_SPLIT_KEEP.split(str(text or ""))]


def valuation_conclusion_hits(
    report: str, valuation_status: Any, peer_position: Any = ""
) -> list[dict[str, Any]]:
    """D01/D02：正文与结构化估值口径**相反**的句（逐句命中 + 原因）。

    - D01：`valuation_status == undetermined` 时，正文不得出现低估/安全垫类结论词；
    - D02：S5 同业位置为「高于同业」时，正文不得写「低于同业」或「安全垫」。
    已带对齐标注、或句内已自我否认的句子不再命中（幂等，避免与自己打架）。
    """
    status = str(valuation_status or "")
    above_metrics = peer_above_metrics(peer_position)
    above = peer_position_is_above(peer_position)
    if status != VALUATION_STATUS_UNDETERMINED and not above:
        return []
    hits: list[dict[str, Any]] = []
    for fragment in _sentence_fragments(report):
        probe = fragment.strip()
        if not probe or VALUATION_ALIGN_NOTE in probe or _D02_NOTE_MARKER in probe:
            continue
        if is_valuation_denial(probe):
            continue
        reasons: list[str] = []
        if status == VALUATION_STATUS_UNDETERMINED:
            banned = undoubted_ban_phrases(probe)
            if banned:
                reasons.append("估值状态「无法判断」但正文出现结论词：" + "、".join(banned))
        if above:
            contradict = peer_contradiction_words(probe, above_metrics)
            if contradict:
                reasons.append(
                    f"S5 为「{PEER_POSITION_ABOVE}」但正文写：" + "、".join(contradict)
                )
        if reasons:
            hits.append({"sentence": probe[:160], "reasons": reasons})
    return hits


def align_valuation_language(
    report: str, valuation_status: Any, peer_position: Any = ""
) -> tuple[str, list[dict[str, Any]]]:
    """D01/D02：给结论相反的句子加**对齐标注**（保留事实句，撤回结论语气）。

    与方向研判 D05「撤下/降级即加行内标注」同一处理哲学：服务端不代写结论、不编数字、
    不删模型原文，只把不成立的结论句标明不成立并留痕（`changes` 含行号与原句）。
    """
    status = str(valuation_status or "")
    above_metrics = peer_above_metrics(peer_position)
    above = peer_position_is_above(peer_position)
    if status != VALUATION_STATUS_UNDETERMINED and not above:
        return report, []
    changes: list[dict[str, Any]] = []
    lines = str(report or "").splitlines()
    changed = False
    for index, line in enumerate(lines):
        parts = _SENTENCE_SPLIT_KEEP.split(line)
        touched = False
        for pos in range(0, len(parts), 2):
            fragment = parts[pos]
            probe = fragment.strip()
            if not probe or VALUATION_ALIGN_NOTE in probe or _D02_NOTE_MARKER in probe:
                continue
            if is_valuation_denial(probe):
                continue
            notes: list[str] = []
            if status == VALUATION_STATUS_UNDETERMINED and undoubted_ban_phrases(probe):
                notes.append(VALUATION_ALIGN_NOTE)
            if above:
                contradict = peer_contradiction_words(probe, above_metrics)
                if contradict:
                    note = peer_contradict_note(sorted(above_metrics) or ["ps"])
                    if note not in notes:
                        notes.append(note)
            if not notes:
                continue
            parts[pos] = f"{fragment}{''.join(notes)}"
            touched = True
            changes.append({"line": index + 1, "sentence": probe[:120], "notes": notes})
        if touched:
            lines[index] = "".join(parts)
            changed = True
    if not changed:
        # 一处未改就返回**原对象**：不因为 splitlines/join 而改掉尾部换行这类无意义差异。
        return report, []
    return "\n".join(lines), changes


def needs_summary_rewrite(summary: str) -> bool:
    """D04：摘要超过**目标上界**（165）即触发摘要重写（原策略只在超 180 硬上限时重写）。

    实测（M0）：东瑞摘要 176 字——落在「超出目标区间」与硬上限之间，原策略不触发重写，
    于是一条本可自动压回 150–165 的形式告警被留给了用户。口径改为：超出目标上界就重写。
    """
    return report_char_count(summary) > SUMMARY_TARGET_MAX


def anchor_window_verify_by(value: str) -> bool:
    """验证时点是否已带「…披露后 N 个交易日内」这类到期窗口（D03 认可的锚定写法）。"""
    return bool(re.search(r"后\s*\d+\s*个?\s*交易日内", str(value or "")))


def repair_verify_by(value: str) -> tuple[str, str]:
    """D03：把粒度不足的验证时点改成「锚定事件 + 到期窗口」或「时点未知」。

    返回 `(新值, 处理原因)`；无需处理时原样返回、原因为空串。
    - 已带到期窗口 / 已能解析出具体日期 → 不动；
    - 只到月份或用了相对时间词，但含锚定事件词（简报/披露/公告…）→ 追加到期窗口（不编日期）；
    - 其余 → 「时点未知」，**绝不编假日期**。
    """
    text = str(value or "").strip()
    if not text:
        return text, ""
    if anchor_window_verify_by(text) or extract_due_date(text):
        return text, ""
    if month_only_verify_by(text) or vague_verify_by(text):
        if any(word in text for word in VERIFY_BY_ANCHOR_WORDS):
            base = text if text.endswith("后") else f"{text}后"
            return f"{base} {VERIFY_BY_WINDOW_TEXT}", "anchored_window"
        return VERIFY_BY_UNKNOWN_TEXT, "unknown_timepoint"
    return text, ""


def repair_watchpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """D03：就地修复 `payload["watchpoints"][].verify_by`，返回逐条留痕。

    只改**时点粒度**，不动 signal / expected_if_true；模型原话保留在 `verify_by_raw`
    （审计可回溯，不静默丢弃）。**不编造日期**、不替模型补事件。
    """
    items = payload.get("watchpoints")
    if not isinstance(items, list):
        return []
    repairs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        original = str(item.get("verify_by", "") or "").strip()
        fixed, reason = repair_verify_by(original)
        if not reason or fixed == original:
            continue
        if "verify_by_raw" not in item:
            item["verify_by_raw"] = original
        item["verify_by"] = fixed
        repairs.append({
            "signal": str(item.get("signal", "")).strip(),
            "from": original,
            "to": fixed,
            "reason": reason,
        })
    return repairs

_VALUATION_CASE_NAMES = ("bear", "base", "bull")
_VALUATION_CASE_LABELS: dict[str, str] = {"bear": "悲观", "base": "基准", "bull": "乐观"}
VALUATION_CONFIDENCE_LEVELS = ("low", "medium", "high")


def _normalized_earnings_of(value: Any) -> dict[str, Any]:
    """正常化盈利块：{value: float|None, basis: str, confidence: low|medium|high|""}（不编造）。"""
    if not isinstance(value, dict):
        return {"value": None, "basis": "", "confidence": ""}
    confidence = str(value.get("confidence", "")).strip().lower()
    return {
        "value": _as_float_or_none(value.get("value")),
        "basis": str(value.get("basis", "")).strip(),
        "confidence": confidence if confidence in VALUATION_CONFIDENCE_LEVELS else "",
    }


def _valuation_cases_of(value: Any) -> list[dict[str, Any]]:
    """三情景估值 [{name: bear|base|bull, earnings, multiple, fair_value}]；非数值字段如实 None。"""
    cases: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return cases
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        cases.append({
            "name": name if name in _VALUATION_CASE_NAMES else "",
            "name_label": _VALUATION_CASE_LABELS.get(name, ""),
            "earnings": _as_float_or_none(item.get("earnings")),
            "multiple": _as_float_or_none(item.get("multiple")),
            "fair_value": _as_float_or_none(item.get("fair_value")),
        })
    return cases


def derive_valuation_status(
    *,
    declared: str,
    normalized_earnings: dict[str, Any],
) -> tuple[str, str]:
    """估值状态四态的**确定性推导**（同 `derive_range_status` 口径：不信任模型自报）。

    规则：
    - 声明 undervalued/fair/expensive 且正常化盈利有值或有依据 → 采信声明；
    - 声明 undervalued/fair/expensive 但既无正常化盈利值又无依据 → 降级 undetermined，
      note 说明「低 PE 分位不能排除利润周期高点/一次性收益」（方案 §一.2）；
    - 声明 undetermined 或缺失 → undetermined（note 说明未验证）；
    - 其他取值（越界）→ 原样返回，由 `validate_report` 告警。
    """
    text = str(declared or "").strip().lower()
    has_basis = bool(normalized_earnings.get("value") is not None or str(normalized_earnings.get("basis") or "").strip())
    if text in ("undervalued", "fair", "expensive"):
        if has_basis:
            return text, ""
        return "undetermined", (
            f"模型声称「{VALUATION_STATUS_LABELS[text]}」但未给出正常化盈利值或依据；"
            "低 PE 分位不能排除利润处于周期高点、一次性收益或现金流较弱，按「无法判断」处理。"
        )
    if text in ("", "undetermined"):
        return "undetermined", "" if text else "模型未输出估值状态（旧版提示词），按未判定处理。"
    return text, ""


def normalize_valuation(value: Any) -> dict[str, Any]:
    """估值结构化块（v32 扩展）：基础五字段（字符串）+ 正常化估值五组字段。

    非dict / 字段缺失一律留空/None（不编造模型没说过的口径）；`valuation_status`
    经 `derive_valuation_status` 确定性推导，模型原声明保留在 `valuation_status_raw`。
    """
    out: dict[str, Any] = {
        key: "" for key in VALUATION_KEYS
    }
    out.update({
        "normalized_earnings": {"value": None, "basis": "", "confidence": ""},
        "valuation_cases": [],
        "margin_of_safety": None,
        "valuation_status": "",
        "valuation_status_raw": "",
        "valuation_status_note": "",
        "verdict_raw": "",
        "verdict_note": "",
        "valuation_limitations": [],
    })
    if not isinstance(value, dict):
        return out
    for key in VALUATION_KEYS:
        out[key] = str(value.get(key, "")).strip()
    out["normalized_earnings"] = _normalized_earnings_of(value.get("normalized_earnings"))
    out["valuation_cases"] = _valuation_cases_of(value.get("valuation_cases"))
    out["margin_of_safety"] = _as_float_or_none(value.get("margin_of_safety"))
    raw_limitations = value.get("limitations")
    out["valuation_limitations"] = (
        [str(item).strip() for item in raw_limitations if str(item).strip()][:3]
        if isinstance(raw_limitations, list) else []
    )
    out["valuation_status_raw"] = str(value.get("valuation_status", "")).strip().lower()
    status, note = derive_valuation_status(
        declared=out["valuation_status_raw"],
        normalized_earnings=out["normalized_earnings"],
    )
    out["valuation_status"] = status
    out["valuation_status_note"] = note
    # —— J01：结构化结论词与四态状态同口径（不删模型原话，原值进 verdict_raw）——
    out["verdict_raw"] = out["verdict"]
    if status == VALUATION_STATUS_UNDETERMINED and out["verdict"] in VALUATION_CONCLUSION_VERDICTS:
        out["verdict_note"] = (
            f"模型给出的估值结论「{out['verdict']}」缺少正常化盈利支撑，"
            "按「无法判断」口径归一为「无数据」（原结论词保留在 verdict_raw）。"
        )
        out["verdict"] = VALUATION_VERDICT_NO_DATA
    out["valuation_evidence"] = derive_valuation_evidence_state(out)
    return out


# ---------------------------------------------------------------------------
# Q05（2026-09-19 研报与追问质量路线图）：估值「三态」口径
# ---------------------------------------------------------------------------
# 症状：申通样例里 PE(TTM)11.70、PB(MRQ)1.94、历史分位、同业位次全都有，估值块却整行写
# 「无数据」——把**合理价值尚未评估**说成了**指标缺失**，用户据此以为系统没取到数据。
# 这里把两件事拆成三态，页面、导出与追问提示词共用同一份推导：
#   metric_missing            指标本身没取到（S5 缺失/失败）
#   normalization_unverified  指标已取得，但正常化盈利未验证 → 合理价值未评估（**不是无数据**）
#   fair_value_assessed       正常化盈利 + 情景公允价值/安全边际齐备 → 已评估
VALUATION_METRIC_FIELDS = ("pe_ttm", "pe_percentile", "peer_position")
VALUATION_EVIDENCE_STATES = ("metric_missing", "normalization_unverified", "fair_value_assessed")
VALUATION_EVIDENCE_STATE_LABELS: dict[str, str] = {
    "metric_missing": "估值指标缺失",
    "normalization_unverified": "指标已取得，合理价值尚未评估",
    "fair_value_assessed": "合理价值已完成评估",
}
# 占位写法视同「没取到」：模型常把缺失写成「无 / — / 不适用」。
_VALUATION_PLACEHOLDERS = ("无", "—", "-", "不适用", "无数据", "n/a", "na", "null", "未提供")


def _metric_present(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.lower() not in _VALUATION_PLACEHOLDERS and text not in _VALUATION_PLACEHOLDERS


def derive_valuation_evidence_state(valuation: Any) -> dict[str, Any]:
    """估值三态的**确定性推导**（不信任模型自报的口径词）。

    返回 {state, label, note, display, verdict_display, metrics, metric_count,
    fair_value_ready, margin_of_safety, valuation_status, valuation_status_label}。
    `display` 是给提示词/页面/导出共用的单行表述，`verdict_display` 替换掉笼统的「无数据」。
    """
    data = valuation if isinstance(valuation, dict) else {}
    metrics = {key: str(data.get(key) or "").strip() for key in VALUATION_METRIC_FIELDS if _metric_present(data.get(key))}
    normalized = _normalized_earnings_of(data.get("normalized_earnings"))
    cases = [row for row in _valuation_cases_of(data.get("valuation_cases")) if row.get("fair_value") is not None]
    margin = _as_float_or_none(data.get("margin_of_safety"))
    status = str(data.get("valuation_status") or "").strip().lower()
    earnings_ready = normalized.get("value") is not None or bool(str(normalized.get("basis") or "").strip())
    fair_value_ready = bool(cases) or margin is not None

    if earnings_ready and fair_value_ready and status in ("undervalued", "fair", "expensive"):
        state = "fair_value_assessed"
        note = (
            "已给出正常化盈利口径与情景公允价值"
            f"{'（安全边际 ' + format(margin, '.1%') + '）' if margin is not None else ''}，"
            "估值结论可按「已评估」使用；仍建议复核正常化依据的可持续性。"
        )
    elif metrics:
        state = "normalization_unverified"
        listed = "｜".join(f"{key} {value}" for key, value in metrics.items())
        note = (
            f"{listed} 已取得，但正常化盈利未经验证（未拆分一次性损益/周期因素），"
            "因此不能给出低估或偏贵结论——这属于「合理价值尚未评估」，**不是没有估值数据**。"
        )
    else:
        state = "metric_missing"
        note = "未取得 PE/PB/历史分位等同口径估值指标（来源缺失或获取失败），本轮不对合理价值作判断。"

    return {
        "state": state,
        "label": VALUATION_EVIDENCE_STATE_LABELS[state],
        "note": note,
        "metrics": metrics,
        "metric_count": len(metrics),
        "fair_value_ready": fair_value_ready,
        "margin_of_safety": margin,
        "valuation_status": status,
        "valuation_status_label": VALUATION_STATUS_LABELS.get(status, ""),
        "normalized_earnings_ready": earnings_ready,
        "display": f"{VALUATION_EVIDENCE_STATE_LABELS[state]}：{note}",
        "verdict_display": {
            "metric_missing": "无估值指标数据",
            "normalization_unverified": "合理价值未评估（指标已有，缺正常化盈利验证）",
            "fair_value_assessed": VALUATION_STATUS_LABELS.get(status, "已评估"),
        }[state],
    }


# ---------------------------------------------------------------------------
# Q08（同上路线图 §3）：未校准倾向 ≠ 上涨概率，历史高点 ≠ 目标价
# ---------------------------------------------------------------------------
UNCALIBRATED_PROBABILITY_LABEL = "倾向（未统计校准）"
# 只凭「历史高点/低点」身份出现的价位，没有推导就不能进未来目标区间。
_REFERENCE_BASIS_WORDS = (
    "历史高点", "历史低点", "250日", "年内高点", "年内低点", "收盘高点", "收盘低点",
    "盘中最高", "盘中最低", "前高", "前低", "高点回撤",
)
# 有这些词才算给出了推导路径（倍率×正常化盈利、测算、模型区间等）。
_DERIVATION_WORDS = ("正常化", "倍率", "倍数", "测算", "估值情景", "收益分布", "回撤位", "斐波", "分位")


def scenario_probability_view(scenarios: Any, *, levels: Any = None) -> dict[str, Any]:
    """情景概率与预期区间的**口径视图**（页面与导出共用，Q08 验收「口径一致」）。

    - 概率：`probability_basis` 目前恒为 `none`（无历史后验基准），因此展示词只能是
      「倾向（未统计校准）」，**不得写成上涨概率**；
    - 区间上下界：与关键价位比对，落在历史高点/低点类价位且该情景没给推导依据时，
      标为 `reference`（参考压力/支撑位）并说明不能当目标价。
    """
    rows = [row for row in (scenarios if isinstance(scenarios, list) else []) if isinstance(row, dict)]
    level_rows: list[dict[str, Any]] = []
    if isinstance(levels, dict):
        for group in ("support", "resistance"):
            for row in levels.get(group) or []:
                if isinstance(row, dict):
                    level_rows.append({**row, "kind": group})
    elif isinstance(levels, list):
        level_rows = [row for row in levels if isinstance(row, dict)]

    def bound_kind(price: float | None) -> tuple[str, str]:
        if price is None:
            return ("", "")
        for row in level_rows:
            row_price = _as_float_or_none(row.get("price"))
            basis = str(row.get("basis") or "")
            if row_price is None or abs(row_price - price) > 1e-6:
                continue
            if any(word in basis for word in _REFERENCE_BASIS_WORDS):
                return ("reference", f"{price} 来自「{basis}」，只是历史参考位")
        return ("technical", "")

    view: list[dict[str, Any]] = []
    reference_bounds: list[dict[str, Any]] = []
    for row in rows[:3]:
        name = str(row.get("name") or "情景").strip()
        basis_text = f"{row.get('range_basis') or ''} {row.get('trigger') or ''}"
        has_derivation = any(word in basis_text for word in _DERIVATION_WORDS)
        range_block = row.get("expected_range") if isinstance(row.get("expected_range"), dict) else {}
        bounds: list[dict[str, Any]] = []
        for key in ("low", "high"):
            price = _as_float_or_none(range_block.get(key))
            kind, note = bound_kind(price)
            if kind == "reference" and not has_derivation:
                note += "：该情景未给出推导过程，只作参考压力/支撑位，不作为未来窗口的目标价。"
                reference_bounds.append({"scenario": name, "level": price, "note": note})
            elif kind == "reference":
                note += "：情景已给出推导，可按情景区间使用，但仍属情景而非承诺。"
            bounds.append({"side": key, "value": price, "kind": kind or "declared", "note": note})
        level = str(row.get("probability_level") or "").strip()
        view.append({
            "name": name,
            "probability_level": level,
            "probability_display": f"{level}（未统计校准，非上涨概率）" if level else "未给出",
            "probability_basis": str(row.get("probability_basis") or PROBABILITY_BASIS_NONE),
            "bounds": bounds,
        })
    return {
        "calibrated": False,
        "probability_label": UNCALIBRATED_PROBABILITY_LABEL,
        "note": PROBABILITY_NOTE,
        "scenarios": view,
        "reference_bounds": reference_bounds,
    }



# ---------------------------------------------------------------------------
# v32（2026-09-13 方案 §二.1）：首屏驾驶舱三行分项判断。
# 与 `pillar_rows`（按 claim requires 字段聚合的**支撑计数**）互补：本归一化读的是
# 模型输出的**结论词**（偏强/低估/确认…），两套并列展示——结论卡看词、支撑看数。
# ---------------------------------------------------------------------------

PILLAR_VERDICT_KEYS = ("fundamental", "valuation", "technical")
PILLAR_VERDICT_LABELS: dict[str, str] = {
    "fundamental": "基本面",
    "valuation": "价值估值",
    "technical": "技术状态",
}
PILLAR_VERDICT_VOCAB: dict[str, tuple[str, ...]] = {
    "fundamental": ("偏强", "中性", "偏弱", "无法判断"),
    "valuation": ("低估", "合理", "偏贵", "无法判断"),
    "technical": ("确认", "待确认", "转弱", "无信号"),
}
# 适用期限词表（方案 §二.1：「短线、3～12个月、3～5年」）；全角/连字符写法归一到统一展示。
HORIZON_VOCAB = ("短线", "3~12个月", "3~5年")


def normalize_horizon(value: Any) -> list[str]:
    """适用期限归一：全角波浪线/连字符写法并入统一词表；越界值原样保留（如实展示，不猜测）。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:3]:
        text = str(item).strip().replace("～", "~").replace("－", "-")
        if text in ("3-12个月", "3—12个月", "3至12个月"):
            text = "3~12个月"
        elif text in ("3-5年", "3—5年", "3至5年"):
            text = "3~5年"
        if text and text not in out:
            out.append(text)
    return out


def normalize_pillar_verdicts(value: Any) -> dict[str, Any]:
    """驾驶舱三行分项 {fundamental, valuation, technical, horizon[]}；缺失字段留空（不编造）。"""
    out: dict[str, Any] = {key: "" for key in PILLAR_VERDICT_KEYS}
    out["horizon"] = []
    if not isinstance(value, dict):
        return out
    for key in PILLAR_VERDICT_KEYS:
        out[key] = str(value.get(key, "")).strip()
    out["horizon"] = normalize_horizon(value.get("horizon"))
    return out


def pillar_verdicts_valid(verdicts: dict[str, Any]) -> list[str]:
    """驾驶舱取值越界清单（供告警）；空值不算越界（缺失如实降级，前端不渲染该行）。"""
    problems: list[str] = []
    for key in PILLAR_VERDICT_KEYS:
        text = str(verdicts.get(key) or "")
        if text and text not in PILLAR_VERDICT_VOCAB[key]:
            problems.append(f"{PILLAR_VERDICT_LABELS[key]}={text}")
    return problems


# ---------------------------------------------------------------------------
# v27「可信交付」：证据质量分级 + claim→source 覆盖检查（2026-09-12 方案 §3.1）。
#
# 问题：v24 之后「引用真实存在」已有保证，但「引用是否支撑得住那条判断」无人校验——
# 用一条新闻稿去支撑「主业减亏」，与用财报支撑，在界面上长得一模一样。
#
# 解法：① 给每类来源定等级与**覆盖字段集**；② 让模型为每条核心判断输出
# claim（sources + requires），`requires` 必须取自本模块的 `COVERAGE_VOCAB`（唯一词表，
# 提示词与闸门共用），从而「覆盖是否成立」是**可确定性复算**的集合运算，不是模糊判断。
#
# 等级口径（A 最优）：
#   A 公告原文、财报原文、交易所原始数据；
#   B 公司正式说明、监管披露的二次页面、行情/财务数据的接口分发；
#   C 主流媒体或数据商摘要；
#   D 全市场扫描、传闻、无明确标的指向的情绪信息。
# ★ 诚实声明：**当前没有任何来源达到 A 级**——S1/S4 均经数据商接口二次分发，S3 只有公告标题。
#   等级随接入方式演进（如直连交易所公告原文）时修改 SOURCE_PROFILES 并递增
#   `EVIDENCE_QUALITY_VERSION`，不靠改文案。
# ---------------------------------------------------------------------------

EVIDENCE_QUALITY_VERSION = "v1"

QUALITY_A = "A"
QUALITY_B = "B"
QUALITY_C = "C"
QUALITY_D = "D"

_QUALITY_RANK: dict[str, int] = {QUALITY_A: 4, QUALITY_B: 3, QUALITY_C: 2, QUALITY_D: 1}

# 覆盖字段词表：claim.requires 只允许取自这里；不在词表中的需求**无法校验**（如实标注）。
COVERAGE_VOCAB: tuple[str, ...] = (
    # 技术面（S1）
    "trend", "price_level", "volume", "indicator", "period_return", "drawdown",
    # 新闻（S2）
    "news_event", "market_sentiment",
    # 公告（S3）
    "corporate_action", "disclosure",
    # 财务（S4）
    "revenue", "net_income", "cash_flow", "debt", "margin", "roe", "inventory", "goodwill",
    # J04（桌面端升级路线图 2026-09-18）：单季同比入表——financial_evidence.derive_quarterly_series
    # 本来就算单季 yoy（缺去年同季为 None，不用累计同比冒充单季），随 S4 证据包进入覆盖；
    # 新闻口径（S2）的同比数字不覆盖这两个字段，含同比数字的判断未声明它们时按既有规则降级。
    "net_income_yoy", "revenue_yoy",
    # 估值（S5）
    "pe", "pb", "ps", "valuation_percentile", "peer_comparison", "share_count",
    # —— 以下字段**刻意不挂在任何来源上**：当前证据包无法覆盖，要求它们的判断会被如实降级 ——
    #    segment_profit（分部利润）/ net_income_deducted（扣非净利）：需分部数据或扣非口径，尚未接入。
    "segment_profit", "net_income_deducted",
)

COVERAGE_VOCAB_SET = frozenset(COVERAGE_VOCAB)

# 各来源的 kind / 等级 / 覆盖字段（唯一来源；提示词据此向模型说明「哪些字段由谁覆盖」）。
SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "S1": {
        "kind": "market_data",
        "quality": QUALITY_B,
        "label": "日/周/月 K 线（行情接口，交易所数据的二次分发）",
        "coverage": ("trend", "price_level", "volume", "indicator", "period_return", "drawdown"),
    },
    "S2": {
        "kind": "news",
        "quality": QUALITY_C,
        "label": "近期新闻（媒体摘要口径）",
        "coverage": ("news_event", "market_sentiment"),
    },
    "S3": {
        "kind": "announcement",
        "quality": QUALITY_B,
        "label": "近期公告标题（披露的二次页面，仅标题）",
        "coverage": ("corporate_action", "disclosure"),
    },
    "S4": {
        "kind": "financial",
        "quality": QUALITY_B,
        "label": "财务摘要（F10 归一化行项目）",
        "coverage": (
            "revenue", "net_income", "cash_flow", "debt", "margin", "roe", "inventory", "goodwill",
            # J04：单季同比由 S4 财报数据实算（derive_quarterly_series），新闻口径不覆盖。
            "net_income_yoy", "revenue_yoy",
        ),
    },
    "S5": {
        "kind": "valuation",
        "quality": QUALITY_C,
        "label": "估值快照（数据商口径）",
        "coverage": ("pe", "pb", "ps", "valuation_percentile", "peer_comparison", "share_count"),
    },
    SEAT_SOURCE_ID: {
        "kind": "seat",
        # 方案 §九：东财龙虎榜是交易所披露的**转载**，不是中金所那种一级源 → 标 B。
        # B 意味着可以支撑结论，但局限必须写「转载自东财数据中心，原始披露为沪深交易所」。
        "quality": QUALITY_B,
        "label": "龙虎榜席位（东财数据中心转载自沪深交易所披露）",
        # 刻意**不**把任何「胜率/概率」类字段放进 coverage：数据商的 RISE_PROBABILITY_3DAY
        # 不是本产品算出来的，不能作为判断依据（方案铁律 6）。
        "coverage": (),
    },
}

# 摘要默认只允许 A/B 级证据支撑核心结论；C 级只能作背景；D 级不得单独触发结论。
CORE_SUPPORT_QUALITIES = (QUALITY_A, QUALITY_B)

# J04（桌面端升级路线图 2026-09-18）：单季同比覆盖字段与「同比 + 百分比数字」检测。
# 用于发现「正文报了同比增速、requires 却没挂对应字段」的判断（样报 002468 的 +128.31% 逃过覆盖校验）。
_YOY_COVERAGE_FIELDS = ("net_income_yoy", "revenue_yoy")
_PERCENT_NUMBER_PAT = re.compile(r"\d+(?:\.\d+)?\s*%")

_ISO_DATE = re.compile(r"((?:19|20)\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")


def extract_due_date(value: str) -> str:
    """从验证时点文本里解析**具体日期**（ISO 8601）；只到月份或锚定事件时返回空串。

    只解析出得出确切年月日的写法（`2026-10-31` / `2026年10月31日` / `2026/10/31`）；
    「2026-10 前」「三季报披露后」一律返回空串——不猜一个日期出来充数。
    """
    match = _ISO_DATE.search(value or "")
    if match is None:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def _as_float_or_none(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int_or_none(value: Any) -> int | None:
    """整数计数（同业池只数、分位窗口根数）：bool 与不可解析值一律按「没有」处理。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def source_profile(source_id: str) -> dict[str, Any]:
    """来源的等级/覆盖元数据；未登记来源按 D 级、零覆盖处理（不假装它有内容）。"""
    profile = SOURCE_PROFILES.get(str(source_id))
    if profile is None:
        return {
            "kind": "unknown", "quality": QUALITY_D,
            "label": "未登记来源（无可信度依据）", "coverage": (),
        }
    return profile


def evidence_quality_report(source_keys: Iterable[str]) -> dict[str, Any]:
    """本次证据包的分级明细（前端展示 + 闸门共用），逐来源给出等级与可覆盖字段。"""
    keys = sorted({str(key) for key in source_keys})
    sources = [
        {
            "id": key,
            "kind": source_profile(key)["kind"],
            "quality": source_profile(key)["quality"],
            "label": source_profile(key)["label"],
            "coverage": list(source_profile(key)["coverage"]),
        }
        for key in keys
    ]
    tiers: dict[str, list[str]] = {QUALITY_A: [], QUALITY_B: [], QUALITY_C: [], QUALITY_D: []}
    for item in sources:
        tiers[str(item["quality"])].append(str(item["id"]))
    return {
        "version": EVIDENCE_QUALITY_VERSION,
        "sources": sources,
        "tiers": tiers,
        "coverage_vocab": list(COVERAGE_VOCAB),
        "core_support_qualities": list(CORE_SUPPORT_QUALITIES),
        "note": (
            "A 级（公告/财报/交易所原始数据）当前**尚未接入**：S1/S4 经数据商接口二次分发、"
            "S3 仅公告标题，故按 B 级如实标注；等级口径随接入方式演进并升版本号。"
        ),
    }


# —— v28 二次方案 §2.3「证据支撑链」 ——
#
# v27 已经把「引用是否真实存在」和「引用是否覆盖所需字段」变成确定性集合运算；v28 在其上补三件事：
#   ① 每条判断带上 importance（重要度）与**服务端判定的 support**（full/partial/none）；
#   ② 新增两个**可复算**的质量判据：来源数据是否过期（stale）、同一判断的引用口径是否跨度过大（conflict）；
#   ③ 核心判断拿不到 full 支持时，摘要必须出现「部分支持 / 待核验」降级提示。
#
# 硬边界（与全站口径一致）：
# - `support` 由本模块判定，**不采信模型自述的 support**（模型自评不算证据）；
# - 降级提示是**服务端附加的结构化字段**，与模型原话并列展示，**绝不改写 executive_summary**；
# - ★ 诚实声明：**内容层面的「冲突来源」检测未实现**（需要语义比对，属 P2「多模型冲突矩阵」范畴）。
#   本模块给出的 conflict_signals 只是**口径层面**的确定性判据（等级跨度/单来源/未知来源），
#   在 UI 与导出里如实标注为「口径检查」，不得被当成内容冲突结论。

CLAIM_IMPORTANCE_LEVELS = ("high", "medium", "low")
# J03（桌面端升级路线图 2026-09-18）：核心结论判定口径阈值化（v1 的 supported>0 已废），版本随语义升级。
CLAIM_SUPPORT_VERSION = "v2"

SUPPORT_FULL = "full"
SUPPORT_PARTIAL = "partial"
SUPPORT_NONE = "none"
_SUPPORT_RANK = {SUPPORT_FULL: 2, SUPPORT_PARTIAL: 1, SUPPORT_NONE: 0}
_SUPPORT_LABELS = {SUPPORT_FULL: "完整支持", SUPPORT_PARTIAL: "部分支持", SUPPORT_NONE: "无来源支撑"}

# 枚举字段 → 中文可读标签（只用于展示；闸门判定始终用英文枚举）。
COVERAGE_LABELS: dict[str, str] = {
    "trend": "趋势结构", "price_level": "关键价位", "volume": "量能", "indicator": "技术指标",
    "period_return": "区间涨跌", "drawdown": "回撤",
    "news_event": "新闻事件", "market_sentiment": "市场情绪",
    "corporate_action": "公司行为", "disclosure": "信息披露",
    "revenue": "营业收入", "net_income": "净利润", "cash_flow": "现金流", "debt": "负债",
    "margin": "利润率", "roe": "净资产收益率", "inventory": "存货", "goodwill": "商誉",
    "net_income_yoy": "净利润同比(单季)", "revenue_yoy": "营收同比(单季)",
    "pe": "市盈率", "pb": "市净率", "ps": "市销率", "valuation_percentile": "估值历史分位",
    "peer_comparison": "同业比较", "share_count": "股本",
    "segment_profit": "分部利润", "net_income_deducted": "扣非净利润",
}

# 来源数据时效阈值（天）：超过则标注「来源数据可能已过期」。按来源类别区分——
# 行情看日线停更，新闻/公告是事件驱动，财报以报告期为准天然滞后半年。
# S6 未上榜即不存在，因此没有「过期」概念；列入天数只为格式完整性，
# 且它不会被当作缺口——`CONDITIONAL_SOURCE_IDS` 的来源缺省属正常。
STALENESS_MAX_AGE_DAYS: dict[str, int] = {"S1": 7, "S2": 30, "S3": 30, "S4": 180, "S5": 30, SEAT_SOURCE_ID: 7}
DEFAULT_STALENESS_MAX_AGE_DAYS = 30

CONCLUSION_KEYS = ("statement", "direction", "core_conflict", "confidence")
ALLOWED_CONCLUSION_DIRECTIONS = ("偏多", "偏空", "中性", "多空交织")

# —— v30 四次升级方案（§4 通用情景模型 / §5 证据与降级 / §2.2 告警分级 / §3 结论驾驶舱） ——
#
# 设计边界（与全站口径一致）：
# - `range_status` 由服务端**确定性推导**（数值校验不信任模型自报；模型可声明，越界按实测重判）；
# - `categorized_warnings` 是对既有 warnings 的**确定性展示层分组**，逐条给出影响与建议动作；
# - claims 的 `aspects`（数值事实/样本比较/综合判断三层）只在模型输出时逐层校验；
#   模型未输出 → 空列表，前端按现状整体展示，不假装拆分。

# 执行摘要预算（申通方案 §二.1）：目标 150–165 字，180 为绝对软上限；超限只重写摘要、不重跑全文。
SUMMARY_TARGET_MIN = 150
SUMMARY_TARGET_MAX = 165
SUMMARY_HARD_MAX = 180
SUMMARY_BUDGET_VERSION = "v1"

# 情景预期区间五态（四次方案 §4）：
#   estimated       有计算方法和有效输入（双端数值有效）
#   technical_range 仅有支撑/压力位（技术位推得）
#   unavailable     模型没有可靠范围（缺历史基准等）
#   not_applicable  事件型情景，只能由公告/财报事件验证，无合理价格目标
#   invalid         数据类型或上下界错误（low>=high / 负值）
RANGE_STATUSES = ("estimated", "technical_range", "unavailable", "not_applicable", "invalid")
RANGE_STATUS_LABELS: dict[str, str] = {
    "estimated": "估算区间",
    "technical_range": "技术位区间",
    "unavailable": "暂无区间",
    "not_applicable": "不适用（事件型）",
    "invalid": "未生成（字段校验失败）",
}
_TECH_BASIS_WORDS = ("技术位", "支撑", "压力", "技术")
_EVENT_BASIS_WORDS = ("事件", "公告", "财报", "披露", "分红", "并购")

# 质量告警四级（四次方案 §2.2）：首屏只展开 evidence/computation，format 折叠，blocking 单列。
WARN_CATEGORIES = ("blocking", "evidence", "computation", "format")
WARN_CATEGORY_LABELS: dict[str, str] = {
    "blocking": "阻断错误",
    "evidence": "证据告警",
    "computation": "计算告警",
    "format": "形式告警",
}
# 每类告警的固定影响与建议动作（确定性展示层；message 用原告警文本，不改写）。
WARN_CATEGORY_META: dict[str, dict[str, str]] = {
    "blocking": {
        "impact": "防止虚构引用或损坏数据进入报告（服务端已处理的部分会注明）",
        "action": "核对被剔除项后即可交付；如需引用该来源请补取数后重新生成",
    },
    "evidence": {
        "impact": "相关判断需按「未独立验证」对待，不得当作已证实事实",
        "action": "补充来源、确认口径或接受降级（处理后保留审计记录，不改写模型原文）",
    },
    "computation": {
        "impact": "该字段当前不可直接依赖，需人工核对口径",
        "action": "核对计算口径与输入期间；无法核实时按缺失处理",
    },
    "format": {
        "impact": "不影响结论有效性，影响阅读与跟踪体验",
        "action": "可在「质量详情」中查看；形式问题可在后续修订中处理",
    },
}
WARN_CATEGORY_VERSION = "v1"

# claims 三层命题（申通方案 §二.2 / 四次方案 §5）：一条估值判断实际混合三种命题，
# 数值事实可被 C 级快照支持，样本比较需同业样本可回链，综合判断必须保留「部分支持」。
CLAIM_ASPECT_KINDS = ("fact", "comparison", "judgement")
CLAIM_ASPECT_LABELS: dict[str, str] = {
    "fact": "数值事实",
    "comparison": "样本比较",
    "judgement": "综合判断",
}

# 覆盖字段 → 首屏驾驶舱分项（三次方案 §3「基本面/估值/技术面三行」）。归属确定性可复算；
# 未列入的字段不参与分项（该判断不进任何一行，不强行归类）。
FIELD_PILLAR: dict[str, str] = {
    # 基本面（S4 财务 + S3 公告/公司行为）
    "revenue": "fundamental", "net_income": "fundamental", "cash_flow": "fundamental",
    "debt": "fundamental", "margin": "fundamental", "roe": "fundamental",
    "inventory": "fundamental", "goodwill": "fundamental",
    "segment_profit": "fundamental", "net_income_deducted": "fundamental",
    "corporate_action": "fundamental", "disclosure": "fundamental",
    "news_event": "fundamental",
    # 估值（S5）
    "pe": "valuation", "pb": "valuation", "ps": "valuation",
    "valuation_percentile": "valuation", "peer_comparison": "valuation", "share_count": "valuation",
    # 技术面（S1）+ 市场情绪
    "trend": "technical", "price_level": "technical", "volume": "technical",
    "indicator": "technical", "period_return": "technical", "drawdown": "technical",
    "market_sentiment": "technical",
}
PILLAR_KEYS = ("fundamental", "valuation", "technical")
PILLAR_LABELS: dict[str, str] = {"fundamental": "基本面", "valuation": "估值", "technical": "技术面"}

DECISION_CARD_VERSION = "v4"


def coverage_label(field: str) -> str:
    """覆盖字段的中文标签；未登记字段原样返回（不假装认识它）。"""
    return COVERAGE_LABELS.get(field, field)


def claim_support_from_status(status: str) -> str:
    """`check_claims` 的三态 → 支撑强度三态（对外统一口径）。

    supported → full（引用真实、字段覆盖齐全、等级达 B 及以上）
    downgraded → partial（有真实来源，但覆盖不足或等级偏低）
    no_source → none（引用的来源一个都不在本次真实来源里）
    """
    if status == "supported":
        return SUPPORT_FULL
    if status == "downgraded":
        return SUPPORT_PARTIAL
    return SUPPORT_NONE


def _age_days(as_of: str, today: date) -> int | None:
    """as_of（ISO 日期前缀）距 today 的天数；解析不出返回 None（不猜）。"""
    text = (as_of or "")[:10]
    if len(text) < 10:
        return None
    try:
        return (today - date(int(text[0:4]), int(text[5:7]), int(text[8:10]))).days
    except ValueError:
        return None


def claim_conflict_signals(cited: list[str], *, requires: list[str]) -> list[dict[str, Any]]:
    """同一判断的引用在**口径层面**是否跨度过大（确定性判据，不做内容比对）。

    - `quality_gap`：所引来源的证据等级跨度 ≥ 2 级（如同时引 B 级财报与 D 级未登记来源）；
    - `single_source`：核心字段需求只由单一来源覆盖（缺交叉验证）；
    - `unknown_source`：引用了未登记来源（无可信度依据）。
    """
    signals: list[dict[str, Any]] = []
    ranks = sorted({_QUALITY_RANK.get(str(source_profile(sid)["quality"]), 1) for sid in cited})
    if len(cited) >= 2 and ranks and (ranks[-1] - ranks[0]) >= 2:
        signals.append({
            "kind": "quality_gap",
            "detail": f"所引来源的证据等级跨度 {ranks[-1] - ranks[0]} 级，口径差距较大，需人工确认是否互相矛盾。",
        })
    if len(cited) == 1 and requires:
        signals.append({
            "kind": "single_source",
            "detail": f"覆盖 {'、'.join(coverage_label(f) for f in requires[:3])} 的来源只有 {cited[0]} 一个，缺交叉验证。",
        })
    unknown = [sid for sid in cited if sid not in SOURCE_PROFILES]
    if unknown:
        signals.append({
            "kind": "unknown_source",
            "detail": f"引用了未登记来源（{'、'.join(unknown)}），无可信度依据。",
        })
    return signals


CLAIM_KEYS = (
    "claim_id", "text", "sources", "requires",
    # —— v28 二次方案 §2.3：证据支撑链 ——
    "importance", "confidence", "missing",
)


def normalize_claims(value: Any) -> list[dict[str, Any]]:
    """核心判断清单 [{claim_id, text, sources[], requires[], importance, confidence, missing[]}]（最多 8 条）。

    只做结构归一：文本与来源**原样保留**，`requires` 里不在词表中的字段也保留（由
    `check_claims` 如实标注为「无法校验」而不是悄悄删掉）。

    v28 新增三项（均**只透传模型自述**，不参与判定）：
    - `importance` 重要度（high/medium/low，用于挑出「核心判断」）；
    - `confidence` 模型自评置信度；
    - `missing` 模型自述的证据缺口（服务端还会另算一份 `missing`，两者**并列标注来源**，不互相覆盖）。
    ★ `support`（支撑强度）**不在此处采信**——它由 `check_claims` 确定性判定，模型自评不算证据。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        raw_sources = item.get("sources")
        raw_requires = item.get("requires")
        raw_missing = item.get("missing")
        sources = [str(s).strip() for s in raw_sources if str(s).strip()] if isinstance(raw_sources, list) else []
        requires = [str(r).strip() for r in raw_requires if str(r).strip()] if isinstance(raw_requires, list) else []
        missing = [str(m).strip() for m in raw_missing if str(m).strip()] if isinstance(raw_missing, list) else []
        importance = str(item.get("importance", "")).strip().lower()
        # 编号由服务端兜底（v39 真机缺陷）：模型不吐 claim_id 时，追问闸门（allowed_claim_ids）、
        # 核心支撑点名（「2/3 是哪些」）与降级留痕会同时失去锚点，只剩「—、—、—」。
        claim_id = str(item.get("claim_id", "")).strip()

        # v30 三层命题（申通方案 §二.2）：一条 claim 可拆为数值事实/样本比较/综合判断，
        # 每层用自己的 sources/requires，由 check_claims 逐层独立判定支撑强度。
        # 模型未输出 → 空列表（前端按现状整体展示，不假装拆分）。
        aspects: list[dict[str, Any]] = []
        raw_aspects = item.get("aspects")
        if isinstance(raw_aspects, list):
            for aspect in raw_aspects[:4]:
                if not isinstance(aspect, dict):
                    continue
                kind = str(aspect.get("kind", "")).strip().lower()
                if kind not in CLAIM_ASPECT_KINDS:
                    continue
                aspect_sources_raw = aspect.get("sources")
                aspect_requires_raw = aspect.get("requires")
                aspects.append({
                    "kind": kind,
                    "text": str(aspect.get("text", "")).strip(),
                    "sources": [str(s).strip() for s in aspect_sources_raw if str(s).strip()] if isinstance(aspect_sources_raw, list) else [],
                    "requires": [str(r).strip() for r in aspect_requires_raw if str(r).strip()] if isinstance(aspect_requires_raw, list) else [],
                })

        out.append({
            "claim_id": claim_id,
            "text": str(item.get("text", "")).strip(),
            "sources": sources[:5],
            "requires": requires[:8],
            "importance": importance,
            "confidence": str(item.get("confidence", "")).strip().lower(),
            "missing": missing[:5],
            "aspects": aspects,
        })
    _assign_claim_ids(out)
    return out


def _assign_claim_ids(claims: list[dict[str, Any]]) -> None:
    """就地补齐核心判断编号：缺失或重复的按位置补 `C{n}`，模型给的有效编号原样保留。

    正文用 `[C3]` 引用判断，编号一旦为空，「部分支撑（2/3）」就点不出被统计的是哪几条，
    追问侧也无法再按编号剔除模型自造的判断——所以编号是**服务端职责**，不是模型自评。
    """
    used = {str(row.get("claim_id") or "").strip() for row in claims if str(row.get("claim_id") or "").strip()}
    for index, row in enumerate(claims, start=1):
        current = str(row.get("claim_id") or "").strip()
        if current and current not in [str(other.get("claim_id")) for other in claims[:index - 1]]:
            continue
        candidate = f"C{index}"
        while candidate in used:
            candidate = f"{candidate}*"
        used.add(candidate)
        row["claim_id"] = candidate


def _evaluate_claim_sources(
    sources: list[str],
    requires: list[str],
    *,
    allowed: set[str],
    layer: str = "judgement",
    text: str = "",
) -> dict[str, Any]:
    """单条命题（claim 或其 aspect）的覆盖/等级/三态判定（全部确定性，供 check_claims 与 aspects 复用）。

    `layer`（v30 三层命题，申通方案 §二.2 / 四次方案 §5）决定**证据等级门槛**的适用方式：
    - judgement（默认）：维持 A/B 级门槛——C/D 级只能让综合判断「部分支持」；
    - fact：**低等级来源不应让基础数值事实整体失败**——引用真实 + 覆盖齐全即 full
      （来源等级本身仍如实在 evidence_quality 展示，只是不作事实层的降级依据）；
    - comparison：同业样本明细回链尚未接入，**覆盖齐全也最高 partial**（上限封顶，note 说明）。

    `text`（J04，桌面端升级路线图 2026-09-18）：命题原文——文本同时出现「同比」与百分比数字、
    而 requires 未声明任何单季同比字段（net_income_yoy/revenue_yoy）且所引来源也覆盖不到时，
    按未覆盖降级：同比数字若只来自新闻口径（S2），服务端无法核实其口径与准确性。
    """
    cited = [sid for sid in sources if sid in allowed]
    dropped = [sid for sid in sources if sid not in allowed]
    valid_requires = [field for field in requires if field in COVERAGE_VOCAB_SET]
    unknown = [field for field in requires if field not in COVERAGE_VOCAB_SET]
    covered: set[str] = set()
    for sid in cited:
        covered.update(source_profile(sid)["coverage"])
    missing = [field for field in valid_requires if field not in covered]
    qualities = [str(source_profile(sid)["quality"]) for sid in cited]
    best_quality = max(qualities, key=lambda q: _QUALITY_RANK.get(q, 1)) if qualities else None

    # J04：同比数字未挂单季同比字段 → 覆盖缺口（不入 missing 列表，避免谎称「两个字段都缺」）。
    yoy_undeclared = bool(
        text
        and "同比" in text
        and _PERCENT_NUMBER_PAT.search(text)
        and not (set(_YOY_COVERAGE_FIELDS) & (set(valid_requires) | covered))
    )

    apply_quality_gate = layer == "judgement"
    support_cap = SUPPORT_PARTIAL if layer == "comparison" else None

    notes: list[str] = []
    if not cited:
        status = "no_source"
        notes.append("引用的来源均不属于本次真实取到的来源，该判断无来源支撑")
    elif missing or yoy_undeclared:
        status = "downgraded"
        if missing:
            notes.append(
                f"所引来源未覆盖 {'/'.join(coverage_label(f) for f in missing)}，该判断降级为「未独立验证」（原始表述保留）"
            )
        if yoy_undeclared:
            notes.append(
                "判断文本含同比数字，但 requires 未声明单季同比字段（"
                f"{coverage_label('net_income_yoy')} / {coverage_label('revenue_yoy')} 任一），"
                "同比口径无法核实（若来自新闻口径则未经财报数据验证），该判断降级为「未独立验证」（原始表述保留）"
            )
    elif apply_quality_gate and best_quality is not None and best_quality not in CORE_SUPPORT_QUALITIES:
        status = "downgraded"
        notes.append(
            f"最高证据等级仅 {best_quality} 级（核心结论需 {'/'.join(CORE_SUPPORT_QUALITIES)} 级），"
            "该判断降级为「公司口径，未独立验证」（原始表述保留）"
        )
    else:
        status = "supported"
    if support_cap is not None and status == "supported":
        status = "downgraded"
        notes.append("同业样本明细回链尚未接入，样本比较层最高「部分支持」（接入回链后自动升级）。")
    if dropped:
        notes.append(f"剔除不属于本次来源的引用：{'、'.join(dropped)}")
    if unknown:
        notes.append(f"字段需求 {'/'.join(unknown)} 不在覆盖词表内，无法校验（未据其判定）")
    return {
        "cited": cited, "dropped": dropped, "valid_requires": valid_requires,
        "unknown": unknown, "missing": missing, "best_quality": best_quality,
        "status": status, "notes": notes,
    }


def check_claims(
    claims: Iterable[dict[str, Any]],
    *,
    allowed: set[str],
    source_asof: dict[str, str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """逐条校验 claim 的引用是否覆盖其所需字段、证据等级是否够支撑核心结论。

    判定（全部可确定性复算）：
    - `no_source`   —— 该判断引用的来源一个都不在本次真实取到的来源里（同「假引用」口径）；
    - `downgraded`  —— 引用的来源真实存在，但 ① 未覆盖 `requires` 中某个字段，或
                       ② 最高等级低于 B（只有 C/D 级证据）→ 降级为「未独立验证」；
    - `supported`   —— 引用真实且覆盖齐全、等级达 B 及以上。

    v28（二次方案 §2.3）在其上补三件事，**全部只标注、不改写**：
    - `support`（full/partial/none）+ `importance`（模型自述，用于挑「核心判断」）+ 服务端算出的
      `missing`（可读缺口）与模型自述的 `model_missing` 并列；
    - `stale`：所引来源的数据日期距今超过该来源类别的时效阈值（需调用方提供 `source_asof` 与 `today`；
      两者缺任一时**不做该项判定**并如实标注 unavailable，不拿未知当通过）；
    - `conflict_signals`：**口径层面**的跨度判据（见 `claim_conflict_signals`）。

    硬边界：本函数**只标注与降级，不改写、不删除模型原话**；是否阻断交付由调用方决定。
    """
    asof = source_asof or {}
    findings: list[dict[str, Any]] = []
    downgraded: list[str] = []
    no_source: list[str] = []
    unsupported_fields: list[str] = []
    unknown_requires: list[str] = []
    stale_hits: list[str] = []
    core_downgraded: list[str] = []

    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        # 主命题判定（提取为 _evaluate_claim_sources，供三层 aspects 复用同一口径）。
        evaluated = _evaluate_claim_sources(claim["sources"], claim["requires"], allowed=allowed, text=str(claim.get("text", "")))
        cited = evaluated["cited"]
        dropped = evaluated["dropped"]
        valid_requires = evaluated["valid_requires"]
        unknown = evaluated["unknown"]
        missing = evaluated["missing"]
        best_quality = evaluated["best_quality"]
        status = evaluated["status"]
        notes = list(evaluated["notes"])
        if not cited:
            no_source.append(claim_id)
        elif missing:
            downgraded.append(claim_id)
            unsupported_fields.extend(missing)
        elif best_quality is not None and best_quality not in CORE_SUPPORT_QUALITIES:
            downgraded.append(claim_id)
        if unknown:
            unknown_requires.extend(unknown)

        # —— v28 数据时效：来源数据的日期距今是否超阈值（不做该判定时如实标注 unavailable）——
        stale_notes: list[str] = []
        if today is None or not asof:
            staleness_available = False
        else:
            staleness_available = True
            for sid in cited:
                limit = STALENESS_MAX_AGE_DAYS.get(sid, DEFAULT_STALENESS_MAX_AGE_DAYS)
                age = _age_days(asof.get(sid, ""), today)
                if age is None:
                    continue
                if age > limit:
                    stale_notes.append(
                        f"{sid} 来源数据最新日期距今 {age} 天（{sid} 的时效上限 {limit} 天），该来源可能已过期"
                    )
        if stale_notes:
            stale_hits.append(claim_id)
            notes.extend(stale_notes)

        support = claim_support_from_status(status)
        importance = str(claim.get("importance") or "")
        if importance == "high" and support != SUPPORT_FULL:
            core_downgraded.append(claim_id)

        # v30 三层命题逐层判定（申通方案 §二.2）：数值事实/样本比较/综合判断各自用自己的
        # sources/requires 独立定级——C 级快照可以支持 PE 数值事实，却不自动支持「整体便宜」的综合判断。
        # 模型未输出 aspects → 空列表（不假装拆分）。同层派生指标公式留痕见 quarterly（financial_evidence）。
        aspect_findings: list[dict[str, Any]] = []
        for aspect in claim.get("aspects") or []:
            a_eval = _evaluate_claim_sources(aspect["sources"], aspect["requires"], allowed=allowed, layer=aspect["kind"], text=str(aspect.get("text", "")))
            aspect_findings.append({
                "kind": aspect["kind"],
                "kind_label": CLAIM_ASPECT_LABELS[aspect["kind"]],
                "text": aspect["text"],
                "sources": a_eval["cited"],
                "dropped_sources": a_eval["dropped"],
                "requires": a_eval["valid_requires"],
                "missing_coverage": a_eval["missing"],
                "evidence_quality": a_eval["best_quality"],
                "status": a_eval["status"],
                "support": claim_support_from_status(a_eval["status"]),
                "support_label": _SUPPORT_LABELS[claim_support_from_status(a_eval["status"])],
                "note": "；".join(a_eval["notes"]),
            })

        # 服务端算出的缺口（可读）+ 模型自述缺口，两者并列，互不覆盖。
        server_missing = [
            f"所引来源未覆盖：{coverage_label(field)}（{field}）" for field in missing
        ]
        if best_quality is not None and best_quality not in CORE_SUPPORT_QUALITIES and not missing:
            server_missing.append(f"最高证据等级仅 {best_quality} 级，核心结论需 {'/'.join(CORE_SUPPORT_QUALITIES)} 级")
        server_missing.extend(stale_notes)

        findings.append({
            "claim_id": claim_id,
            "text": claim["text"],
            "sources": cited,
            "dropped_sources": dropped,
            "requires": valid_requires,
            "unknown_requires": unknown,
            "missing_coverage": missing,
            "evidence_quality": best_quality,
            "status": status,
            "note": "；".join(notes),
            # —— v28 证据支撑链 ——
            "importance": importance,
            "confidence": str(claim.get("confidence") or ""),
            "support": support,
            "support_label": _SUPPORT_LABELS[support],
            "missing": server_missing,
            "model_missing": list(claim.get("missing") or []),
            "stale": bool(stale_notes),
            "staleness_checked": staleness_available,
            "conflict_signals": claim_conflict_signals(cited, requires=valid_requires),
            # v30 三层命题（模型未输出时为空列表，前端按现状整体展示）
            "aspects": aspect_findings,
        })

    warnings: list[str] = []
    if no_source:
        warnings.append(
            f"{len(no_source)} 条核心判断引用的来源均不存在于本次证据包（{'、'.join(no_source)}）——"
            "防「用缺失来源支撑结论」。"
        )
    if downgraded:
        warnings.append(
            f"{len(downgraded)} 条核心判断因覆盖不足或证据等级偏低被降级为「未独立验证」"
            f"（{'、'.join(downgraded)}）。"
        )
    if unsupported_fields:
        unique_fields = sorted(set(unsupported_fields))
        warnings.append(
            f"有判断要求当前证据包无法覆盖的字段（{'、'.join(coverage_label(f) for f in unique_fields)}），"
            "这类结论只能作为待核验线索，不得当作已证实事实。"
        )
    if unknown_requires:
        warnings.append(
            f"判断中出现不在覆盖词表内的字段需求（{'、'.join(sorted(set(unknown_requires)))}），无法校验。"
        )
    if stale_hits:
        warnings.append(
            f"{len(stale_hits)} 条核心判断所引来源的数据日期已超过该来源类别的时效上限"
            f"（{'、'.join(stale_hits)}）——结论建立在可能过期的数据上，需重新取数确认。"
        )

    total = len(findings)
    supported = sum(1 for item in findings if item["status"] == "supported")
    partial = sum(1 for item in findings if item["support"] == SUPPORT_PARTIAL)
    none_count = sum(1 for item in findings if item["support"] == SUPPORT_NONE)
    # J03（桌面端升级路线图 2026-09-18）：「有支撑」阈值化——核心判断（importance=high）
    # **全数支撑**才算「有支撑」；部分支撑必须带分母（N/M），禁止用非核心条数撑住判定
    # （旧口径 `supported > 0` 让 1/6 与 4/6 显示完全相同）。模型一个核心标记都没给时，
    # 全部 claims 视为核心（保守兜底：只会让「有支撑」更难达成，不猜该挑哪几条）。
    core_items = [item for item in findings if str(item.get("importance") or "") == "high"] or findings
    core_total = len(core_items)
    core_supported = sum(1 for item in core_items if item["support"] == SUPPORT_FULL)
    if total == 0:
        core_state: str | None = None
    elif core_supported == core_total:
        core_state = "supported"
    elif core_supported > 0:
        core_state = "partial"
    else:
        core_state = "unsupported"
    return {
        "claims": findings,
        "total": total,
        "supported": supported,
        "downgraded": downgraded,
        "no_source": no_source,
        # 核心结论三态（J03）：supported=核心全数支撑 / partial=核心部分支撑（分母见
        # core_support_counts）/ unsupported=核心无一支撑；没有输出 claim 时 None（不假装通过）。
        "core_conclusion_state": core_state,
        "core_support_counts": {"supported": core_supported, "total": core_total},
        "core_support_note": (
            "核心口径：importance=high 的判断全数支撑才算「有支撑」（支撑=覆盖齐全且等级达标）；"
            "模型未标 importance 时全部判断视为核心；部分支撑以「部分支撑（N/M）」呈现。"
        ),
        # 兼容旧布尔消费方：True 仅当核心全数支撑（旧 `supported > 0` 口径已废）。
        "core_conclusion_supported": (None if core_state is None else core_state == "supported"),
        "warnings": warnings,
        # —— v28 证据支撑链汇总（口径可复算）——
        "version": CLAIM_SUPPORT_VERSION,
        "support_counts": {SUPPORT_FULL: supported, SUPPORT_PARTIAL: partial, SUPPORT_NONE: none_count},
        "core_claims": [item["claim_id"] for item in findings if item["importance"] == "high"],
        "core_downgraded": core_downgraded,
        "stale_claims": stale_hits,
        "staleness_checked": today is not None and bool(asof),
        "conflict_note": (
            "口径检查：conflict_signals 只比对证据等级跨度/来源数量/来源是否登记，"
            "**不做内容层面的矛盾检测**（内容冲突比对属 P2「多模型冲突矩阵」，本期未实现）。"
        ),
    }


# ---------------------------------------------------------------------------
# v28 二次方案 §2.1「首屏结论卡」：报告首屏只放五行——
# 结论 / 证据强弱 / 最大支持 / 最大反证 / 下一动作。
#
# 设计边界：
# - 本卡片是**聚合视图**，不产生新的判断：每个字段都能追到 v27/v28 已有的结构化字段
#   （evidence_quality / claim_findings / counter_check / watchpoints / valuation）；
# - 取不到就如实为 None 并给出 reason，**不拿占位文字充数**（如「暂无」必须带原因）；
# - 结论一句话由模型输出（`conclusion`），direction 越界只告警、不改写。
# ---------------------------------------------------------------------------

_QUALITY_TO_CONFIDENCE = {QUALITY_A: "高", QUALITY_B: "高", QUALITY_C: "中", QUALITY_D: "低"}


def categorize_warnings(warnings: list[str]) -> list[dict[str, Any]]:
    """把既有 warnings 按四级**确定性分组**（四次方案 §2.2），每条带影响与建议动作。

    规则（关键词匹配，按 blocking → evidence → computation → format 顺序首个命中生效；
    全部不中 → format）。这是展示层分组：message 保留原告警全文，不改写、不拆分；
    `handled` 恒为 False（处理动作留痕属 review_action 闭环，本期只读）。
    """
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("blocking", ("剔除", "虚构引用", "均不存在于本次", "假引用")),
        ("evidence", ("降级", "未独立验证", "时效", "过期", "无来源", "覆盖", "等级", "来源数据", "证据")),
        ("computation", ("区间", "口径", "概率", "验证点", "时点", "机读")),
    )
    out: list[dict[str, Any]] = []
    for message in warnings:
        category = "format"
        for candidate, keywords in rules:
            if any(word in message for word in keywords):
                category = candidate
                break
        meta = WARN_CATEGORY_META[category]
        out.append({
            "category": category,
            "category_label": WARN_CATEGORY_LABELS[category],
            "message": message,
            "impact": meta["impact"],
            "action": meta["action"],
            "handled": False,
        })
    return out


def pillar_rows(claim_findings: dict[str, Any]) -> list[dict[str, Any]]:
    """首屏驾驶舱的三行分项（基本面/估值/技术面，申通方案 §四.3 + 四次方案 §3）。

    归属规则（确定性可复算）：每条 claim 的 requires 字段按 FIELD_PILLAR 归柱，
    claim 归入**命中字段最多的柱**；无任何命中字段的判断不进分项（不强行归类）。
    每行给出：判断数、各支撑强度计数、代表判断（importance 最高的那条的原文截断）。
    模型未输出 claims → 返回空列表（前端分项区整体不渲染，不冒充有分项）。
    """
    findings = claim_findings.get("claims") if isinstance(claim_findings, dict) else []
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in PILLAR_KEYS}
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        scores = {key: 0 for key in PILLAR_KEYS}
        for field in finding.get("requires") or []:
            pillar = FIELD_PILLAR.get(str(field))
            if pillar:
                scores[pillar] += 1
        best = max(scores, key=lambda key: scores[key])
        if scores[best] == 0:
            continue
        buckets[best].append(finding)
    rows: list[dict[str, Any]] = []
    for pillar in PILLAR_KEYS:
        items = buckets[pillar]
        if not items:
            continue
        importance_rank = {"high": 2, "medium": 1, "low": 0}
        representative = max(
            items,
            key=lambda item: importance_rank.get(str(item.get("importance") or ""), 0),
        )
        rows.append({
            "pillar": pillar,
            "label": PILLAR_LABELS[pillar],
            "total": len(items),
            "full": sum(1 for item in items if item.get("support") == SUPPORT_FULL),
            "partial": sum(1 for item in items if item.get("support") == SUPPORT_PARTIAL),
            "none": sum(1 for item in items if item.get("support") == SUPPORT_NONE),
            "representative": str(representative.get("text") or "")[:60],
        })
    return rows


def normalize_conclusion(value: Any) -> dict[str, str]:
    """一句话结论 {statement, direction, core_conflict, confidence}；字段缺失一律留空（不编造）。"""
    if not isinstance(value, dict):
        return {key: "" for key in CONCLUSION_KEYS}
    return {key: str(value.get(key, "")).strip() for key in CONCLUSION_KEYS}


def core_min_quality(claim_findings: dict[str, Any]) -> str | None:
    """核心判断中**最低**的证据等级（保守口径：一条弱则整体按弱展示）。

    核心判断 = importance 标为 high 的那些；若模型一条都没标 importance，则取全部判断
    （宁可按全部算低等级，也不假装「没有核心判断」）。无判断时返回 None。
    """
    claims = list(claim_findings.get("claims") or [])
    if not claims:
        return None
    core = [item for item in claims if str(item.get("importance")) == "high"] or claims
    ranks = [_QUALITY_RANK.get(str(item.get("evidence_quality")), 0) for item in core]
    if not ranks:
        return None
    lowest = min(ranks)
    if lowest <= 0:
        return None
    return next((tier for tier, rank in _QUALITY_RANK.items() if rank == lowest), None)


def summary_downgrade_notes(claim_findings: dict[str, Any]) -> list[str]:
    """核心判断未获 full 支持时，摘要旁**必须**出现的降级提示（二次方案 §2.3）。

    实现边界：**不改写 executive_summary 一个字符**——提示以结构化字段随报告返回，
    由前端/导出与模型原话**并列展示**（全站「绝不改写模型原话」口径优先于「自动加字」）。
    """
    notes: list[str] = []
    claims = list(claim_findings.get("claims") or [])
    core = [item for item in claims if str(item.get("importance")) == "high"]
    targets = core or claims
    partial = [item for item in targets if str(item.get("support")) == SUPPORT_PARTIAL]
    none_supported = [item for item in targets if str(item.get("support")) == SUPPORT_NONE]
    if partial:
        names = "、".join(str(item.get("claim_id") or item.get("text") or "")[:24] for item in partial[:3])
        notes.append(f"摘要口径：{len(partial)} 条判断仅获**部分支持**（{names}），结论待核验。")
    if none_supported:
        names = "、".join(str(item.get("claim_id") or item.get("text") or "")[:24] for item in none_supported[:3])
        notes.append(f"摘要口径：{len(none_supported)} 条判断**无来源支撑**（{names}），相关内容不得当作已证实结论。")
    # v28 §2.3：来源数据**时效**也进摘要提示——支撑再强，引用的数据过期则结论有效性存疑。
    stale = [item for item in targets if item.get("stale")]
    if stale:
        names = "、".join(str(item.get("claim_id") or item.get("text") or "")[:24] for item in stale[:3])
        notes.append(
            f"摘要口径：{len(stale)} 条判断引用的来源数据已超出该来源的时效阈值（{names}），"
            "结论可能滞后于最新情况。"
        )
    if not claims:
        notes.append("摘要口径：模型未输出核心判断清单（claims），无法判定结论支撑度，按「未经核验」对待。")
    return notes


def quality_blockers_of(
    *,
    missing_sections: list[str],
    report_chars: int,
    kept_citations: list[str],
    claim_findings: dict[str, Any],
) -> list[dict[str, Any]]:
    """阻断项清单（§2.3 顺序：章节 → 正文 → 引用 → claims；§八.4：不含任何字数阈值）。

    每条 {code, section?, message}：code 供前端/统计定位，message 可直接展示。
    阻断 ≠ 丢弃：incomplete 报告仍可作为草稿留痕（§八.4），只是不得标记为「完成」。
    """
    blockers: list[dict[str, Any]] = []
    for name in missing_sections:
        blockers.append({
            "code": "missing_section",
            "section": name,
            "message": f"缺少必需小节「{name}」（未生成，或仅有标题没有实质内容），报告不完整。",
        })
    if report_chars < EMPTY_REPORT_CHARS:
        blockers.append({
            "code": "report_empty",
            "message": "模型未返回有效研报正文（正文为空或只有零星文字），报告不完整。",
        })
    if not kept_citations:
        blockers.append({
            "code": "no_citations",
            "message": "剔除不属于本次来源的引用后无任何真实引用，按「无引用不发布」处理（ADR-0006）。",
        })
    claims = claim_findings.get("claims") or []
    none_count = (claim_findings.get("support_counts") or {}).get(SUPPORT_NONE, 0)
    if len(claims) > 0 and none_count == len(claims):
        blockers.append({
            "code": "claims_all_no_source",
            "message": f"全部 {len(claims)} 条核心判断均无来源支撑，证据链不成立。",
        })
    return blockers


def _watchpoint_next_action(watchpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    """下一动作 = 最近的验证点：优先取能解析出确切日期的最早一条，否则取第一条锚定事件。"""
    if not watchpoints:
        return None
    dated = [item for item in watchpoints if item.get("due_on")]
    target = min(dated, key=lambda item: str(item["due_on"])) if dated else watchpoints[0]
    return {
        "signal": str(target.get("signal", "")),
        "verify_by": str(target.get("verify_by", "")),
        "due_on": str(target.get("due_on", "")),
        "expected_if_true": str(target.get("expected_if_true", "")),
        # 无确切日期 = 锚定事件（到期由事件触发，不猜日期）。
        "event_anchored": not bool(target.get("due_on")),
        "status": str(target.get("status", "")),
    }


def decision_card(
    *,
    symbol: str,
    conclusion: dict[str, str],
    evidence_quality: dict[str, Any],
    claim_findings: dict[str, Any],
    counter_check: dict[str, Any] | None,
    watchpoints: list[dict[str, Any]],
    valuation: dict[str, Any],
    peer_count: int | None = None,
    pillar_verdicts: Any = None,
    research_completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """首屏结论卡：结论 / 证据强弱 / 最大支持 / 最大反证 / 下一动作（纯聚合，零新判断）。

    `peer_count` = 估值快照里的同业样本数（来自 S5 取数结果，非模型自述）；
    取得到就按二次方案 §2.1 的示例拼进标签（「偏贵｜中等可信｜…｜同业样本 14 只」），
    取不到则如实不展示该项。
    `research_completeness`（Q19）= 调用方**先**用 `research_completeness()` 算好的完成度视图
    （反方检查执行后可含更强判据，故由调用方传入而非在此重算）；未提供时为 None。
    """
    tiers = evidence_quality.get("tiers") or {}
    counts = {tier: len(tiers.get(tier) or []) for tier in (QUALITY_A, QUALITY_B, QUALITY_C, QUALITY_D)}
    total_sources = sum(counts.values())
    support_counts = claim_findings.get("support_counts") or {}
    min_quality = core_min_quality(claim_findings)

    verdict = str(valuation.get("verdict", "") or "")
    valuation_label = ""
    valuation_note = ""
    if verdict:
        basis = str(valuation.get("basis") or valuation.get("peer_position") or "").strip()
        # 可信度取自估值来源（S5）的登记等级，不写死——等级表变则展示随之变。
        confidence = _QUALITY_TO_CONFIDENCE.get(str(source_profile("S5")["quality"]), "未知")
        parts = [verdict, f"{confidence}可信"]
        if basis:
            parts.append(basis)
        if isinstance(peer_count, int) and peer_count > 0:
            parts.append(f"同业样本 {peer_count} 只")
        valuation_label = "｜".join(parts)
        if not (isinstance(peer_count, int) and peer_count > 0):
            valuation_note = "同业样本数未取到（S5 快照未给出 peer_count），故标签不展示「样本 N 只」。"

    check = counter_check or {}
    counter_ok = bool(check.get("ok"))
    counter_reason = ""
    max_support = (
        {
            "text": str(check.get("strongest_support", "")),
            "source": str(check.get("strongest_support_source", "")),
            "as_of": str(check.get("strongest_support_date", "")),
        }
        if counter_ok and check.get("strongest_support") else None
    )
    max_counter = (
        {
            "text": str(check.get("strongest_counter", "")),
            "source": str(check.get("strongest_counter_source", "")),
            "as_of": str(check.get("strongest_counter_date", "")),
        }
        if counter_ok and check.get("strongest_counter") else None
    )
    if not counter_ok:
        counter_reason = str(check.get("detail") or "本次未执行反方检查。")

    return {
        "version": DECISION_CARD_VERSION,
        "symbol": symbol,
        "conclusion": {
            "statement": conclusion.get("statement", ""),
            "direction": conclusion.get("direction", ""),
            "core_conflict": conclusion.get("core_conflict", ""),
            "confidence": conclusion.get("confidence", ""),
        },
        "evidence_strength": {
            "counts": counts,
            "total_sources": total_sources,
            "core_min_quality": min_quality,
            "support_counts": support_counts,
            "core_conclusion_supported": claim_findings.get("core_conclusion_supported"),
            # J03：首屏结论卡同样带三态与分母，屏显与导出统一为「部分支撑（N/M）」形态。
            "core_conclusion_state": claim_findings.get("core_conclusion_state"),
            "core_support_counts": claim_findings.get("core_support_counts"),
            "note": (
                "证据强弱 = 本次证据包的 A–D 分级来源数量，以及核心判断的**最低**证据等级"
                "（保守口径：一条弱则整体按弱展示）。"
            ),
        },
        "valuation_label": valuation_label,
        "valuation_label_note": valuation_note,
        # v32 正常化估值状态（方案 §二.3）：四态结论随结论卡下发，前端不再从 verdict 文本猜测。
        "valuation_status": str(valuation.get("valuation_status", "") or ""),
        "valuation_status_label": VALUATION_STATUS_LABELS.get(str(valuation.get("valuation_status", "") or ""), ""),
        "valuation_status_note": str(valuation.get("valuation_status_note", "") or ""),
        "normalized_earnings": valuation.get("normalized_earnings") or {"value": None, "basis": "", "confidence": ""},
        "max_support": max_support,
        "max_counter": max_counter,
        "counter_check_reason": "" if counter_ok else counter_reason,
        "next_action": _watchpoint_next_action(watchpoints),
        "downgrade_notes": summary_downgrade_notes(claim_findings),
        # v30：首屏驾驶舱三行分项（基本面/估值/技术面，按 requires 字段归类聚合，纯推导可复算；
        # 模型未输出 claims 时为空列表，前端分项区整体不渲染）。
        "pillar_rows": pillar_rows(claim_findings),
        # v32：驾驶舱三行**结论词**（模型输出的偏强/低估/确认…，归一后随卡下发；
        # 模型未输出时各行为空串，前端按行降级不渲染，不冒充有分项结论）。
        "cockpit": normalize_pillar_verdicts(pillar_verdicts),
        # Q19（P2）：四项关键研究项完成度随卡下发（调用方未提供时为 None，前端不渲染该项）。
        "research_completeness": (
            research_completeness if isinstance(research_completeness, dict) else None
        ),
        "research_completeness_note": (
            ""
            if isinstance(research_completeness, dict)
            else "本次未提供研究完成度视图（旧链路或未接 Q19），首屏不以研究完成度作判断。"
        ),
    }


# ---------------------------------------------------------------------------
# Q19（2026-09-19 研报与追问质量路线图 §5 P2）：深研「研究完成度」——四项关键研究项，字数只作辅助
# ---------------------------------------------------------------------------
# 症状（申通快递 002468 深研版样报，2026-09-19 18:10）：正文 1793 字 < 深研版下限 3500 字，
# 闸门只回一条字数告警；真正的问题——盈利增长只有「H1 归母净利 10.35 亿 vs 4.53 亿」一句、
# 没有业务量/价格/成本效率/低基数/非经常性损益拆解，现金流只给 2.63 倍覆盖就下「盈利质量较好」，
# PE(TTM) 1.7% 分位与 PB 66.9% 分位的分化无人解释——在 warnings 里一个字都看不见。
#
# 铁律（与全站一致）：
# - 完成度**只由 payload 里可复算的结构化字段 + 正文探针确定性推导**，不采信模型自评的「已拆解」；
# - 探针命不中就说「缺失」，不猜模型「大概写了」；结构化块缺字段时按缺口如实标注，不补数；
# - 字数**只作辅助提示**（`word_count_advisory=True`）：长而缺关键论证者不完整，
#   短而四项齐备者不因篇幅被判无效（§5 Q19 验收）。
# ---------------------------------------------------------------------------

RESEARCH_COMPLETENESS_VERSION = "v1"
COMPLETENESS_DONE = "done"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_MISSING = "missing"
COMPLETENESS_STATE_LABELS: dict[str, str] = {
    COMPLETENESS_DONE: "已完成",
    COMPLETENESS_PARTIAL: "部分完成",
    COMPLETENESS_MISSING: "缺失",
}
COMPLETENESS_ITEM_KEYS: tuple[str, ...] = (
    "earnings_decomposition",
    "cash_flow_and_capex",
    "valuation_explanation",
    "scenarios_and_counter_evidence",
)
COMPLETENESS_ITEM_LABELS: dict[str, str] = {
    "earnings_decomposition": "盈利增长拆解",
    "cash_flow_and_capex": "现金流与资本开支",
    "valuation_explanation": "估值分化解释",
    "scenarios_and_counter_evidence": "情景与反证",
}

# 盈利增长的五个拆解维度（§5 Q16 同一词表；探针用于「正文写了但没进结构化块」的情形）。
EARNINGS_FACTOR_LABELS: dict[str, str] = {
    "volume": "业务量",
    "price": "价格",
    "cost_efficiency": "成本效率",
    "low_base": "低基数",
    "non_recurring": "非经常性损益",
}
_EARNINGS_FACTOR_PROBES: dict[str, tuple[str, ...]] = {
    "volume": ("业务量", "件量", "票量", "单量", "完成业务量", "吞吐量", "运送量", "销量"),
    "price": ("单票收入", "单价", "票价", "客单价", "均价", "提价", "价格战", "单件收入"),
    "cost_efficiency": ("单票成本", "成本率", "费用率", "毛利率", "单位成本", "自动化", "产能利用率", "人效", "分拣"),
    "low_base": ("低基数", "上年同期", "去年基数", "基数效应", "去年同季", "同期基数"),
    "non_recurring": ("非经常性", "一次性损益", "政府补助", "投资收益", "资产处置", "公允价值变动", "扣非"),
}

# 现金流质量的探针：只有「经营现金流 ÷ 净利」这一项不足以判盈利质量（§5 Q17）。
_CASH_FLOW_PROBES: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": ("经营现金流", "经营性现金流", "经营活动现金流", "OCF"),
    "depreciation_amortization": ("折旧", "摊销", "折旧摊销"),
    "working_capital": ("营运资本", "运营资本", "净营运资金", "应收账款", "经营性应收", "应付账款", "存货变动"),
    "capex": ("资本开支", "资本性支出", "购建固定资产", "投资支出"),
    "free_cash_flow": ("自由现金流",),
}
_CASH_FLOW_LABELS: dict[str, str] = {
    "operating_cash_flow": "经营现金流",
    "depreciation_amortization": "折旧摊销",
    "working_capital": "营运资本变动",
    "capex": "资本开支",
    "free_cash_flow": "自由现金流",
}
# Q17 结构化块的字段名探针（键名小写包含式匹配，避免与 Q17 模块的字段命名强耦合）。
_CASH_FLOW_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    # 精确别名表（不做前缀猜测）：`capex_period` / `free_cash_flow_derived` 都不是数值本身。
    "operating_cash_flow": ("operating_cash_flow", "ocf"),
    "depreciation_amortization": ("depreciation_amortization", "depreciation", "amortization"),
    "working_capital": ("working_capital", "working_capital_change"),
    "capex": ("capex", "capital_expenditure"),
    "free_cash_flow": ("free_cash_flow", "fcf"),
}
# 现金流口径的两类分量（v39 真机缺陷修订）：只有「解释项」能支撑盈利质量结论，
# 「派生项」是从经营现金流算出来的，不增加解释力。
_CASH_FLOW_EXPLANATORY = ("depreciation_amortization", "working_capital")
_CASH_FLOW_DERIVED = ("capex", "free_cash_flow")

# 估值分化解释（§5 Q18）：PE/PB 分化要用 ROE / 盈利周期 / 净资产口径解释，可比样本与剔除规则要交代。
_VALUATION_EXPLAIN_PROBES: dict[str, tuple[str, ...]] = {
    "percentile": ("分位", "百分位", "历史区间", "近5年", "近十年"),
    "divergence": ("分化", "背离", "不一致", "相悖", "一个低一个高"),
    "roe": ("ROE", "净资产收益率", "净资产回报"),
    "equity_basis": ("每股净资产", "净资产口径", "MRQ", "BPS", "权益口径", "商誉", "定增", "回购注销"),
    "cycle": ("周期", "盈利高点", "利润高点", "景气", "底部", "反转", "正常化"),
    "comparables": ("可比公司", "同业样本", "样本", "剔除", "中位数", "可比口径", "商业模式"),
    "low_pe_caution": ("不等于安全边际", "不等于低估", "不代表低估", "不构成安全边际", "不是安全边际"),
}
_VALUATION_EXPLAIN_LABELS: dict[str, str] = {
    "roe": "ROE/净资产回报口径",
    "equity_basis": "净资产口径",
    "cycle": "盈利周期位置",
}


def _payload_prose(payload: dict[str, Any]) -> str:
    """完成度探针语料：正文 + 摘要 + 一句话结论 + 各条核心判断原文（只看模型实际写了什么）。"""
    chunks: list[str] = [
        str(payload.get("report") or ""),
        str(payload.get("executive_summary") or ""),
        str(payload.get("conclusion", {}).get("statement") or "")
        if isinstance(payload.get("conclusion"), dict) else "",
    ]
    claims = payload.get("claims")
    if isinstance(claims, list):
        for item in claims:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)


def _prose_hits(text: str, probes: dict[str, tuple[str, ...]]) -> list[str]:
    """探针命中的维度（保持 probes 的声明顺序，可复算）。"""
    return [key for key, words in probes.items() if any(word in text for word in words)]


def _structured_rows(value: Any) -> list[dict[str, Any]]:
    """结构化拆解块归一：既接受 list[dict]，也接受 {"factors": [...]} / {"items": [...]}。"""
    if isinstance(value, dict):
        for key in ("factors", "items", "rows", "components"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _block_field_names(block: Any, probes: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """按**精确键名（含别名）**取块里的字段值：`{"roe": None}` 就是 roe 没值。

    早先是子串匹配，于是 `free_cash_flow_derived: False` 被当成 `free_cash_flow` 有值、
    `capex_period: "2026H1"` 被当成 `capex` 有值——同一份导出里「自由现金流 未取到」与
    「已覆盖 自由现金流」就是这么同时出现的。别名表由调用方列全，不做前缀猜测。
    """
    if not isinstance(block, dict):
        return {}
    lowered = {str(key).strip().lower(): value for key, value in block.items()}
    picked: dict[str, Any] = {}
    for key, aliases in probes.items():
        for alias in aliases:
            name = alias.lower()
            if name in lowered:
                picked[key] = lowered[name]
                break
    return picked


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {}) and value is not False


def _block_fields_present(block: Any, probes: dict[str, tuple[str, ...]]) -> list[str]:
    """结构化块里**真的有值**的字段（None/空/False 视为缺失，不拿占位充数）。"""
    picked = _block_field_names(block, probes)
    return [key for key in probes if key in picked and _has_value(picked[key])]


def _block_declares(block: Any, probes: dict[str, tuple[str, ...]]) -> list[str]:
    """结构化块里**声明了但没有值**的字段（显式 null / 空串）——即「取不到」而非「没说」。"""
    picked = _block_field_names(block, probes)
    return [key for key in probes if key in picked and not _has_value(picked[key])]


def _covered_with_block_authority(
    *,
    block: Any,
    block_probes: dict[str, tuple[str, ...]],
    prose_probes: dict[str, tuple[str, ...]],
    labels: dict[str, str],
    prose: str,
) -> tuple[list[str], list[str]]:
    """完成度的「覆盖」口径：**结构化块有值 > 正文提及**（v39 真机缺陷）。

    上一版把正文关键词命中也算覆盖，于是出现「现金流与资本开支：已完成（已覆盖 折旧摊销、
    营运资本变动）」与同一份导出里「折旧摊销 未取到」并存——因为正文写的是
    「来源未提供折旧摊销与营运资本变动明细」，探针只看见词、看不见那句话是在说缺。
    现在：块里声明了该字段却没有值 → 记缺口（并把「正文提到」单独留痕）；
    只有整块缺失（旧报告）时才退回正文探针，避免把旧档案一律判成未完成。
    """
    hits = _block_fields_present(block, block_probes)
    declared_missing = _block_declares(block, block_probes)
    prose_hits = _prose_hits(prose, prose_probes)
    covered: list[str] = []
    mentioned_but_missing: list[str] = []
    for key in prose_probes:
        label = labels[key]
        if key in hits:
            covered.append(label)
        elif key in declared_missing:
            if key in prose_hits:
                mentioned_but_missing.append(label)
        elif key in prose_hits:
            covered.append(label)
    return covered, mentioned_but_missing


# ---------------------------------------------------------------------------
# Q16/Q17/Q18（2026-09-19 路线图 §5）：深研论证的三块结构化拆解
# ---------------------------------------------------------------------------
# 症状（申通样例）：盈利高增长只用「10.35 亿 vs 4.53 亿」一句带过；「经营现金流/净利 = 2.63」
# 被直接写成「盈利质量较好」；PE 1.7% 分位与 PB 66.9% 分位的分化没有解释，低 PE 还被当作
# 安全边际。三块归一器把「有没有拆、拆得出来吗、有没有来源」变成可复算字段——
# **算不出来的比例一律留空**，服务端不替模型编一个贡献百分比。

EARNINGS_FACTOR_KEYS = ("volume", "price", "cost_efficiency", "low_base", "non_recurring")
_CASH_FLOW_CORE_FIELDS = ("operating_cash_flow", "net_income")
_CASH_FLOW_EXTRA_FIELDS = ("depreciation_amortization", "working_capital", "capex", "free_cash_flow")
CASH_FLOW_VERDICT_INSUFFICIENT = "不足以判断盈利质量"
CASH_FLOW_VERDICT_STRONG = "盈利质量较好"
CASH_FLOW_VERDICT_WEAK = "盈利质量偏弱"


def normalize_earnings_growth(value: Any, *, allowed_sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """盈利增长拆解（Q16）：五类因素各自的效应、贡献与来源。

    - `contribution_pct` **只在原文同时给出数字与算法时**才保留（可复算）；否则置 None 并在
      `note` 说明缺什么——不给模型一个服务端替他算出来的百分比（验收：不伪造贡献比例）；
    - `source` 不在本次真实取到的来源集合内 → `source_note="无来源"`（复用 `filter_citations` 口径）；
    - 越界 factor 原样留痕在 `factor_raw`，不猜归属。
    """
    allowed = {str(item).strip() for item in (allowed_sources or set()) if str(item).strip()}
    rows = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for item in rows[:6]:
        if not isinstance(item, dict):
            continue
        factor_raw = str(item.get("factor", "")).strip().lower()
        contribution_raw = _as_float_or_none(item.get("contribution_pct"))
        basis = str(item.get("basis", "") or "").strip()
        formula = str(item.get("formula", "") or "").strip()
        quantified = contribution_raw is not None and bool(basis or formula)
        notes: list[str] = []
        if contribution_raw is None:
            notes.append("未给出可复算的贡献比例（服务端不代填数字）")
        elif not quantified:
            contribution_raw = None
            notes.append("贡献比例缺少计算依据（basis/formula），按未量化处理")
        sources = [str(sid).strip() for sid in (item.get("sources") or []) if str(sid).strip()]
        if allowed:
            kept, dropped = filter_citations(sources, allowed)
            if dropped:
                notes.append(f"引用的来源不属于本次真实取到的来源：{'/'.join(dropped)}")
        else:
            kept = sources
        out.append({
            "factor": factor_raw if factor_raw in EARNINGS_FACTOR_KEYS else "",
            "factor_label": EARNINGS_FACTOR_LABELS.get(factor_raw, factor_raw or "未指明因素"),
            "factor_raw": factor_raw,
            "effect": str(item.get("effect", "")).strip()[:160],
            "contribution_pct": round(contribution_raw, 2) if quantified and contribution_raw is not None else None,
            "basis": basis[:200],
            "formula": formula[:160],
            "formula_inputs": item.get("formula_inputs") if isinstance(item.get("formula_inputs"), list) else [],
            "sources": kept[:5],
            "source_note": "、".join(kept) if kept else "无来源",
            "note": "；".join(notes),
        })
    return out


def cash_flow_quality(
    *,
    operating_cash_flow: Any = None,
    net_income: Any = None,
    depreciation_amortization: Any = None,
    working_capital_change: Any = None,
    capex: Any = None,
    free_cash_flow: Any = None,
    claimed_quality: str = "",
    operating_cash_flow_period: str = "",
    capex_period: str = "",
) -> dict[str, Any]:
    """现金流质量（Q17）：覆盖倍数只是入口，结论强度由补到的口径数量决定。

    - 覆盖倍数 = 经营现金流 ÷ 归母净利（两者齐备才算得出）；
    - **只有覆盖倍数时一律 `verdict=不足以判断盈利质量`**：折旧摊销（非现金费用）、
      营运资本变动（应收/应付摆动）、资本开支都会把倍数顶高或压低；
    - 正向结论还须有**解释项**（折旧摊销或营运资本变动）——资本开支/自由现金流是派生项；
    - 自由现金流只有在两个输入的**统计期一致**时才由服务端相减得出（v39 真机：
      TTM 经营现金流 51.23 减 H1 资本开支 14.47 得 36.76 被当成事实报出——期不同根本不能相减）；
    - 正文若已写「盈利质量较好」而口径不足，`downgrade_note` 给出降档理由（不改写正文）。
    """
    ocf = _as_float_or_none(operating_cash_flow)
    profit = _as_float_or_none(net_income)
    d_a = _as_float_or_none(depreciation_amortization)
    wc = _as_float_or_none(working_capital_change)
    capex_value = _as_float_or_none(capex)
    fcf = _as_float_or_none(free_cash_flow)
    coverage = round(ocf / profit, 2) if ocf is not None and profit not in (None, 0) else None
    ocf_period = str(operating_cash_flow_period or "").strip()
    capex_period_value = str(capex_period or "").strip()
    periods_comparable = bool(ocf_period and capex_period_value and ocf_period == capex_period_value)
    # Q17 修订（v39 真机缺陷）：资本开支与自由现金流**不能**和折旧摊销、营运资本并列算「解释项」。
    # 自由现金流 = 经营现金流 − 资本开支，是从覆盖倍数那一侧算术派生出来的，它不解释
    # 「为什么经营现金流是净利的 2.63 倍」——能解释这件事的只有非现金费用（折旧摊销）
    # 与应收应付摆动（营运资本变动）。只有派生项时按证据不足对待。
    derived_fcf_note = ""
    fcf_period_note = ""
    if fcf is None and ocf is not None and capex_value is not None:
        if periods_comparable:
            fcf = round(ocf - capex_value, 2)
            derived_fcf_note = f"（{ocf_period}：经营现金流－资本开支，服务端复算）"
        else:
            fcf_period_note = (
                "自由现金流未计算：经营现金流（"
                + (ocf_period or "统计期未标注") + "）与资本开支（"
                + (capex_period_value or "统计期未标注") + "）统计期不一致或未声明，跨期相减会得到无意义的数。"
            )
    explanatory = [label for label, value in (("折旧摊销", d_a), ("营运资本变动", wc)) if value is not None]
    derived = [label for label, value in (("资本开支", capex_value), ("自由现金流", fcf)) if value is not None]
    derived = [f"{label}{derived_fcf_note}" if label == "自由现金流" and derived_fcf_note else label for label in derived]
    extras = explanatory + derived

    if ocf is None or coverage is None:
        verdict = "无现金流口径"
        note = "未取得经营现金流或归母净利润：本轮不对盈利质量作判断。"
    elif fcf is not None and fcf < 0:
        # 负向结论（钱被资本开支吞掉）不需要解释项也成立：它是算术事实。
        verdict = CASH_FLOW_VERDICT_WEAK
        note = f"自由现金流为 {fcf}（负值）：资本开支吞掉经营现金流，覆盖倍数高不等于赚到手。"
    elif explanatory and len(extras) >= 2:
        verdict = CASH_FLOW_VERDICT_STRONG
        note = f"覆盖倍数 {coverage}，并已交叉核对 {'、'.join(extras)}。"
    else:
        verdict = CASH_FLOW_VERDICT_INSUFFICIENT
        note = (
            f"覆盖倍数 {coverage} 只说明经营现金流与净利的比值"
            f"；当前只补到 {'、'.join(extras) if extras else '无其他口径'}——"
            + (
                "资本开支与自由现金流是从经营现金流算术派生的，不解释覆盖倍数为何是现在这个数；"
                "要谈盈利质量还差折旧摊销或营运资本变动（非现金费用与应收应付摆动）。"
                if derived and not explanatory else
                "折旧摊销、营运资本变动、资本开支至少再补两项才能谈盈利质量。"
            )
        )

    downgrade_note = ""
    if claimed_quality and (verdict == CASH_FLOW_VERDICT_INSUFFICIENT or verdict == "无现金流口径"):
        downgrade_note = (
            f"正文出现「{claimed_quality}」式结论，但现金流口径不足（当前判定：{verdict}）："
            "该结论按证据不足对待，只保留为待验证说法。"
        )
    if fcf_period_note:
        note = f"{note} {fcf_period_note}"
    return {
        "version": "v1",
        "operating_cash_flow": ocf,
        "net_income": profit,
        "coverage_ratio": coverage,
        "operating_cash_flow_period": ocf_period,
        "capex_period": capex_period_value,
        "free_cash_flow_derived": bool(derived_fcf_note),
        "depreciation_amortization": d_a,
        # 与 `_CASH_FLOW_BLOCK_KEYS` 的探针口径对齐：完成度读 `working_capital`，
        # 两个键都给出（同值），避免「补了营运资本却没被看见」这种自相矛盾。
        "working_capital": wc,
        "working_capital_change": wc,
        "capex": capex_value,
        "free_cash_flow": fcf,
        "extras_present": extras,
        "explanatory_present": explanatory,
        "derived_present": derived,
        "verdict": verdict,
        "note": note,
        "downgrade_note": downgrade_note,
        "strength": "insufficient" if verdict in (CASH_FLOW_VERDICT_INSUFFICIENT, "无现金流口径") else "usable",
    }


def _normalize_business_model(text: str) -> str:
    """商业模式口径的规范化：去空白与常见接尾词，便于「加盟制快递」与「快递服务」能对上。"""
    cleaned = "".join(str(text or "").split()).lower()
    for suffix in ("运营商", "服务商", "服务商之一", "企业", "公司", "商", "业"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix) + 1:
            cleaned = cleaned[: -len(suffix)]
    return cleaned.strip("的之")


def _business_model_matches(candidate: str, normalized_subject: str) -> bool:
    """同模式判定：规范化后互为包含即算同模式（精确相等太脆，模型每次用词都不同）。"""
    normalized = _normalize_business_model(candidate)
    if not normalized or not normalized_subject:
        return False
    return normalized_subject in normalized or normalized in normalized_subject


def comparable_review(
    comparables: Any,
    *,
    subject_model: str = "",
    min_sample: int = 3,
    server_peer_count: int | None = None,
    server_comparable_basis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """可比公司样本与剔除规则（Q18）：样本多少、剔了谁、商业模式近不近。

    样本数**以服务端取数为准**（v39 真机缺陷）：模型自述「5 只样本，剔除 1 只」，而同业快照
    实际是 20 只池、PE 口径 7 只可比——采信自述会让「同业中位数」的分母凭空变小。
    模型给的名单与剔除理由仍然保留（那是它实际比对过的对象），但计数冲突要写出来。
    """
    rows = [row for row in (comparables if isinstance(comparables, list) else []) if isinstance(row, dict)]
    kept = [row for row in rows if str(row.get("excluded", "")).strip().lower() not in ("true", "1", "yes")]
    excluded = [
        {"name": str(row.get("name") or row.get("symbol") or "—"), "reason": str(row.get("exclude_reason") or "未说明")}
        for row in rows if row not in kept
    ]
    basis = {
        str(key): int(value)
        for key, value in (server_comparable_basis or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    # 服务端可比口径取最保守（最小）的那一项作为分母：PE 可比 7 只时，中位数只有 7 只的支撑。
    server_min_basis = min(basis.values()) if basis else None
    authoritative = server_min_basis if server_min_basis is not None else server_peer_count
    model_claimed = len(kept)
    sample_count = authoritative if authoritative is not None and authoritative > 0 else model_claimed
    conflict = ""
    if authoritative is not None and authoritative > 0 and model_claimed and model_claimed != authoritative:
        conflict = (
            f"模型自述可比样本 {model_claimed} 只，服务端同业快照为 {authoritative} 只"
            + (f"（同业池共 {server_peer_count} 只，各口径可比数：{ '、'.join(f'{k}:{v}' for k, v in sorted(basis.items())) }）"
               if server_peer_count else "")
            + "——按服务端口径解释中位数，自述只作留痕。"
        )
    normalized_subject = _normalize_business_model(subject_model)
    subject_is_sentence = len(str(subject_model or "").strip()) > 16 or any(
        mark in str(subject_model or "") for mark in ("，", "。", "（", "；")
    )
    same_model = [
        row for row in kept
        if normalized_subject and _business_model_matches(str(row.get("business_model") or ""), normalized_subject)
    ]
    # 剔除数按服务端口径算（同业池 − 可比数）：模型名单里列了几只「已剔除」不等于同业只剔了那么几只
    # （真机：名单 1 只，快照实为 20 只池里 13 只 PE 为负被排除）。
    excluded_count = max(int(server_peer_count) - int(sample_count), 0) if (
        server_peer_count and sample_count and int(server_peer_count) >= int(sample_count)
    ) else len(excluded)
    if not sample_count:
        comparability = "unavailable"
        note = "无可比样本：本轮不给同业结论。"
    elif sample_count < min_sample:
        comparability = "thin"
        note = f"可比样本仅 {sample_count} 只（同业池 {excluded_count + sample_count} 只，剔除 {excluded_count} 只）：只作参照，不足以支撑「同业都这么贵/便宜」。"
    elif not subject_model or subject_is_sentence:
        # 没给标的商业模式就没比对象：跳过核对却写「已核对」是把没做的说成做了（v39 真机）。
        comparability = "unverified"
        reason = "模型未给出标的商业模式" if not subject_model else "标的商业模式写成了整句描述，无法与样本逐项对齐"
        note = (
            f"{sample_count} 只样本，剔除 {excluded_count} 只；"
            f"商业模式口径**未核对**（{reason}，无法判断样本是否同模式）。"
        )
    elif len(same_model) < max(2, min_sample):
        comparability = "mixed"
        # 只陈述「对不上」这个事实并把样本口径列出来，不替读者断言中位数一定被带偏。
        sample_labels = "、".join(sorted({
            str(row.get("business_model") or "").strip() for row in kept if str(row.get("business_model") or "").strip()
        })[:4]) or "样本未标注商业模式"
        note = (
            f"{sample_count} 只样本中仅 {len(same_model)} 只标注与标的同模式（标的：{subject_model}；样本口径：{sample_labels}）："
            "两者用词不能直接对齐，同业中位数按部分可比对待，是否等价请回查来源。"
        )
    else:
        comparability = "ok"
        note = f"{sample_count} 只样本，剔除 {excluded_count} 只，商业模式口径已核对。"
    if conflict:
        note = f"{note} {conflict}"
    return {
        "sample_count": sample_count,
        "model_claimed_sample_count": model_claimed,
        "excluded_count": excluded_count,
        "server_peer_count": server_peer_count,
        "comparable_basis": basis,
        "count_conflict": conflict,
        "excluded": excluded[:8],
        "exclusion_rule": "；".join(f"{item['name']}：{item['reason']}" for item in excluded[:4]) or "未声明剔除规则",
        "comparability": comparability,
        "comparability_label": {"ok": "可比", "thin": "样本偏少", "mixed": "模式混杂",
                                "unverified": "模式未核对", "unavailable": "无可比样本"}[comparability],
        "note": note,
        "sensitivity_allowed": False,  # 下面由调用方按正常化依据置真；默认不给敏感性结论
    }


def valuation_divergence(
    *,
    valuation: Any,
    financials: Any = None,
    comparables: Any = None,
    subject_model: str = "",
    server_metrics: Any = None,
) -> dict[str, Any]:
    """估值分化解释（Q18）：PE/PB 分化要联系 ROE、盈利周期与净资产口径。

    `low_pe_is_not_margin_of_safety` 恒为 True——这是本轮要根治的误读：**低 PE 分位不能单独
    构成安全边际**（利润处于周期高点时 PE 低分位反而危险）。`sensitivity_allowed` 只在
    正常化盈利依据齐备（`derive_valuation_evidence_state == fair_value_assessed`）时为真。

    `server_metrics` 是服务端估值快照（`valuation_chart`：PE/PB/PS 原值 + 历史分位 + 同业中位
    与可比口径）。v39 真机缺陷：不传它就会把模型转述的字符串「PE(TTM) 11.70」当数值存下来，
    而 PB 1.94 / 66.9% 分位明明取得到，块里却显示「未取到」——数值一律以快照为准，
    模型那份只作展示兜底与冲突留痕。
    """
    data = valuation if isinstance(valuation, dict) else {}
    fin = financials if isinstance(financials, dict) else {}
    snap = server_metrics if isinstance(server_metrics, dict) else {}
    percentiles = snap.get("percentiles") if isinstance(snap.get("percentiles"), dict) else {}
    evidence_state = derive_valuation_evidence_state(data)
    roe = _as_float_or_none(fin.get("roe") if fin.get("roe") is not None else data.get("roe"))
    pe_ttm = _as_float_or_none(snap.get("pe_ttm"))
    pb_mrq = _as_float_or_none(snap.get("pb_mrq"))
    pe_percentile = _as_float_or_none(percentiles.get("pe_ttm"))
    pb_percentile = _as_float_or_none(percentiles.get("pb_mrq"))
    axes: list[str] = []
    if roe is not None:
        axes.append("roe")
    if str(data.get("pb_basis") or fin.get("equity_basis") or "").strip() or fin.get("book_value") is not None:
        axes.append("equity_basis")
    cycle_note = str(fin.get("cycle_position") or data.get("cycle_note") or "").strip()
    if cycle_note:
        axes.append("cycle")
    review = comparable_review(
        comparables,
        subject_model=subject_model,
        server_peer_count=_as_int_or_none(snap.get("peer_count")),
        server_comparable_basis=snap.get("peer_median_basis"),
    )
    review["sensitivity_allowed"] = evidence_state["state"] == "fair_value_assessed"
    # 分位必须带着窗口一起出现：真机那份把「1.7% 分位」裸着写，反方检查点名的正是
    # 「分位基于近1250日样本起点、易被读成绝对低估」。窗口取自服务端快照，缺就不写。
    window_bars = _as_int_or_none(snap.get("percentile_window_bars"))
    window_from = str(snap.get("percentile_window_from") or "").strip()
    # 不再自带括号：展示层会把分位包进「PE 11.7（…）」那一层，套两圈括号读不动。
    window_note = ""
    if window_bars:
        window_note = f"，近{window_bars}个交易日" + (f"自{window_from}" if window_from else "")
    return {
        "version": "v1",
        "pe_ttm": pe_ttm if pe_ttm is not None else str(data.get("pe_ttm") or "").strip(),
        "pb_mrq": pb_mrq if pb_mrq is not None else str(data.get("pb_mrq") or fin.get("pb_mrq") or "").strip(),
        "pe_percentile": (
            f"{pe_percentile}%{window_note}" if pe_percentile is not None
            else str(data.get("pe_percentile") or "").strip()
        ),
        "pb_percentile": (
            f"{pb_percentile}%{window_note}" if pb_percentile is not None
            else str(data.get("pb_percentile") or "").strip()
        ),
        "percentile_window_bars": _as_int_or_none(snap.get("percentile_window_bars")),
        "percentile_window_from": str(snap.get("percentile_window_from") or "").strip(),
        "peer_median": {
            str(key): _as_float_or_none(value)
            for key, value in (snap.get("peer_median") if isinstance(snap.get("peer_median"), dict) else {}).items()
        },
        "metrics_source": "服务端估值快照" if pe_ttm is not None or pb_mrq is not None else "模型自述（未取得快照）",
        "roe": roe,
        "equity_basis": str(data.get("pb_basis") or fin.get("equity_basis") or "").strip(),
        "cycle_note": cycle_note,
        "explained_axes": axes,
        "explanation_state": "done" if len(axes) >= 2 and review["comparability"] in ("ok", "mixed") else (
            "partial" if axes or review["sample_count"] else "missing"
        ),
        "low_pe_is_not_margin_of_safety": True,
        "guardrail": (
            "低 PE 分位只说明「按当前利润算的倍数低」，利润处于周期高点或含一次性收益时"
            "它反而危险；安全边际必须建立在正常化盈利与可复算的公允价值之上。"
        ),
        "valuation_evidence_state": evidence_state["state"],
        "sensitivity_allowed": review["sensitivity_allowed"],
        "sensitivity_note": (
            "正常化盈利依据齐备，可给出可复算的情景/敏感性估值。"
            if review["sensitivity_allowed"]
            else "正常化盈利未验证：不给敏感性或目标价，只报指标与分位。"
        ),
        "comparable_review": review,
    }


def _earnings_completeness(payload: dict[str, Any], prose: str) -> dict[str, Any]:
    """盈利增长拆解：五维度里覆盖了几个（结构化块优先，正文探针兜底）。"""
    # Q16 的结构化拆解块：`earnings_growth` 是提示词契约里的键名，`earnings_decomposition`
    # 是路线图书面写法，两者都读——完成度只负责「看见有没有拆」，不绑定某一份命名。
    rows = _structured_rows(payload.get("earnings_growth") or payload.get("earnings_decomposition"))
    structured_covered: list[str] = []
    unquantified: list[str] = []
    unsourced: list[str] = []
    for row in rows:
        kind = str(row.get("factor") or "").strip().lower()
        label = EARNINGS_FACTOR_LABELS.get(kind)
        if label is None or not str(row.get("effect") or "").strip():
            continue
        if label not in structured_covered:
            structured_covered.append(label)
        if _as_float_or_none(row.get("contribution_pct")) is None:
            note = str(row.get("note") or "").strip()
            unquantified.append(f"{label}：{note or '贡献比例无法从文本复算（服务端不伪造比例）'}"[:80])
        if not str(row.get("source") or "").strip() or "无来源" in str(row.get("source_note") or ""):
            unsourced.append(label)
    prose_labels = [EARNINGS_FACTOR_LABELS[key] for key in _prose_hits(prose, _EARNINGS_FACTOR_PROBES)]
    # 有拆解块时只认块里的因素：正文提到「价格战」但没拆进块，属于没拆（同一份导出不能
    # 既写「缺：价格」又靠正文关键词把价格算成已覆盖）。整块缺失的旧报告仍按正文探针判。
    if rows:
        covered = list(structured_covered)
        mentioned = [label for label in prose_labels if label not in structured_covered]
    else:
        covered = list(dict.fromkeys(structured_covered + prose_labels))
        mentioned = []
    gaps = [label for label in EARNINGS_FACTOR_LABELS.values() if label not in covered]
    evidence: list[str] = []
    if rows:
        evidence.append(f"结构化拆解 {len(structured_covered)} 项带因素结论（{('、'.join(structured_covered)) or '无有效因素'}）")
    if prose_labels:
        evidence.append(f"正文提及维度：{'、'.join(prose_labels)}")
    if mentioned:
        evidence.append(f"正文提到但未拆进拆解块，按缺口计：{'、'.join(mentioned)}")
    if unquantified:
        evidence.append(f"未给出可复算贡献比例：{'；'.join(unquantified[:3])}")
    if unsourced:
        evidence.append(f"无来源支撑的因素：{'、'.join(unsourced)}")
    if len(covered) >= 3:
        state = COMPLETENESS_DONE
    elif covered:
        state = COMPLETENESS_PARTIAL
    else:
        state = COMPLETENESS_MISSING
    return _completeness_item(
        "earnings_decomposition",
        state,
        covered=covered,
        gaps=gaps,
        evidence=evidence,
        note="至少需拆到业务量/价格/成本效率/低基数/非经常性损益中的三项，才算把增长来源说清。",
    )


def _cash_flow_completeness(payload: dict[str, Any], prose: str) -> dict[str, Any]:
    """现金流与资本开支：覆盖倍数之外是否补到**解释项**（折旧摊销 / 营运资本变动）。"""
    block = payload.get("cash_flow_quality")
    covered, mentioned = _covered_with_block_authority(
        block=block,
        block_probes=_CASH_FLOW_BLOCK_KEYS,
        prose_probes=_CASH_FLOW_PROBES,
        labels=_CASH_FLOW_LABELS,
        prose=prose,
    )
    hits = _block_fields_present(block, _CASH_FLOW_BLOCK_KEYS)
    declared_missing = _block_declares(block, _CASH_FLOW_BLOCK_KEYS)
    prose_hits = _prose_hits(prose, _CASH_FLOW_PROBES)

    def seen(key: str) -> bool:
        # 块里有值 → 看见；块声明了但没值 → 没看见（正文再怎么说也不算）；整块没这个字段 → 退回正文探针。
        return key in hits or (key not in declared_missing and key in prose_hits)

    explanatory = [key for key in _CASH_FLOW_EXPLANATORY if seen(key)]
    derived = [key for key in _CASH_FLOW_DERIVED if seen(key)]
    extras = explanatory + derived
    ocf_seen = seen("operating_cash_flow")
    gaps = [_CASH_FLOW_LABELS[key] for key in _CASH_FLOW_PROBES if _CASH_FLOW_LABELS[key] not in covered]
    evidence: list[str] = []
    if isinstance(block, dict):
        verdict = str(block.get("verdict") or "").strip()
        if verdict:
            evidence.append(f"服务端现金流质量判定：{verdict}")
        downgrade = str(block.get("downgrade_note") or "").strip()
        if downgrade:
            evidence.append(f"结论强度已降低：{downgrade[:80]}")
    if covered:
        evidence.append("已覆盖：" + "、".join(covered))
    if mentioned:
        evidence.append(
            f"正文提到但结构化块为「未取到」，按缺口计：{'、'.join(mentioned)}"
            "（探针只认有值的口径，不把『说了缺什么』当成覆盖）。"
        )
    if ocf_seen and not explanatory:
        evidence.append(
            "只报了经营现金流对净利的覆盖倍数"
            + (f"（{'、'.join(_CASH_FLOW_LABELS[key] for key in derived)} 是算术派生项）" if derived else "")
            + "——覆盖倍数本身不等于盈利质量，折旧摊销与营运资本变动才解释它为何是这个数。"
        )
    if not ocf_seen:
        state = COMPLETENESS_MISSING
    elif explanatory and len(extras) >= 2:
        state = COMPLETENESS_DONE
    else:
        state = COMPLETENESS_PARTIAL
    return _completeness_item(
        "cash_flow_and_capex",
        state,
        covered=covered,
        gaps=gaps,
        evidence=evidence,
        note="现金流 ÷ 净利之外，还须补到折旧摊销或营运资本变动（解释项）之一，再加一项资本开支/自由现金流。",
    )


def _valuation_completeness(payload: dict[str, Any], prose: str) -> dict[str, Any]:
    """估值分化解释：PE/PB 分化是否用 ROE/盈利周期/净资产口径解释，可比样本与剔除规则是否交代。"""
    block = payload.get("valuation_divergence")
    review = payload.get("comparables") if isinstance(payload.get("comparables"), dict) else None
    prose_hits = _prose_hits(prose, _VALUATION_EXPLAIN_PROBES)
    axis_probes = {
        "roe": ("roe",),
        "equity_basis": ("equity_basis", "book_value", "pb_basis"),
        "cycle": ("cycle", "cycle_note"),
    }
    # 与现金流同一口径：块里 `roe: null` / `equity_basis: ""` 就是没解释到，正文出现「ROE」
    # 三个字不算覆盖（真机那份报告正文写了 ROE 17.95%（S4派生），块里却是 null——按缺口计并留痕）。
    axes_covered, axes_mentioned = _covered_with_block_authority(
        block=block,
        block_probes=axis_probes,
        prose_probes={key: _VALUATION_EXPLAIN_PROBES[key] for key in axis_probes},
        labels=_VALUATION_EXPLAIN_LABELS,
        prose=prose,
    )
    axes = [key for key in axis_probes if _VALUATION_EXPLAIN_LABELS[key] in axes_covered]
    comparables_ok = bool(
        "comparables" in prose_hits
        or isinstance(block, dict) and (block.get("comparable_review") or block.get("comparables"))
        or isinstance(review, dict)
    )
    stated = bool(
        {"percentile", "divergence"} & set(prose_hits)
        or (isinstance(block, dict) and block)
    )
    covered = [_VALUATION_EXPLAIN_LABELS[key] for key in axes]
    if comparables_ok:
        covered.append("可比样本与剔除规则")
    gaps = [_VALUATION_EXPLAIN_LABELS[key] for key in ("roe", "equity_basis", "cycle") if key not in axes]
    if not comparables_ok:
        gaps.append("可比公司样本与筛选规则")
    evidence: list[str] = []
    if stated:
        evidence.append("正文报了估值分位或指标分化")
    if axes:
        evidence.append("分化解释：" + "、".join(_VALUATION_EXPLAIN_LABELS[key] for key in axes))
    if axes_mentioned:
        evidence.append(
            f"正文提到但估值块里未取到，按缺口计：{'、'.join(axes_mentioned)}"
        )
    if comparables_ok:
        evidence.append("已交代可比样本与筛选规则")
    if "low_pe_caution" in prose_hits:
        evidence.append("已声明低 PE/PS 分位不等于安全边际")
    satisfied = sum([stated, len(axes) >= 2, comparables_ok])
    if stated and len(axes) >= 2 and comparables_ok:
        state = COMPLETENESS_DONE
    elif satisfied:
        state = COMPLETENESS_PARTIAL
    else:
        state = COMPLETENESS_MISSING
    return _completeness_item(
        "valuation_explanation",
        state,
        covered=covered,
        gaps=gaps,
        evidence=evidence,
        note="低分位与高分位并存时必须解释（ROE/盈利周期/净资产口径），并说明可比样本与筛选规则。",
    )


def _scenario_completeness(payload: dict[str, Any], prose: str) -> dict[str, Any]:
    """情景与反证：情景可证伪（有触发条件也有失效条件）+ 反证可见（反方检查或局限声明）。"""
    scenarios = normalize_scenarios(payload.get("scenarios"))
    with_trigger = [item for item in scenarios if item["trigger"]]
    falsiable = [item for item in with_trigger if item["invalidates"]]
    limitations = payload.get("limitations")
    limitations = [str(item).strip() for item in limitations if str(item).strip()] if isinstance(limitations, list) else []
    check = payload.get("counter_check")
    counter_seen = isinstance(check, dict)
    counter_ok = bool(counter_seen and check.get("ok") and str(check.get("strongest_counter") or "").strip())
    covered: list[str] = []
    evidence: list[str] = []
    if scenarios:
        covered.append("情景清单")
        evidence.append(f"{len(scenarios)} 条情景，其中 {len(with_trigger)} 条有触发条件、{len(falsiable)} 条有失效条件")
    if limitations:
        covered.append(f"局限声明 {len(limitations)} 条")
    if counter_ok:
        covered.append("反方检查最强反证")
        evidence.append(f"反方检查已执行并给出反证：{str(check.get('strongest_counter'))[:80]}")
    elif counter_seen:
        evidence.append("反方检查未成功执行（结果按未核验对待）")
    else:
        evidence.append("本次 payload 未带反方检查结果，反证以局限声明判定")
    if not counter_ok and any(word in prose for word in ("反证", "最大风险", "反方")):
        # 摘要/正文里写了反证句**不单独算完成**：它是提示词要求的第四句，几乎每篇都有；
        # 只作为已有反证信号的补充说明，判定仍以结构化情景 + 局限声明 + 反方检查为准。
        evidence.append("正文另有反证表述（未结构化，不单独计为完成）")
    scenario_part = bool(falsiable)
    counter_part = counter_ok or len(limitations) >= MIN_LIMITATIONS
    if scenario_part and counter_part:
        state = COMPLETENESS_DONE
    elif scenario_part or counter_part:
        state = COMPLETENESS_PARTIAL
    else:
        state = COMPLETENESS_MISSING
    gaps: list[str] = []
    if not scenarios:
        gaps.append("结构化情景清单")
    elif not falsiable:
        gaps.append("情景失效条件（不可证伪）")
    if not counter_ok and len(limitations) < MIN_LIMITATIONS:
        gaps.append("反证或局限声明")
    return _completeness_item(
        "scenarios_and_counter_evidence",
        state,
        covered=covered,
        gaps=gaps,
        evidence=evidence,
        note="情景须同时给触发条件与失效条件，并留下反证（反方检查最强反证或 ≥3 条局限声明）。",
    )


def _completeness_item(
    key: str,
    state: str,
    *,
    covered: list[str],
    gaps: list[str],
    evidence: list[str],
    note: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": COMPLETENESS_ITEM_LABELS[key],
        "state": state,
        "state_label": COMPLETENESS_STATE_LABELS[state],
        "covered": covered,
        "gaps": gaps,
        "evidence": evidence or ["payload 里没有可判定该项的字段或文字"],
        "note": note,
    }


def research_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    """深研完成度（§5 Q19）：四项关键研究项各 `done|partial|missing`，`score = done/4`。

    - `complete` 只在四项**全部 done** 时为 True：**长而缺关键论证的报告不算研究完整**；
    - 字数只进 `word_count_note`（`word_count_advisory=True`），**短而四项齐备的报告不因篇幅被判无效**；
    - payload 里没有的字段一律如实计入缺口，不猜模型「大概写了」。
    """
    data = payload if isinstance(payload, dict) else {}
    prose = _payload_prose(data)
    items = [
        _earnings_completeness(data, prose),
        _cash_flow_completeness(data, prose),
        _valuation_completeness(data, prose),
        _scenario_completeness(data, prose),
    ]
    done_count = sum(1 for item in items if item["state"] == COMPLETENESS_DONE)
    partial_count = sum(1 for item in items if item["state"] == COMPLETENESS_PARTIAL)
    missing_count = sum(1 for item in items if item["state"] == COMPLETENESS_MISSING)
    complete = done_count == len(items)

    chars = report_char_count(data.get("report") if isinstance(data.get("report"), str) else "")
    declared_mode = str(data.get("report_mode") or "").strip() or DEFAULT_REPORT_MODE
    mode_fallback_note = ""
    try:
        mode_name, mode_label, mode_min, mode_max = report_mode_budget(declared_mode)
    except ValueError:
        mode_name, mode_label, mode_min, mode_max = report_mode_budget(DEFAULT_REPORT_MODE)
        mode_fallback_note = f"（研报模式标注异常「{declared_mode}」，按{mode_label}口径给出字数辅助提示）"
    if chars < mode_min:
        word_count_note = (
            f"正文 {chars} 字，低于{mode_label}建议区间 {mode_min}-{mode_max} 字"
            f"{mode_fallback_note}；字数只作辅助提示，不参与完成度判定。"
        )
    elif chars > mode_max:
        word_count_note = (
            f"正文 {chars} 字，超过{mode_label}建议区间 {mode_min}-{mode_max} 字"
            f"{mode_fallback_note}；字数只作辅助提示，篇幅长不等于研究完整。"
        )
    else:
        word_count_note = (
            f"正文 {chars} 字落在{mode_label}建议区间 {mode_min}-{mode_max} 字内{mode_fallback_note}；"
            "字数只作辅助提示。"
        )

    if complete:
        headline = (
            f"四项关键研究项齐备（{done_count}/{len(items)}）；{word_count_note}"
        )
    else:
        lacking = "；".join(
            f"{item['label']}（{item['state_label']}）"
            for item in items if item["state"] != COMPLETENESS_DONE
        )
        headline = (
            f"关键研究项缺失：{lacking}——研究完成度 {done_count}/{len(items)}，"
            "字数只作辅助提示，不能替代论证。"
        )

    warnings: list[str] = []
    for item in items:
        if item["state"] == COMPLETENESS_DONE:
            continue
        gaps = "、".join(item["gaps"][:4]) if item["gaps"] else "可判定的论证内容"
        warnings.append(
            f"关键研究项{item['state_label']}（{item['label']}，完成度 {done_count}/{len(items)}）："
            f"{item['note']}当前缺 {gaps}；下一步：补上述项的来源与计算口径——"
            "无法拆解的因素如实留空，服务端不代填数字，本项按证据不足降级对待。"
        )
    if not complete and chars >= mode_min:
        warnings.append(
            f"正文 {chars} 字已达{mode_label}建议下限，但四项关键研究项只完成 {done_count}/{len(items)}"
            "——篇幅不等于研究完整（证据缺失属论证问题，不是排版问题），深研结论按证据不足对待。"
        )

    return {
        "version": RESEARCH_COMPLETENESS_VERSION,
        "items": items,
        "done_count": done_count,
        "partial_count": partial_count,
        "missing_count": missing_count,
        "score": round(done_count / len(items), 2),
        "complete": complete,
        "headline": headline,
        "word_count": chars,
        "word_count_mode": {"mode": mode_name, "label": mode_label, "min": mode_min, "max": mode_max},
        "word_count_note": word_count_note,
        "word_count_advisory": True,
        "warnings": warnings,
        "note": (
            "完成度由 payload 的结构化拆解块与正文探针**确定性推导**，不采信模型自评；"
            "探针命不中即按缺口如实标注，不补写模型没说过的内容。"
        ),
    }


# ---------------------------------------------------------------------------
# Q20（同路线图 §5 P2）：让反方检查影响最终摘要——末尾否认了，摘要就不能继续无条件宣称
# ---------------------------------------------------------------------------
# 症状（申通快递样报，2026-09-19）：反方检查末尾已写「低 PE/PS 分位不等于安全边际」，
# 执行摘要却仍以「PE 历史分位仅 1.7%」作支持性口径。原链路只在**正文**做 D01/D02 对齐，
# 摘要（首屏唯一必读的一段）无人管；本函数把同一套判据用到摘要上，并**就地标注**：
# - 复用 `undoubted_ban_phrases` / `is_valuation_denial` / `align_valuation_language`（不另起词表）；
# - 反方否认与摘要断言逐句配对，命中处追加「（反方检查：此说不成立，…）」；
# - 模型原话整份保留在 `summary_original`（留痕），服务端只加标注、不改写事实句、不删句子；
# - 未完成反方检查时**如实说明未运行**，不假装已修订。
# ---------------------------------------------------------------------------

SUMMARY_OVERREACH_POLICY_VERSION = "v1"
SUMMARY_OVERREACH_MARKER_PREFIX = "（反方检查：此说不成立，"
_SUMMARY_OVERREACH_MARKER_SUFFIX = "）"

# 反方检查里对三类结论的**否认**判据（同句还需出现否认词，见 `_sentence_denies_kind`）。
_OVERREACH_COUNTER_PROBES: dict[str, tuple[str, ...]] = {
    "margin_of_safety": ("安全边际", "安全垫", "低估", "偏便宜", "错杀", "估值保护", "分位", "低位"),
    "normalization": ("正常化", "可持续", "一次性", "非经常性", "低基数", "周期高点", "扣非", "盈利质量", "利润质量"),
    "confirmation": ("确认", "确立", "突破", "趋势成立"),
}
# 摘要侧的**宣称**判据（命中且句内无保留/否认时视为替该结论背书）。
_OVERREACH_SUMMARY_PROBES: dict[str, tuple[str, ...]] = {
    "margin_of_safety": ("安全边际", "安全垫", "低估", "便宜", "错杀", "分位", "百分位", "历史低"),
    "normalization": ("盈利质量", "利润质量", "含金量", "可持续", "业绩改善", "利润改善", "增长质量"),
    "confirmation": ("确认", "确立", "突破", "趋势成立", "信号成立"),
}
_OVERREACH_KIND_LABELS: dict[str, str] = {
    "margin_of_safety": "安全边际/低估",
    "normalization": "正常化盈利与可持续性",
    "confirmation": "趋势确认",
}
# 就地标注里给出的理由（确定性文案；反方原话进 `overreach[].counter_quote`，不塞进标注里）。
_OVERREACH_REASONS: dict[str, str] = {
    "margin_of_safety": "PE/PS 历史分位与同业位次不等于安全边际，现价相对内在价值的折价未经验证",
    "normalization": "正常化盈利与利润可持续性未经验证，盈利质量与可持续增长结论不成立",
    "confirmation": "技术信号尚未被后续 K 线确认，趋势确立/有效突破结论不成立",
}
# 句内否认词（「未/无法/不能/不等于…」）——同句出现主题词即构成反方否认。
_OVERREACH_DENIAL_RE = re.compile(
    r"尚未|并未|未经|未|无法|不能|不足以|难以|没有|不等于|不代表|不构成|并非|不是|存疑|待验证|尚待"
)
# 摘要句里的自我保留/转折（自我保留的句子不算过度断言）。
_SUMMARY_HEDGING_RE = re.compile(
    r"不等于|不代表|不构成|并非|不是|无法|不能|不足以|尚未|待|存疑|但|然而|不过|风险"
)
# 「PE 历史分位仅 1.7%」这类**没有结论词却当支撑用**的写法（Q20 的原始症状）。
_PERCENTILE_FRAMING_RE = re.compile(r"(仅|只有|不足|低至|低于|处于低位|历史低)")
_PEER_METRIC_IN_TEXT_RE = re.compile(r"P(?:E|B|S)", re.IGNORECASE)


def _skip_overreach_sentence(sentence: str) -> bool:
    """标点/单字片段与**已带同类标注**的句子都要跳过（幂等：重复执行不再叠加标注）。"""
    return len(sentence) <= 1 or SUMMARY_OVERREACH_MARKER_PREFIX in sentence


def _counter_check_texts(counter_check: Any) -> list[str]:
    """反方检查结果里可作「否认来源」的文本池（最强反证 / 最易误读句 / 成立必要条件）。"""
    if isinstance(counter_check, str):
        return [counter_check] if counter_check.strip() else []
    if not isinstance(counter_check, dict) or not counter_check.get("ok"):
        return []
    texts: list[str] = [str(counter_check.get("strongest_counter") or "")]
    misread = counter_check.get("most_misread_sentence")
    if isinstance(misread, dict):
        texts.append(str(misread.get("quote") or ""))
        texts.append(str(misread.get("why") or ""))
    conditions = counter_check.get("necessary_conditions")
    if isinstance(conditions, list):
        texts.extend(str(item) for item in conditions)
    return [text for text in texts if str(text).strip()]


def _sentence_denies_kind(text: str, kind: str) -> bool:
    """反方句是否**否认**了该主题的结论。

    「安全边际」先换成「安全垫」，让 D01 的否认词表与对照否定逻辑直接覆盖同义词
    （「低 PE/PS 不等于安全边际」→「不等于安全垫」→ 命中 `is_valuation_denial`）；
    另两类靠「主题词 + 句内否认词」判定。
    """
    probe = text.replace("安全边际", "安全垫")
    if not any(word in probe for word in _OVERREACH_COUNTER_PROBES[kind]):
        return False
    if kind == "margin_of_safety":
        return is_valuation_denial(probe) or bool(_OVERREACH_DENIAL_RE.search(probe))
    return bool(_OVERREACH_DENIAL_RE.search(probe))


def counter_check_denials(counter_check: Any) -> dict[str, str]:
    """反方检查里成立的主题否认：`{kind: 反方原句}`（取首个命中的句子，原话截断保留）。"""
    denials: dict[str, str] = {}
    for text in _counter_check_texts(counter_check):
        for fragment in _sentence_fragments(text):
            sentence = fragment.strip()
            if not sentence or _skip_overreach_sentence(sentence):
                continue
            for kind in _OVERREACH_COUNTER_PROBES:
                if kind in denials or not _sentence_denies_kind(sentence, kind):
                    continue
                denials[kind] = sentence[:120]
    return denials


def _assertive_ban_words(text: str) -> list[str]:
    """D01 禁词里**未被否定字紧邻修饰**的那些（摘要侧的加严判据）。

    `undoubted_ban_phrases` 是句级否认窗口判据（正文对齐用），摘要更短、更口语，
    出现过「不判低估」「难称安全垫」这类不在 D01 动词表里的写法；这类句子已经自我否认，
    不该再被追加标注。故在复用 D01 词表之后，本地再过一层「禁词前 4 字内有无否定字」。
    """
    out: list[str] = []
    for pos, word in _ban_matches(text):
        head = text[max(0, pos - 4) : pos]
        if any(mark in head for mark in ("不", "非", "未", "无", "难", "别")):
            continue
        out.append(word)
    return out


def _summary_overreach_claims(sentence: str, denials: dict[str, str]) -> list[dict[str, Any]]:
    """摘要句在**被反方否认的主题**上是否还在下结论（返回命中的 kind + 命中的词）。"""
    if not sentence or _skip_overreach_sentence(sentence):
        return []
    claims: list[dict[str, Any]] = []
    upper = sentence.upper()
    for kind in ("margin_of_safety", "normalization", "confirmation"):
        if kind not in denials:
            continue
        if not any(word in sentence or word.upper() in upper for word in _OVERREACH_SUMMARY_PROBES[kind]):
            continue
        if kind == "confirmation" and not any(
            word in sentence for word in ("确认", "确立", "突破", "趋势成立", "信号成立")
        ):
            continue
        normalized = sentence.replace("安全边际", "安全垫")
        hedged = bool(_SUMMARY_HEDGING_RE.search(sentence)) or is_valuation_denial(normalized)
        banned = [word for word in undoubted_ban_phrases(normalized) if word in _assertive_ban_words(normalized)]
        # 摘要只有四句话，且第 4 句按提示词要求就是「最大风险/反证」：
        # 句内自带保留或转折（「不等于安全边际」「虽低但赔率有限」）即算如实表述，不加注。
        if hedged:
            continue
        if kind == "margin_of_safety":
            percentile_framing = bool(
                _PEER_METRIC_IN_TEXT_RE.search(sentence)
                and ("分位" in sentence or "百分位" in sentence or "历史低" in sentence)
                and _PERCENTILE_FRAMING_RE.search(sentence)
            )
            margin_words = [word for word in ("安全垫", "安全边际", "低估", "偏便宜", "错杀") if word in sentence]
            hits = sorted(set(banned + margin_words + (["低估值分位当支撑"] if percentile_framing else [])))
            if not hits:
                continue
        else:
            hits = [word for word in _OVERREACH_SUMMARY_PROBES[kind] if word in sentence or word.upper() in upper][:3]
            if not hits:
                continue
        claims.append({"kind": kind, "phrases": hits})
    return claims


def summary_overreach_review(
    *,
    executive_summary: str,
    counter_check: Any,
    valuation: Any,
    claim_findings: Any,
) -> dict[str, Any]:
    """Q20：反方检查否认过的结论，执行摘要不得继续无条件宣称——**就地标注 + 原话留痕**。

    返回 `{overreach, revised_summary, revision_note, revision_trace, ...}`：
    - `revised_summary`：就地标注后的摘要（无改动时为空串，调用方据此决定是否替换）；
    - `summary_original`：模型原话（一字未改，随记录留痕）；
    - `revision_trace`：逐处 `{line, sentence, markers, kind, counter_quote}`；
    - 同时复用 `align_valuation_language` 把 D01/D02 口径打到摘要上（此前只打正文），
      以及 `needs_summary_rewrite` / `summary_downgrade_notes` 给出配套的预算与降级信息。
    """
    summary = str(executive_summary or "")
    block = valuation if isinstance(valuation, dict) else {}
    status = str(block.get("valuation_status") or "").strip()
    peer_position = str(block.get("peer_position") or "").strip()
    denials = counter_check_denials(counter_check)

    # —— 复用 D01/D02：先给摘要里与驾驶舱口径相反的句子加既有对齐标注（词表与正文同源）——
    aligned, aligned_changes = align_valuation_language(summary, status, peer_position)
    aligned_lines = {int(item["line"]) for item in aligned_changes}

    # —— 反方否认 → 摘要断言逐句就地标注 ——
    lines = aligned.splitlines()
    marked: list[dict[str, Any]] = []
    overreach: list[dict[str, Any]] = []
    changed = bool(aligned_changes)
    for index, line in enumerate(lines):
        parts = _SENTENCE_SPLIT_KEEP.split(line)
        touched = False
        for pos in range(0, len(parts), 2):
            fragment = parts[pos]
            probe = fragment.strip()
            claims = _summary_overreach_claims(probe, denials)
            if not claims:
                continue
            markers: list[str] = []
            for claim in claims:
                marker = f"{SUMMARY_OVERREACH_MARKER_PREFIX}{_OVERREACH_REASONS[claim['kind']]}{_SUMMARY_OVERREACH_MARKER_SUFFIX}"
                if marker in probe:
                    continue
                markers.append(marker)
                overreach.append({
                    "kind": claim["kind"],
                    "kind_label": _OVERREACH_KIND_LABELS[claim["kind"]],
                    "line": index + 1,
                    "sentence": probe[:120],
                    "phrases": claim["phrases"],
                    "counter_quote": denials.get(claim["kind"], ""),
                    "marker_added": True,
                    "also_aligned_by": "D01/D02 摘要对齐" if (index + 1) in aligned_lines else "",
                })
            if not markers:
                continue
            parts[pos] = f"{fragment}{''.join(markers)}"
            touched = True
            changed = True
            marked.append({
                "line": index + 1,
                "sentence": probe[:120],
                "markers": [marker[:120] for marker in markers],
                "kinds": [claim["kind"] for claim in claims],
                "counter_quotes": [denials.get(claim["kind"], "") for claim in claims],
            })
        if touched:
            lines[index] = "".join(parts)
    revised = "\n".join(lines) if changed else ""

    for item in aligned_changes:
        overreach.append({
            "kind": "valuation_language_conflict",
            "kind_label": "估值口径与驾驶舱相反（D01/D02）",
            "line": int(item["line"]),
            "sentence": str(item.get("sentence") or "")[:120],
            "phrases": list(item.get("notes") or []),
            "counter_quote": "",
            "marker_added": True,
            "also_aligned_by": "复用 align_valuation_language 的对齐标注",
        })

    warnings: list[str] = []
    if marked:
        warnings.append(
            f"执行摘要过度断言：{len(marked)} 处结论与反方检查冲突，已就地标注「{SUMMARY_OVERREACH_MARKER_PREFIX}…）」"
            "（证据告警：模型原话保留在 summary_original，服务端不改写事实句）。"
        )
    if aligned_changes:
        warnings.append(
            f"执行摘要估值表述与驾驶舱口径相反 {len(aligned_changes)} 处，已按 D01/D02 口径加注（证据告警）。"
        )

    if marked or aligned_changes:
        revision_note = (
            f"执行摘要 {len(marked) + len(aligned_changes)} 处断言已就地标注"
            f"（反方检查否认 {len(marked)} 处、估值口径对齐 {len(aligned_changes)} 处）；"
            "模型原话整份保留在 summary_original，服务端只加标注、不改写事实句；"
            "标注为服务端过程说明，不计入摘要字数预算（与方向研判 C01 同一口径）。"
        )
    elif denials:
        revision_note = "反方检查已否认相关结论，但执行摘要未出现对应断言，无需修订（原话照常展示）。"
    elif not _counter_check_texts(counter_check):
        revision_note = "本次未完成反方检查（未执行或解析失败），摘要过度断言检查未运行——不假装已修订。"
    else:
        revision_note = "反方检查未否认低估/安全边际、正常化或趋势确认类结论，摘要无需修订。"

    return {
        "version": SUMMARY_OVERREACH_POLICY_VERSION,
        "reviewed": bool(_counter_check_texts(counter_check)),
        "changed": changed,
        "overreach": overreach,
        "counter_denials": denials,
        "summary_original": summary,
        "revised_summary": revised,
        "revision_note": revision_note,
        "revision_trace": marked,
        "valuation_language_repairs": aligned_changes,
        "original_summary_chars": report_char_count(summary),
        "revised_summary_chars": report_char_count(revised) if revised else report_char_count(summary),
        "original_over_target": needs_summary_rewrite(summary),
        "related_downgrade_notes": summary_downgrade_notes(
            claim_findings if isinstance(claim_findings, dict) else {}
        ),
        "warnings": warnings,
        "note": (
            "判据与正文 D01/D02 同源（undoubted_ban_phrases / is_valuation_denial / "
            "align_valuation_language），只在被反方检查否认的主题上加注，不另立词表。"
        ),
    }


# ---------------------------------------------------------------------------
# JV04（Jev 决策模型接入路线图 2026-09-21）：逐 claim 引用支撑校验（语义层）。
#
# 补的是本模块开头第 7-8 行、以及 `claim_conflict_signals` 的 `conflict_note` 反复自认的缺口：
# v27/v28 的 `support` 由**确定性集合运算**得出（引用的来源是否真实存在、是否覆盖所需字段、
# 证据等级是否达标）——**但「引用的这段证据在内容上是否真的支撑这条判断」从未校验**。
# 拿一条新闻稿去支撑「主业减亏」，与拿财报支撑，在确定性口径下长得一模一样。
#
# 这一层把语义比对交给 Jev（它只回类型化答案、不生成文本，因此不存在 JSON 解析失败）。
#
# 硬边界（与全站口径一致，务必遵守）：
# - **软校验**：只标注、只降级，**不改写模型原话、不删除判断、不阻断交付**；
# - **可整层关闭**：调用方按 `jev_enabled` 决定要不要调；关闭时行为与接入前完全一致；
# - **本模块保持纯函数**：这里只做「构造 state / 构造题目 / 合并结果」，网络调用在调用方
#   （与 `tactics_ai.py` 同一分工）；
# - **无引用的判断不进该层**：没有来源就无从比对，硬塞进去只会白付输入 token 并产出噪声；
# - **R6 已标定**：弱信号（`partial`）现已参与降级，依据与回退方式见 `JEV_PARTIAL_DOWNGRADES`。
# ---------------------------------------------------------------------------

#: 调用方注入的出网回调（本模块保持纯函数的边界所在）：
#: `(state, questions) -> {"answers": {题目 id: JevAnswer}, "model": "<响应版本号>"}`；
#: 失败/未启用时由调用方返回 None 或抛异常，两种情形都只降级、不阻断。
JevJudge = Callable[[str, Mapping[str, Mapping[str, Any]]], Mapping[str, Any] | None]

JEV_CLAIM_SCHEMA_VERSION = "1.0"

JEV_CLAIM_SUPPORT_QUESTION = "supports"
JEV_CLAIM_INVENTED_QUESTION = "invented"

# 官方上限：state + 最长单题 ≤ 32k。留足余量，state 上限取 24k。
JEV_CLAIM_STATE_MAX_CHARS = 24_000
# 单条来源只送**证据片段**（《契约》§F 白名单：明确排除「未引用的取数原文」与整份研报正文）。
JEV_CLAIM_SOURCE_CHARS = 700
# 单条判断原句的截断（防止一条超长判断吃掉整个 state 预算）。
JEV_CLAIM_TEXT_CHARS = 400
# 单次请求最多评估多少条判断；超出部分**如实标注「未进该层」**，不静默丢弃。
JEV_CLAIM_MAX_CLAIMS = 10

# 语义支撑四选项（id → 描述）。用描述性选项而不是「是/否」，因为「不支撑」与「无关」
# 是两种不同的毛病：前者是证据相悖，后者是拿错证据——修法不同，不该混为一谈。
JEV_CLAIM_SUPPORT_OPTIONS: dict[str, str] = {
    "support": "支撑：所引证据直接证实该判断，不需要额外假设",
    "partial": "部分支撑：证据方向一致，但强度或范围不足以完全证实该判断",
    "unsupport": "不支撑：证据存在，但内容与该判断相悖，或推不出该判断",
    "irrelevant": "无关：所引证据与该判断不是同一件事（例如用新闻稿支撑财务结论）",
}

JEV_CLAIM_SUPPORT_LABELS: dict[str, str] = {
    "support": "语义支撑",
    "partial": "语义部分支撑",
    "unsupport": "语义不支撑",
    "irrelevant": "引用无关",
}

# 语义层判定 → 是否让该判断走降级通道。
# 「不支撑 / 无关」是强信号（证据与判断相悖，或根本不是一回事），**始终降级**。
# 「部分支撑」**已参与降级**（2026-09-21 R6 标定后由 False 改 True）。依据
# `docs/evidence/jev-cjk-calibration-2026-09-21.md`：30 条有标注中文样本上
# 误伤率（应「支撑」被判成「不支撑/无关」）**0.0**、漏放率（应「不支撑/无关」被判成
# 「支撑/部分支撑」）**0.1**，满足工具设定的门槛（样本 ≥20 且误伤 ≤5% 且漏放 ≤10%）。
# 原先 False 的理由是「确定性层已判定过覆盖与等级，再降一次是重复计数」——这条仍然成立，
# 但标定显示「部分支撑」在中文样本上确实指向证据强度不足，重复计数的代价小于漏放证据不足的判断。
# ⚠️ 单条开关：回退只需把此常量改回 False，不必改判定逻辑。
JEV_PARTIAL_DOWNGRADES = True

# noul 三路（《契约》C1：noul **无 confidence**，只能按值分档）→ 哪一路降级。
# 「存疑」（0.2–0.8 中间地带）**只标注不降级**：交给人工，不硬二值化。
JEV_INVENTED_DOWNGRADES: tuple[str, ...] = ("yes",)


def _jev_snippet(text: Any, limit: int) -> str:
    """压平空白 + 截断（截断必须显式留痕，否则读的人以为原文就这么短）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…（已截断）"


def _jev_source_line(source_id: str, text: Any, *, limit: int) -> str:
    """一条所引证据：来源 id + 类别标签 + 证据等级 + 内容片段。

    带上等级是有用的——Jev 知道「这是只有标题的公告（C 级）」时，对「支撑得住吗」的判断
    会比看到一段光秃秃的文本更准。
    """
    profile = SOURCE_PROFILES.get(source_id) or {}
    label = str(profile.get("label") or source_id)
    quality = str(profile.get("quality") or "?")
    return f"所引证据 [{source_id}]（{label}；证据等级 {quality}）：{_jev_snippet(text, limit)}"


def jev_claim_state(
    claims: Iterable[dict[str, Any]],
    source_texts: Mapping[str, str],
    *,
    max_claims: int = JEV_CLAIM_MAX_CLAIMS,
    source_chars: int = JEV_CLAIM_SOURCE_CHARS,
    text_chars: int = JEV_CLAIM_TEXT_CHARS,
    max_chars: int = JEV_CLAIM_STATE_MAX_CHARS,
) -> dict[str, Any]:
    """构造 Jev 逐 claim 校验的 state（**数据部分**；指令部分在 `jev_claim_questions` 里）。

    返回：
      state      —— 送 Jev 的 state 文本
      evaluated  —— **实际进了 state 的**判断（含 `index`，即它在题目 id 里的编号）
      skipped    —— 没进 state 的判断及原因（`no_cited_source` / `over_claim_limit` /
                    `over_state_budget`）

    ⚠️ `skipped` 必须由调用方**如实标注**给用户——「没校验」与「校验通过」是两件事，
    静默丢弃会让用户以为这一层已经跑过了（对齐 JV00 铁律 6 的降级义务）。

    ⚠️ state 会出网到 TypeSafe（美国托管、默认非零留存），所以这里只送**判断原句 +
    所引证据的片段**，绝不送整份研报正文，也不送未被引用的取数原文（《契约》§F 白名单）。
    """
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    blocks: list[str] = []
    used = 0

    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        # 只送**真实存在且有内容**的来源：引用了本次没取到的来源属另一条口径的问题
        # （`check_claims` 已判 `no_source`），拿它来问语义支撑没有意义。
        cited = [str(sid) for sid in (claim.get("sources") or []) if str(sid) in source_texts]
        if not cited:
            skipped.append({"claim_id": claim_id, "reason": "no_cited_source"})
            continue
        if len(selected) >= max_claims:
            skipped.append({"claim_id": claim_id, "reason": "over_claim_limit"})
            continue

        index = len(selected) + 1
        lines = [
            f"【判断 {index}】（claim_id={claim_id}）",
            f"判断原句：{_jev_snippet(claim.get('text'), text_chars)}",
        ]
        lines.extend(
            _jev_source_line(sid, source_texts.get(sid), limit=source_chars) for sid in cited
        )
        block = "\n".join(lines)
        if used + len(block) > max_chars:
            # 超预算就整条跳过，而不是截一半送出去——半截证据会得出错误的「不支撑」。
            skipped.append({"claim_id": claim_id, "reason": "over_state_budget"})
            continue
        used += len(block)
        blocks.append(block)
        selected.append({
            "index": index,
            "claim_id": claim_id,
            "text": str(claim.get("text") or ""),
            "sources": cited,
        })

    return {"state": "\n\n".join(blocks), "evaluated": selected, "skipped": skipped}


def jev_claim_questions(evaluated: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """为每条进 state 的判断生成两题（`supports` choice + `invented` noul）。

    题目 id 用 `c{index}_supports` / `c{index}_invented`，index 与 state 里的「判断 N」一致；
    合并时按同一编号回填，**不依赖模型自报的 claim_id**（模型不一定会照抄 id）。

    两题一次请求并行评估（官方 fan-out：加题几乎不加时延，但**加题会增加输入 token**，
    输入计费——所以只问这两件确定性口径查不到的事，不为凑数加题）。
    """
    questions: dict[str, dict[str, Any]] = {}
    for item in evaluated:
        index = int(item["index"])
        questions[f"c{index}_{JEV_CLAIM_SUPPORT_QUESTION}"] = jev_client.choice_question(
            f"请只比对【判断 {index}】的原句与它【所引证据】的实际内容：这些证据在内容上"
            "是否支撑该判断？注意分工——引用是否真实存在、是否覆盖所需字段已由系统单独校验，"
            "你只需要判断**语义上是否支撑**。",
            JEV_CLAIM_SUPPORT_OPTIONS,
        )
        questions[f"c{index}_{JEV_CLAIM_INVENTED_QUESTION}"] = jev_client.noul_question(
            f"【判断 {index}】里是否包含**所引证据中找不到**的具体事实（数字、事件、时间、口径）？"
            "只依据给定证据判断，不要引入外部知识；存在则判定值应接近 1。",
            true_means="判断里有在所引证据中找不到的具体事实（疑似编造）",
            false_means="判断里的具体事实都能在所引证据中找到",
        )
    return questions


def apply_jev_claim_findings(
    claim_findings: Mapping[str, Any],
    *,
    evaluated: Iterable[dict[str, Any]],
    answers: Mapping[str, Any] | None,
    skipped: Iterable[Mapping[str, str]] | None = None,
    model: str = "",
    schema_version: str = JEV_CLAIM_SCHEMA_VERSION,
    note: str = "",
) -> dict[str, Any]:
    """把 Jev 应答合并回 `check_claims` 的结果（**软校验：只标注、只降级**）。

    `answers` 是 `{题目 id: JevAnswer}`；传 None 或空表示该层没跑（未启用 / 不可用）——
    此时**判定原样不动**，只在 `jev.available = False` 里如实说明，行为与接入前完全一致。
    `skipped` 来自 `jev_claim_state` 的「没进 state 的判断」，两种情形都要如实透传
    （没跑 ≠ 通过）。

    合并规则：
    - 每条被评估的判断加一个 `jev` 子对象（语义支撑 + 编造风险 + confidence + 所查来源）；
    - `unsupport` / `irrelevant` → `support` 由 `full` 降为 `partial`（原为 `none` 则保持
      `none`，不升不降），并进 `jev_downgraded`；
    - `invented` 判为「是」（noul ≥ `jev_client.JEV_NOUL_YES`，**中文标定值 0.85**，
      见 R6 报告）→ `support` 降为 `none` 并进 `jev_downgraded`，同时给**强告警**
      （疑似编造具体事实）；
    - `partial` → 是否降级由 `JEV_PARTIAL_DOWNGRADES` 决定（R6 标定后为 True）；
      `invented=uncertain` → **只标注**；
    - 从不改写 `text`，从不删除判断，从不阻断交付。

    降级会连带重算 `support_counts` 与 `core_conclusion_state` / `core_support_counts`——
    否则「核心判断 N/M 支撑」会与逐条 `support` 对不上。
    """
    findings_in = claim_findings.get("claims")
    if not isinstance(findings_in, list):
        return dict(claim_findings)

    evaluated_list = list(evaluated)
    skipped = [dict(item) for item in (skipped or [])]
    if not answers:
        merged = dict(claim_findings)
        merged["jev"] = {
            "available": False,
            "evaluated": 0,
            "skipped": skipped,
            "downgraded": [],
            "model": model,
            "schema_version": schema_version,
            "note": note or "语义支撑层未运行（Jev 未启用或不可用）：本次只有确定性口径的判定。",
        }
        merged["jev_warnings"] = []
        return merged

    # 拷贝后再改：调用方可能还要用原始 findings 做对比（不就地污染入参）。
    findings = [dict(item) for item in findings_in]
    by_id = {str(item.get("claim_id") or ""): item for item in findings}

    jev_by_claim: dict[str, dict[str, Any]] = {}
    for item in evaluated_list:
        index = int(item["index"])
        claim_id = str(item.get("claim_id") or "")
        support_answer = answers.get(f"c{index}_{JEV_CLAIM_SUPPORT_QUESTION}")
        invented_answer = answers.get(f"c{index}_{JEV_CLAIM_INVENTED_QUESTION}")
        picked = getattr(support_answer, "choice", None)
        support = str(picked) if picked in JEV_CLAIM_SUPPORT_OPTIONS else ""
        confidence = getattr(support_answer, "confidence", None)
        invented_value = getattr(invented_answer, "noul", None)
        invented = invented_answer.noul_verdict() if invented_answer is not None else None
        jev_by_claim[claim_id] = {
            "support": support,
            "support_label": JEV_CLAIM_SUPPORT_LABELS.get(support, "未取得语义判定"),
            "confidence": float(confidence) if confidence is not None else None,
            "invented": invented,
            "invented_value": float(invented_value) if invented_value is not None else None,
            "sources_checked": list(item.get("sources") or []),
        }

    downgraded: list[str] = []
    weakened: list[str] = []
    invented_claims: list[str] = []

    for claim_id, jev in jev_by_claim.items():
        finding = by_id.get(claim_id)
        if finding is None:
            continue
        finding["jev"] = jev
        is_weak = jev["support"] in ("unsupport", "irrelevant")
        if is_weak or jev["support"] == "partial":
            weakened.append(claim_id)
        if is_weak or (JEV_PARTIAL_DOWNGRADES and jev["support"] == "partial"):
            if finding.get("support") == SUPPORT_FULL:
                finding["support"] = SUPPORT_PARTIAL
                finding["support_label"] = _SUPPORT_LABELS[SUPPORT_PARTIAL]
            finding["support_source"] = "jev_semantic"
            downgraded.append(claim_id)
        if jev["invented"] in JEV_INVENTED_DOWNGRADES:
            invented_claims.append(claim_id)
            if finding.get("support") != SUPPORT_NONE:
                finding["support"] = SUPPORT_NONE
                finding["support_label"] = _SUPPORT_LABELS[SUPPORT_NONE]
            finding["support_source"] = "jev_semantic"
            if claim_id not in downgraded:
                downgraded.append(claim_id)

    # —— 重算汇总口径（降级后必须同步，否则「N/M 支撑」与逐条 support 对不上）——
    # 注意口径：`check_claims` 里 `supported` 数的是 `status == "supported"`，而 `status` 是
    # **确定性**三态（引用真实性/字段覆盖/证据等级）。语义层不改 `status`（那会让前端看到
    # 「被降级但缺口列表为空」的自相矛盾），只改 `support`——所以这里必须按 `support` 计数。
    # `claim_support_from_status` 是三态到支撑强度的双射，Jev 不跑时两种数法结果完全相同。
    supported = sum(1 for item in findings if item.get("support") == SUPPORT_FULL)
    partial = sum(1 for item in findings if item.get("support") == SUPPORT_PARTIAL)
    none_count = sum(1 for item in findings if item.get("support") == SUPPORT_NONE)
    core_items = [item for item in findings if str(item.get("importance") or "") == "high"] or findings
    core_total = len(core_items)
    core_supported = sum(1 for item in core_items if item.get("support") == SUPPORT_FULL)
    if not findings:
        core_state: str | None = None
    elif core_supported == core_total:
        core_state = "supported"
    elif core_supported > 0:
        core_state = "partial"
    else:
        core_state = "unsupported"

    jev_warnings: list[str] = []
    if invented_claims:
        jev_warnings.append(
            f"{len(invented_claims)} 条判断包含**所引证据中找不到**的具体事实"
            f"（{'、'.join(invented_claims)}）——疑似编造，相关表述按未证实对待，须人工复核。"
        )
    if downgraded:
        jev_warnings.append(
            f"{len(downgraded)} 条判断的引用经语义比对后**不支撑**该判断，已降级为「未独立验证」"
            f"（{'、'.join(downgraded)}）——确定性口径只校验了引用真实性与字段覆盖，"
            "这一层是内容比对（Jev 判定，未经中文样本标定，供参考）。"
        )
    if weakened:
        jev_warnings.append(
            f"{len(weakened)} 条判断的引用只是「部分支撑/不支撑」"
            f"（{'、'.join(weakened)}），结论强度应低于确定性口径的呈现。"
        )
    if skipped:
        jev_warnings.append(
            f"{len(skipped)} 条判断未进语义支撑层（原因见逐条记录）——"
            "「未校验」不等于「通过」，不要按已核验对待。"
        )

    merged = dict(claim_findings)
    merged.update({
        "claims": findings,
        "supported": supported,
        "support_counts": {SUPPORT_FULL: supported, SUPPORT_PARTIAL: partial, SUPPORT_NONE: none_count},
        "core_conclusion_state": core_state,
        "core_support_counts": {"supported": core_supported, "total": core_total},
        "core_conclusion_supported": (None if core_state is None else core_state == "supported"),
        "jev": {
            "available": True,
            "evaluated": len(evaluated_list),
            "skipped": skipped,
            "downgraded": downgraded,
            "weakened": weakened,
            "invented": invented_claims,
            "model": model,
            "schema_version": schema_version,
            "partial_downgrades": JEV_PARTIAL_DOWNGRADES,
            "note": note or (
                "语义支撑由 Jev 判定（内容比对），与确定性口径（引用真实性/字段覆盖/证据等级）并列，"
                "互不覆盖；只标注与降级，不改写原话、不阻断交付。"
            ),
        },
        "jev_warnings": jev_warnings,
    })
    return merged


# ---------------------------------------------------------------------------
# JV06：验证点优先级分诊（纯函数层，出网由调用方注入）
#
# **要解决的问题**：报告生成时模型会给出若干**验证点**（`watchpoints[]`：什么信号、什么时点验、
# 若为真则说明什么）。它们一律等权重地堆在「情景与验证」里，用户看不出哪一条值得现在就去查、
# 哪一条其实可以放着——而「查一次」的代价是一次取数 + 一次研报额度（真金白银）。
#
# **本层的分工**：给每条验证点打一道 `score`——值不值得现在花额度去查。评分不是凭手感：
# 题目里把三个判据写死（**是否可证伪 / 是否已有证据覆盖 / 是否影响持仓决策**），
# 由 3 级**描述性** rubric 承载（官方：只给数字等级会让概率摊平、confidence 崩到 0.33）。
#
# 为什么是**一道综合 rubric**而不是三道独立 score 加权合成（路线图允许两者）：
# 三道 score 需要三个权重 + 一个阈值 = **四个未标定的旋钮**，而 R6 的中文标定只覆盖了
# `noul` 与 `partial` 开关。旋钮越多，标定缺失的代价越大——所以收敛成一条综合 rubric，
# 把三个判据写进等级描述里（既守住官方「一题一维度」，又不引入未标定的权重）。
#
# **软校验铁律**（与 `report_quality` 其余各层同源）：
#   - 只**加标注 / 给排序 / 做折叠**，绝不改写验证点原文、绝不删除条目；
#   - 只在「判为最低档」且「置信度达标」时才折叠（见 `_followup_decision`）；
#   - 任何失败 / 缺答 / 低置信一律**不折叠**（宁可多留一条，也不把该查的藏起来）；
#   - 可整层关闭：Jev 未启用或总闸关闭时，本层一个字段都不产生。
# ---------------------------------------------------------------------------

#: 分片复核回调（与 JV05/JV08 共用 `jev_client.build_shard_reviewer` 的返回形态）。
JevShardReviewer = Callable[[Mapping[str, Any]], list[dict[str, Any]]]

JEV_FOLLOWUP_SCHEMA_VERSION = "1.0"

#: 逐条题 id：`w{index}_priority`。`index` 是**跨分片的全局编号**（与 state 里的「验证点 N」一致）。
JEV_FOLLOWUP_QUESTION = "priority"

#: 3 级**描述性** rubric。三个判据（可证伪 / 是否已有证据 / 是否影响决策）写进等级描述，
#: 而不是拆成三道题——理由见本节抬头（未标定的权重旋钮越多越糟）。
JEV_FOLLOWUP_LEVELS: list[str] = [
    "暂缓：即使它被证实也不会改变当前结论，或现有证据账本里已经答过这一问",
    "可稍后：值得记下来，等下一次常规更新（财报/月度数据）时再看就够",
    "优先：它最可能证伪或强化当前结论，且现在就有可查的数据来源",
]
#: 等级短标签（界面用）。
JEV_FOLLOWUP_LEVEL_LABELS: list[str] = ["暂缓", "可稍后", "优先"]

#: 折叠门槛：归一化展示分 **低于** 此值才进「暂缓」。3 级量表下即 `score == 0`。
#:
#: 与 JV08 同一条纪律——折叠 = 把东西藏起来，所以门槛往**保守**方向设：只藏明确判为最低档的。
JEV_FOLLOWUP_DEFER_PERCENT = 50

#: 「低置信 → 不折叠」的地板线。
#:
#: ⚠️ **未标定**：R6 只标定了 `noul` 三路阈值与 `partial` 降级开关，本场景（`score` 的
#: confidence 分档）**没有中文样本**，故沿用官方英文范例的 0.5 地板线。用法是保守方向
#: （置信度不达标就不折叠），所以标定缺失的代价是「折叠得偏少」而不是「把该查的藏起来」。
JEV_FOLLOWUP_CONFIDENCE_FLOOR = 0.5

#: 逐条分诊态：判为优先/可稍后（`priority`）/ 折叠暂缓（`deferred`）/ 没送出去评（`unannotated`）。
#: 与 JV05/JV08 一样，**「没评」不等于「不值得查」**。
JEV_FOLLOWUP_STATE_PRIORITY = "priority"
JEV_FOLLOWUP_STATE_DEFERRED = "deferred"
JEV_FOLLOWUP_STATE_UNANNOTATED = "unannotated"

#: 单轮上限。与 `normalize_watchpoints` 的 `[:5]` 同口径——它已经截过，这里只是**独立**再兜一次
#: （不依赖上游实现细节，换掉上游也不会突然放出几十条）。
JEV_FOLLOWUP_MAX_ITEMS = 5
#: state 预算：官方「state + 最长单题 ≤ 32k」，留足余量取 20k（与 JV04/JV05/JV08 同口径）。
JEV_FOLLOWUP_STATE_MAX_CHARS = 20_000
#: 单条验证点块上限与各字段截断长度。
JEV_FOLLOWUP_ITEM_MAX_CHARS = 1_200
JEV_FOLLOWUP_TEXT_CHARS = 300
JEV_FOLLOWUP_DUE_CHARS = 40


def _followup_clip(value: Any, limit: int) -> str:
    """按字符数裁剪并加省略号（只用于 state 内部片段，不用于判据块本身）。"""
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}…"


def _followup_block(index: int, watchpoint: Mapping[str, Any]) -> str:
    """单条验证点进 state 的文本块（《契约》§F 白名单：只有验证点自身的三个字段 + 到期日）。

    **明确不带**：研报正文、其它验证点、账户与持仓明细、密钥。验证点已经是模型对「接下来
    该验什么」的浓缩表述，再带上正文只是扩大出网面而不增加判别力。
    """
    signal = _followup_clip(watchpoint.get("signal"), JEV_FOLLOWUP_TEXT_CHARS) or "（未给出信号）"
    verify_by = _followup_clip(watchpoint.get("verify_by"), JEV_FOLLOWUP_TEXT_CHARS) or "（未给出时点）"
    expected = _followup_clip(watchpoint.get("expected_if_true"), JEV_FOLLOWUP_TEXT_CHARS) or "（未给出预期）"
    due_on = _followup_clip(watchpoint.get("due_on"), JEV_FOLLOWUP_DUE_CHARS)
    due_line = f"\n到期日：{due_on}" if due_on else "\n到期日：（未给出确切日期，按事件锚定）"
    return (
        f"【验证点 {index}】\n"
        f"信号：{signal}\n"
        f"验证时点：{verify_by}{due_line}\n"
        f"若为真则：{expected}"
    )


def jev_followup_state(
    watchpoints: Sequence[Mapping[str, Any]],
    *,
    max_items: int = JEV_FOLLOWUP_MAX_ITEMS,
    max_chars: int = JEV_FOLLOWUP_STATE_MAX_CHARS,
    item_max_chars: int = JEV_FOLLOWUP_ITEM_MAX_CHARS,
) -> dict[str, Any]:
    """构造 JV06 的 state（**数据部分**；指令部分在 `jev_followup_questions` 里）。

    返回 `{"state", "evaluated", "skipped"}`，其中

      evaluated = `[{"index", "due_on"}]`（index 从 1 起、跨分片全局编号）
      skipped   = `[{"index", "reason", "note"}]`，reason ∈ `over_item_budget` / `over_state_budget`
                  / `over_item_limit`

    超出上限的条目**如实进 `skipped`**，不静默丢弃——「没送评」与「评过了」是两件事。
    """
    evaluated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocks: list[str] = []
    used = 0

    for position, watchpoint in enumerate(watchpoints):
        if position >= max(0, int(max_items)):
            skipped.append({
                "index": position + 1,
                "reason": "over_item_limit",
                "note": f"超出单轮分诊上限 {max_items} 条，本条未送评",
            })
            continue
        block = _followup_block(len(blocks) + 1, watchpoint)
        if len(block) > item_max_chars:
            skipped.append({
                "index": position + 1,
                "reason": "over_item_budget",
                "note": "单条验证点超预算，本条未送评",
            })
            continue
        if blocks and used + len(block) + 2 > max_chars:
            skipped.append({
                "index": position + 1,
                "reason": "over_state_budget",
                "note": "本轮 state 预算已满，本条未送评（不硬塞半截，否则会得出错误结论）",
            })
            continue
        blocks.append(block)
        used += len(block) + 2
        evaluated.append({"index": len(blocks), "due_on": str(watchpoint.get("due_on") or "")})

    return {"state": "\n\n".join(blocks), "evaluated": evaluated, "skipped": skipped}


def jev_followup_questions(evaluated: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """为每条验证点生成一道 `score` 题（3 级描述性 rubric）。

    三个判据（是否可证伪 / 是否已有证据覆盖 / 是否影响持仓决策）写进题干与等级描述里，
    而不是拆成三道独立题——理由见本节抬头（未标定的权重旋钮越多越糟）。
    """
    questions: dict[str, dict[str, Any]] = {}
    for item in evaluated:
        index = int(item["index"])
        questions[f"w{index}_{JEV_FOLLOWUP_QUESTION}"] = jev_client.score_question(
            f"【验证点 {index}】值不值得**现在**花一次取数 + 一次研报额度去查？"
            "按三件事判断：① 它是否**可证伪**（能明确判出真/假，而不是永远说不清）；"
            "② **是否已有证据覆盖**（现有证据账本里已经答过这一问，就不必再花额度）；"
            "③ 它若被证实/证伪，**是否会改变当前的持仓或观察决策**。"
            "三条都弱才选最低档；只依据这条验证点本身判断，不要引入外部行情或你自己的预测。",
            JEV_FOLLOWUP_LEVELS,
        )
    return questions


def jev_followup_shards(
    watchpoints: Sequence[Mapping[str, Any]],
    *,
    max_items: int = JEV_FOLLOWUP_MAX_ITEMS,
    max_chars: int = JEV_FOLLOWUP_STATE_MAX_CHARS,
    item_max_chars: int = JEV_FOLLOWUP_ITEM_MAX_CHARS,
) -> dict[str, Any]:
    """把验证点包成 `build_shard_reviewer` 认的分片包（**恒为单片**，与 JV05/JV08 同形）。

    为什么仍走分片形态而不是直接 `(state, questions)`：退避序列、可重试状态码判定、
    失败隔离这三件事与「评的是什么」无关，各写一遍迟早漂成两套口径。单片包让 JV06 直接复用
    `jev_client.build_shard_reviewer`（`purpose="jev:followup-triage"`）。
    """
    plan = jev_followup_state(
        watchpoints, max_items=max_items, max_chars=max_chars, item_max_chars=item_max_chars
    )
    if not plan["evaluated"]:
        return {"shards": [], "skipped": plan["skipped"]}
    return {
        "shards": [{
            "state": plan["state"],
            "questions": jev_followup_questions(plan["evaluated"]),
            "evaluated": plan["evaluated"],
        }],
        "skipped": plan["skipped"],
    }


def _followup_decision(score_percent: int | None, confidence: float | None) -> tuple[bool, str]:
    """折叠规则（**硬编码且有据可查**，不是手感阈值）。

    折叠需要**两个条件同时成立**：

      1. `score_percent < JEV_FOLLOWUP_DEFER_PERCENT`（模型明确判「暂缓」档）；
      2. `confidence` 有值且 ≥ `JEV_FOLLOWUP_CONFIDENCE_FLOOR`。

    为什么是「与」而不是「或」：折叠 = 把这条藏进「暂缓」，**缺 confidence 一样不折叠**
    （模型自己没把握时我们不做减法）。把该查的验证点藏起来，代价远大于多留一条不太急的。
    """
    if score_percent is None:
        return False, "未取得评分，无法判断，不折叠（宁可多留一条）"
    if score_percent >= JEV_FOLLOWUP_DEFER_PERCENT:
        return False, f"展示分 {score_percent} 未低于折叠门槛 {JEV_FOLLOWUP_DEFER_PERCENT}，留在主线"
    if confidence is None:
        return False, "未返回置信度，无法确认，不折叠（宁可多留一条）"
    if confidence < JEV_FOLLOWUP_CONFIDENCE_FLOOR:
        return False, (
            f"置信度 {confidence:.2f} 低于地板 {JEV_FOLLOWUP_CONFIDENCE_FLOOR:.2f}，"
            "不折叠（宁可多留一条）"
        )
    return True, (
        f"判为「暂缓」档且置信度 {confidence:.2f}，折叠进「暂缓」（仍可展开，不删除）"
    )


def jev_followup_findings(
    shard_results: Sequence[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    """把分片应答合并成逐条分诊结论：`{index: {...}}`（index 与 state 里的「验证点 N」一致）。

    `shard_results` 每项：`{"evaluated", "answers", "error"}`（`build_shard_reviewer` 的返回形态）。

    合并规则（**宁可少折叠，也不藏起该查的**）：
      - 分片带 `error` → 全部标 `unannotated` + 失败原因，**不折叠**；
      - 题号缺答 / score 越界 → 同上；
      - 有有效答案 → 走 `_followup_decision`。

    **三者留痕**（验收明写）：`score`（原始等级轴取值）、`score_percent`（归一化展示分）、
    `confidence` 三个都存，缺一不可——否则事后无法复算「当时为什么把它折叠了」。
    """
    findings: dict[int, dict[str, Any]] = {}
    for result in shard_results:
        error = result.get("error")
        answers = result.get("answers") or {}
        for item in result.get("evaluated") or []:
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if error:
                findings[index] = {
                    "score": None, "score_percent": None, "confidence": None,
                    "level_label": "未取得判定", "deferred": False,
                    "triage_state": JEV_FOLLOWUP_STATE_UNANNOTATED,
                    "note": f"本条所在分片未取得应答（{error}），未分诊——按不折叠处理",
                }
                continue
            answer = answers.get(f"w{index}_{JEV_FOLLOWUP_QUESTION}")
            raw_score = getattr(answer, "score", None)
            levels = getattr(answer, "levels", None)
            level_count = levels if isinstance(levels, int) and levels >= 2 else len(JEV_FOLLOWUP_LEVELS)
            if not isinstance(raw_score, (int, float)) or raw_score < 0 or raw_score > level_count - 1:
                findings[index] = {
                    "score": None, "score_percent": None, "confidence": None,
                    "level_label": "未取得判定", "deferred": False,
                    "triage_state": JEV_FOLLOWUP_STATE_UNANNOTATED,
                    "note": "本条未取得有效评分（题号缺答或取值越界），未分诊——按不折叠处理",
                }
                continue
            score = float(raw_score)
            # 归一化在代码里做（官方 composite-scoring 口径）：不同长度量表必须归一化后才能比较。
            score_percent = round(score / (level_count - 1) * 100)
            confidence_raw = getattr(answer, "confidence", None)
            confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None
            deferred, note = _followup_decision(score_percent, confidence)
            level = max(0, min(round(score), len(JEV_FOLLOWUP_LEVEL_LABELS) - 1))
            findings[index] = {
                "score": round(score, 4),
                "score_percent": score_percent,
                "confidence": confidence,
                "level_label": JEV_FOLLOWUP_LEVEL_LABELS[level],
                "deferred": deferred,
                "triage_state": (
                    JEV_FOLLOWUP_STATE_DEFERRED if deferred else JEV_FOLLOWUP_STATE_PRIORITY
                ),
                "note": note,
            }
    return findings


def _followup_display_order(rows: Sequence[tuple[int, Mapping[str, Any]]]) -> list[int]:
    """展示顺序：展示分降序，**同分保持报告原有次序**。

    同分不重排是刻意的：模型只在「档位」上做区分，不该把同一档内的条目按自己的口味洗一遍牌。
    没有评分的条目（`unannotated`）**不参与重排**，按原序跟在最后——没评的东西没有理由被挪动。
    """
    def _key(pair: tuple[int, Mapping[str, Any]]) -> tuple[int, int, int]:
        position, triage = pair
        percent = triage.get("score_percent")
        ranked = isinstance(percent, (int, float))
        return (0 if ranked else 1, -int(percent) if ranked else 0, position)

    return [position for position, _triage in sorted(rows, key=_key)]


def apply_jev_followup_findings(
    watchpoints: Sequence[Mapping[str, Any]],
    *,
    reviewer: JevShardReviewer | None = None,
    note: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """给每条验证点打上分诊结论，并返回 `(标注后的验证点, 分诊回执块)`。

    **不就地修改入参**（拷贝后再改），因为调用方可能还要用原始列表做对比。

    `reviewer` 为 None（总闸关闭 / Jev 未启用）时**原样返回入参拷贝 + `None` 回执**——
    一个字段都不加，与接入前逐字节一致（JV00 铁律 6）。调用方把 `None` 写进 payload 的
    `jev_followup` 键，与相邻的 JV04 `jev` 同一条「没跑就明说没跑」的惯例。

    **分诊失败不影响验证点本身交付**：本函数**从不抛异常**——复核回调抛错时按
    `unannotated` 如实标注并**保持原序、不折叠**。验证点是报告的既有产出，
    不能因为一个附加的排序层挂掉而少一条。
    """
    items = [dict(item) for item in watchpoints]
    if reviewer is None:
        return items, None
    if not items:
        return items, {
            "available": True, "total": 0, "deferred": 0, "unannotated": 0,
            "order": [], "skipped": [], "model": "", "levels": list(JEV_FOLLOWUP_LEVEL_LABELS),
            "confidence_floor": JEV_FOLLOWUP_CONFIDENCE_FLOOR,
            "note": note or "本轮报告没有验证点，无需分诊。",
            "schema_version": JEV_FOLLOWUP_SCHEMA_VERSION,
        }

    bundle = jev_followup_shards(items)
    shard_results: list[dict[str, Any]] = []
    failure = ""
    if bundle.get("shards"):
        try:
            shard_results = list(reviewer(bundle))
        except Exception as exc:  # noqa: BLE001 - 附加层绝不能拖垮验证点交付
            failure = f"{type(exc).__name__}: {exc}"[:300]
    findings = jev_followup_findings(shard_results)
    models = {str(entry.get("model")) for entry in shard_results if entry.get("model")}
    model = "/".join(sorted(models)) if models else ""

    rows: list[tuple[int, dict[str, Any]]] = []
    for position, item in enumerate(items):
        index = position + 1
        finding = findings.get(index)
        if finding is None:
            # 没进 state（超上限 / 超预算）或没拿到应答 → 如实标 unannotated，**不折叠**。
            reason = next(
                (entry["note"] for entry in bundle.get("skipped") or [] if entry.get("index") == index),
                "",
            )
            finding = {
                "score": None, "score_percent": None, "confidence": None,
                "level_label": "未分诊", "deferred": False,
                "triage_state": JEV_FOLLOWUP_STATE_UNANNOTATED,
                "note": reason or (
                    f"本轮未取得应答（{failure}），未分诊——按不折叠处理"
                    if failure else "本轮未送评，未分诊——按不折叠处理"
                ),
            }
        item["triage"] = {**finding, "model": model, "schema_version": JEV_FOLLOWUP_SCHEMA_VERSION}
        rows.append((position, finding))

    order = _followup_display_order(rows)
    rank_by_position = {position: rank for rank, position in enumerate(order)}
    for position, _finding in rows:
        items[position]["triage"]["rank"] = rank_by_position[position]

    deferred = sum(1 for _position, finding in rows if finding.get("deferred"))
    unannotated = sum(
        1 for _position, finding in rows
        if finding.get("triage_state") == JEV_FOLLOWUP_STATE_UNANNOTATED
    )
    return items, {
        "available": True,
        "total": len(items),
        "deferred": deferred,
        "unannotated": unannotated,
        "order": [position + 1 for position in order],
        "skipped": list(bundle.get("skipped") or []),
        "model": model,
        "levels": list(JEV_FOLLOWUP_LEVEL_LABELS),
        "confidence_floor": JEV_FOLLOWUP_CONFIDENCE_FLOOR,
        "note": note or (
            "验证点优先级由 Jev 分诊（是否可证伪 / 是否已有证据覆盖 / 是否影响决策）；"
            "只给排序与折叠，不改写验证点原文、不删除条目。"
            "「未分诊」表示这一轮没有送出去评，不等于「不值得查」。"
        ),
        "schema_version": JEV_FOLLOWUP_SCHEMA_VERSION,
    }
