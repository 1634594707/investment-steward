from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from investment_steward_core import jev_client
from investment_steward_core.domain import ActionMode, Evidence, Notification, Thesis
from investment_steward_core.evidence_policy import as_utc, is_current_evidence
from investment_steward_core.instruments import subject_matches_instrument

if TYPE_CHECKING:  # 仅用于类型标注，避免与 storage 循环依赖
    from investment_steward_core.storage import Database


def notification_dedup_key(n: Notification) -> str:
    """通知的去重键：同一判断条件 + 同一锚定证据 → 视为同一条提醒，重复评估不产生重复行。

    锚定证据取 evidence_refs[0]；无证据引用则退化为条件原文（仍会去重，但不绑定具体证据）。
    """
    anchor = str(n.evidence_refs[0]) if n.evidence_refs else ""
    return f"{n.thesis_id or 'none'}:{n.condition_kind}:{n.triggered_by}:{anchor}"


def _condition_tokens(condition: str) -> set[str]:
    """条件关键词：抽取 4~5 字连续子串，仅用于与证据 summary 做包含匹配。

    纯子串（无分词库）难以对齐中文条件与证据措辞；改用 n-gram 保守匹配，
    避免单字/双字泛化噪音。
    """
    stripped = condition.replace("，", "").replace(",", "").replace(" ", "")
    tokens: set[str] = set()
    for n in (4, 5):
        tokens.update(stripped[i : i + n] for i in range(len(stripped) - n + 1))
    return tokens


def _evidence_matches(condition: str, evidence: Evidence) -> bool:
    tokens = _condition_tokens(condition)
    if not tokens:
        return False
    haystack = evidence.summary.replace(" ", "")
    return any(token in haystack for token in tokens)


def _recent(evidence: Evidence, now: datetime, window_hours: float) -> bool:
    return is_current_evidence(evidence, now=now) and timedelta(0) <= (now - as_utc(evidence.collected_at)) <= timedelta(hours=window_hours)


def _build_notification(
    user_id: UUID,
    thesis: Thesis,
    evidence: Evidence,
    condition: str,
    condition_kind: str,
    action_mode: ActionMode,
    now: datetime,
) -> Notification:
    suffix = "失效" if condition_kind == "invalidation_condition" else "观察"
    quote = evidence.summary if len(evidence.summary) <= 60 else f"{evidence.summary[:60]}…"
    return Notification(
        notification_id=uuid4(),
        user_id=user_id,
        thesis_id=thesis.thesis_id,
        instrument=thesis.instrument,
        triggered_by=condition,
        condition_kind=condition_kind,
        title=f"{thesis.instrument} 命中已确认{suffix}条件",
        summary=f"新证据「{quote}」与已确认条件「{condition}」匹配，建议复核。",
        action_mode=action_mode,
        evidence_refs=[evidence.evidence_id],
        created_at=now,
        data_time=evidence.collected_at,
    )


def evaluate_notifications(
    db: Database, user_id: UUID, window_hours: float = 48.0
) -> list[Notification]:
    """根据已确认 Thesis 的失效/支撑条件 × 最近证据，产出提醒。

    - 失效条件命中 → review_plan（需人工复核）
    - 支撑条件命中 → observe（维持观察）
    - 无 Thesis，或证据窗口内无任何匹配 → 返回空列表（不推送泛化噪音）
    """
    theses = db.list_theses(user_id)
    evidence = db.list_evidence(user_id)
    now = datetime.now(UTC)
    notifications: list[Notification] = []

    for thesis in theses:
        pairings = [
            ("invalidation_condition", thesis.invalidation_conditions, ActionMode.REVIEW_PLAN),
            ("supporting_condition", thesis.supporting_conditions, ActionMode.OBSERVE),
        ]
        for condition_kind, conditions, action_mode in pairings:
            for condition in conditions:
                matched = [
                    item
                    for item in evidence
                    if _recent(item, now, window_hours)
                    and any(subject_matches_instrument(ref, thesis.instrument) for ref in item.subject_refs)
                    and _evidence_matches(condition, item)
                ]
                latest = max(matched, key=lambda item: item.collected_at, default=None)
                if latest is not None:
                    notifications.append(
                        _build_notification(
                            user_id, thesis, latest, condition, condition_kind, action_mode, now
                        )
                    )
    return notifications


# ---------------------------------------------------------------------------
# JV08：证据巡逻通知分诊（纯函数层，出网由调用方注入）
#
# **要解决的问题**：`evaluate_notifications` 的匹配是「条件原文 n-gram × 证据摘要包含」
# 的纯文本启发式（见 `_evidence_matches`），命中即产通知。这在公告/新闻场景下会放过大量
# **措辞沾边但实质无关**的内容（纯行情播报、公关稿、行业泛泛报道、与已读信息重复）。
# 用户收到的不是「有用的提醒」，而是「提醒的噪音」。
#
# **本层的分工**：给每条待落库的通知打两道语义判定——
#   ① `choice` 是否**实质影响**该持仓/自选标的（这是「低相关」的定义）；
#   ② `score` 通知优先级（3 级有序 rubric）。
# 两个维度分开问，是因为官方「一题一维度」原则：相关性和紧迫性不是同一件事，
# 混成一题会让模型的概率摊平、confidence 失去意义。
#
# **软校验铁律**（与 `report_quality` / `tactics_ai` 同源）：
#   - 只**加标注**，绝不删除通知——被压制的条目仍然落库，可在通知中心「已分诊未通知」翻到；
#   - 只在**两个维度都指向「不值得打扰」且置信度达标**时才压制（见 `_triage_decision`）；
#   - 任何失败/缺答/低置信一律**放行**（宁可不压，也不静默吞掉真提醒）；
#   - 可整层关闭：Jev 未启用或总闸关闭时，本层一个字节都不产生。
#
# **不重复出网**：同一条通知（同 dedup 键）已经带分诊结论落过库的，直接复用结论、
# 不再送评——按小时桶反复评估时这条是成本红线（R1：只按输入 token 计费）。
# ---------------------------------------------------------------------------

JEV_TRIAGE_SCHEMA_VERSION = "1.0"

#: 逐票题 id 前缀：`n{index}_material` / `n{index}_priority`。`index` 是**跨分片的全局编号**。
JEV_TRIAGE_QUESTION_MATERIAL = "material"
JEV_TRIAGE_QUESTION_PRIORITY = "priority"

#: 相关性三选项。用描述性选项而不是「是/否」——「说不准」是真实且常见的一档，
#: 强行二值化会把「信息不足」逼成「不相关」，那就是静默吞提醒。
JEV_TRIAGE_IMPACT_OPTIONS: dict[str, str] = {
    "material": "实质影响：这条消息会改变对该标的的判断，或需要采取动作（业绩变动、政策监管、重大合同、风险事件、关键基本面数据变化）",
    "uncertain": "说不准：信息量不足，无法判断是否影响该标的（标题过短、缺关键数字、只给结论不给依据）",
    "immaterial": "不实质影响：与持仓逻辑无关（纯行情播报、公司公关稿、行业泛泛报道、与已读信息重复）",
}
JEV_TRIAGE_IMPACT_LABELS: dict[str, str] = {
    "material": "实质影响",
    "uncertain": "说不准",
    "immaterial": "不实质影响",
}

#: 优先级 3 级**描述性** rubric（官方：只给数字等级会让概率摊平、confidence 崩到 0.33）。
JEV_TRIAGE_PRIORITY_LEVELS: list[str] = [
    "不必通知：可以留档，但不值得打断用户",
    "常规通知：值得在通知中心看到，不必立刻处理",
    "优先通知：应当立刻引起注意（可能影响持仓判断或需要动作）",
]
JEV_TRIAGE_PRIORITY_LABELS: list[str] = ["不必通知", "常规通知", "优先通知"]

#: 分片上限。每票**两题**（JV05 是一票一题），所以票数上限减半以控制单请求题量。
JEV_TRIAGE_MAX_PER_SHARD = 20
#: 单轮最多分诊多少条。超出部分如实计入 `skipped`（不静默不标）。
JEV_TRIAGE_MAX_CANDIDATES = 100
#: state 预算：官方「state + 最长单题 ≤ 32k」，留足余量取 20k（与 JV05 同口径）。
JEV_TRIAGE_STATE_MAX_CHARS = 20_000
#: 单条通知块上限。超过按降级阶梯削减（见 `jev_triage_item_text`）。
JEV_TRIAGE_ITEM_MAX_CHARS = 1_600
JEV_TRIAGE_TITLE_CHARS = 120
#: 摘要的宽松上限（① 完整档）与收紧上限（② 起）。
JEV_TRIAGE_SUMMARY_CHARS = 600
JEV_TRIAGE_SUMMARY_TIGHT_CHARS = 300
#: 持仓上下文：核心假设最多带几条、每条截多长。只送判定必需的片段（§F 白名单纪律）。
JEV_TRIAGE_ASSUMPTION_LIMIT = 3
JEV_TRIAGE_ASSUMPTION_CHARS = 120

#: 「低置信 → 不压制」的地板线。
#:
#: ⚠️ **未标定**：R6 只标定了 `noul` 三路阈值与 `partial` 降级开关，本场景（`choice`/`score`
#: 的 confidence 分档）**没有中文样本**，故沿用官方英文范例的 0.5 地板线。这里的用法是
#: **保守方向**——置信度不达标就不压制（宁可多弹一条，也不静默吞掉真提醒），所以标定缺失
#: 的代价是「压制得偏少」而不是「误吞提醒」。拿到中文样本后应连同 JV06/JV07 一起重标。
JEV_TRIAGE_CONFIDENCE_FLOOR = 0.5

#: 分诊态：压掉 / 放行 / 没送出去评。与 JV05 一样，**「没评」不等于「通过」**。
JEV_TRIAGE_STATE_SUPPRESSED = "suppressed"
JEV_TRIAGE_STATE_NOTIFIED = "notified"
JEV_TRIAGE_STATE_UNANNOTATED = "unannotated"

_CONDITION_KIND_LABELS: dict[str, str] = {
    "invalidation_condition": "失效条件",
    "supporting_condition": "支撑条件",
    "observation_metric": "观察指标",
}


def _clip(text: Any, limit: int) -> str:
    """按字符数裁剪并加省略号（**只用于 state 内部的可选片段**，不用于判据块）。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}…"


def _sentence_trim(text: Any, limit: int) -> str:
    """按**完整句子**累积到 limit 内；第一句就超限则返回空串（表示这一级降级无效）。

    为什么不直接 `text[:limit]`：摘要尾部常带「影响判断」的关键限定（「……但公司否认」），
    截半句会让 Jev 拿到与原文不同的意思，从而给出自信但错误的「不实质影响」。
    这与 JV05「削内容而不截断」是同一条纪律。
    """
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    out = ""
    for chunk in _split_sentences(cleaned):
        if len(out) + len(chunk) > limit:
            break
        out += chunk
    return out


def _split_sentences(text: str) -> list[str]:
    """按中文句读切句（保留标点）。纯 stdlib，不引入分词库。"""
    pieces: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in "。！？；!?;\n":
            pieces.append(buffer)
            buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def jev_triage_context_text(context: Mapping[str, Any] | None) -> str:
    """「受影响标的的持仓上下文」白名单文本（§F 纪律：只送判定必需的片段）。

    白名单内：标的在持仓/自选（`HoldingStatus`）、标的标签、该标的的**核心假设**前若干条、
    以及触发这条通知的**已确认条件原文**（就是 `triggered_by`，已在标题/摘要之外单列，
    因为它是「为什么会有这条通知」的答案）。

    **明确排除**（每次改白名单都要重新过一遍这张表）：
      - `Holding.strategy_note`（用户自述的策略备注原文）——与「这条消息是否实质影响」无关，
        带上只是扩大出网面；需要时用户会自己在通知中心点开看。
      - 持仓数量 / 成本价 / 盈亏（`Holding` 里本就没有这些字段，这里是显式声明）。
      - 其他标的的假设与条件、账户信息、密钥。
    """
    if not context:
        return ""
    lines: list[str] = []
    status = str(context.get("holding_status") or "")
    label = str(context.get("label") or "")
    if status == "holding":
        lines.append(f"该标的在持仓中{f'（{label}）' if label else ''}")
    elif status == "watchlist":
        lines.append(f"该标的在自选观察中{f'（{label}）' if label else ''}")
    else:
        lines.append("该标的不在持仓也不在自选（仅由已确认判断触发）")

    conditions = [str(item).strip() for item in (context.get("conditions") or []) if str(item).strip()]
    if conditions:
        lines.append(f"触发条件：{'；'.join(_clip(item, 200) for item in conditions[:3])}")

    assumptions = [
        _clip(item, JEV_TRIAGE_ASSUMPTION_CHARS)
        for item in (context.get("core_assumptions") or [])
        if str(item).strip()
    ][:JEV_TRIAGE_ASSUMPTION_LIMIT]
    if assumptions:
        lines.append(f"核心假设：{'；'.join(assumptions)}")
    return "\n".join(lines)


def jev_triage_item_text(
    notification: Notification,
    *,
    context: Mapping[str, Any] | None = None,
    max_chars: int = JEV_TRIAGE_ITEM_MAX_CHARS,
) -> tuple[str, str | None]:
    """单条通知进 state 的文本块（标题 + 摘要 + 持仓上下文）。

    返回 `(文本, 降级说明)`；降级说明非 None 表示为了塞进预算削掉了什么，须如实透传。

    **降级阶梯**（顺序按「对判定的重要性」从低到高削）：
      ① 完整（标题 + 摘要全文 + 持仓上下文）；
      ② 摘要按**完整句子**截到 `JEV_TRIAGE_SUMMARY_CHARS` 内（保留持仓上下文）；
      ③ 去掉持仓上下文；
      ④ 仍超 → 返回空串，调用方计入 `skipped(over_item_budget)`。

    注意 ② 与 ③ 的**顺序**：持仓上下文（「为什么这条消息跟我的持仓有关」）比摘要的
    后半段更有判别力，所以先削摘要、后削上下文。
    """
    instrument = str(notification.instrument or "").strip()
    kind = _CONDITION_KIND_LABELS.get(notification.condition_kind, notification.condition_kind)
    context_text = jev_triage_context_text(context)

    def _build(*, summary_text: str, with_context: bool) -> str:
        lines = [
            f"【标的】{instrument or '（未绑定标的）'}",
            f"【通知类型】{kind}",
            f"【标题】{_clip(notification.title, JEV_TRIAGE_TITLE_CHARS)}",
            f"【摘要】{summary_text}",
        ]
        if with_context and context_text:
            lines.append(f"【持仓上下文】\n{context_text}")
        return "\n".join(lines)

    # ① 完整：摘要按宽松上限裁剪（`_clip` 会加省略号，明确告诉模型「这里被截过」），
    #    持仓上下文全带。
    full = _build(
        summary_text=_clip(notification.summary, JEV_TRIAGE_SUMMARY_CHARS), with_context=True
    )
    if len(full) <= max_chars:
        return full, None

    # ② 摘要改按**完整句子**收紧（不截半句），持仓上下文保留。
    #    句子级收紧失败（第一句就超限）时退回硬裁剪——但**必须如实标注**这一级降级。
    tightened = _sentence_trim(notification.summary, JEV_TRIAGE_SUMMARY_TIGHT_CHARS)
    sentence_ok = bool(tightened)
    if not sentence_ok:
        tightened = _clip(notification.summary, JEV_TRIAGE_SUMMARY_TIGHT_CHARS)
    trimmed = _build(summary_text=tightened, with_context=True)
    if len(trimmed) <= max_chars:
        return trimmed, (
            "为塞进预算已按整句截短摘要"
            if sentence_ok
            else "为塞进预算已硬截摘要（摘要首句即超限，无法按整句收紧）"
        )

    # ③ 再去掉持仓上下文（它比摘要后半段更有判别力，所以排在后面削）。
    without_context = _build(summary_text=tightened, with_context=False)
    if len(without_context) <= max_chars:
        return without_context, "为塞进预算已截短摘要并去掉持仓上下文"

    # ④ 仍超 → 整条跳过，不硬塞半截（与 JV04 的 `over_state_budget` 同一条纪律）。
    return "", "通知标题与摘要超预算，整条未送评"


def jev_triage_questions(evaluated: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """为一票生成两道题：相关性（choice）+ 优先级（score）。

    两题分开而不是合成一题，是官方「一题一维度」原则：相关性与紧迫性不是同一件事，
    混成一题会让模型把概率摊在两轴上、confidence 失去意义。
    """
    questions: dict[str, dict[str, Any]] = {}
    for item in evaluated:
        index = item["index"]
        questions[f"n{index}_{JEV_TRIAGE_QUESTION_MATERIAL}"] = jev_client.choice_question(
            "这条消息是否**实质影响**该标的的持仓/观察逻辑？只看消息内容本身与给出的持仓上下文，"
            "不要引入外部行情或你自己的预测。纯行情播报、公司公关稿、行业泛泛报道、"
            "与已读信息重复的内容都算「不实质影响」；信息量不足无法判断时选「说不准」，不要硬判。",
            JEV_TRIAGE_IMPACT_OPTIONS,
        )
        questions[f"n{index}_{JEV_TRIAGE_QUESTION_PRIORITY}"] = jev_client.score_question(
            "这条消息对用户的**通知优先级**有多高？按「是否值得打断用户」判断，"
            "不要因为消息本身很长或很正式就抬高级别。",
            JEV_TRIAGE_PRIORITY_LEVELS,
        )
    return questions


def jev_triage_shards(
    items: Sequence[Mapping[str, Any]],
    *,
    max_per_shard: int = JEV_TRIAGE_MAX_PER_SHARD,
    max_chars: int = JEV_TRIAGE_STATE_MAX_CHARS,
    item_max_chars: int = JEV_TRIAGE_ITEM_MAX_CHARS,
) -> dict[str, Any]:
    """把待分诊通知切成若干分片，每片一份 state + 题面（与 `jev_scan_shards` 同构）。

    `items` 每项：`{"dedup_key", "notification", "context"}`。

    返回 `{"shards": [...], "skipped": [...]}`，其中

      shard   = `{"state", "questions", "evaluated": [{"index", "dedup_key", "instrument"}]}`
      skipped = `{"dedup_key", "reason", "note"}`，reason ∈
                `over_candidate_limit` / `over_item_budget` / `empty_state`

    分片策略与 JV05 一致（双重上限：每片 ≤ `max_per_shard` 条 **且** ≤ `max_chars` 字 state；
    一票放不进当前片就开新片而不是挤掉；`index` 跨分片全局编号）。
    """
    shards: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocks: list[tuple[Mapping[str, Any], int, str]] = []

    for position, item in enumerate(items):
        dedup_key = str(item.get("dedup_key") or "")
        if position >= JEV_TRIAGE_MAX_CANDIDATES:
            skipped.append({
                "dedup_key": dedup_key,
                "reason": "over_candidate_limit",
                "note": f"超出单轮分诊上限 {JEV_TRIAGE_MAX_CANDIDATES} 条，本轮未送评",
            })
            continue
        notification = item.get("notification")
        text, degraded = jev_triage_item_text(
            notification, context=item.get("context"), max_chars=item_max_chars
        )
        if not text:
            skipped.append({
                "dedup_key": dedup_key,
                "reason": "over_item_budget" if notification is not None else "empty_state",
                "note": degraded or "无可送评文本",
            })
            continue
        blocks.append((item, len(blocks) + 1, text))

    current: list[tuple[Mapping[str, Any], int, str]] = []
    used = 0

    def _flush() -> None:
        nonlocal current, used
        if not current:
            return
        evaluated = [
            {
                "index": index,
                "dedup_key": str(item.get("dedup_key") or ""),
                "instrument": str(getattr(item.get("notification"), "instrument", "") or ""),
            }
            for item, index, _text in current
        ]
        shards.append({
            "state": "\n\n".join(f"【通知 {index}】\n{text}" for _item, index, text in current),
            "questions": jev_triage_questions(evaluated),
            "evaluated": evaluated,
        })
        current = []
        used = 0

    for item, index, text in blocks:
        block_len = len(text) + 24  # 「【通知 N】」前缀与分隔
        if current and (len(current) >= max_per_shard or used + block_len > max_chars):
            _flush()
        current.append((item, index, text))
        used += block_len
        if len(current) >= max_per_shard:
            _flush()
    _flush()

    return {"shards": shards, "skipped": skipped}


def _triage_decision(
    impact: str | None, priority: int | None, confidence: float | None
) -> tuple[bool, str]:
    """压制规则（**硬编码且有据可查**，不是手感阈值）。

    压制需要**三个条件同时成立**：

      1. `impact == "immaterial"`（模型明确判「不实质影响」）；
      2. `priority == 0`（模型明确判「不必通知」）；
      3. `confidence` 有值且 ≥ `JEV_TRIAGE_CONFIDENCE_FLOOR`。

    为什么要三个都满足（而不是任一）：两个维度是独立问的，**任一维度不确定就放行**——
    静默吞掉一条真提醒的代价，远大于多弹一条低相关提醒的代价。缺 confidence 同样放行
    （模型自己没把握时我们不做减法）。

    三个条件都成立仍被压制的条目**不会消失**：它照常落库，只是不进站内默认列表、不外发，
    可在通知中心「已分诊未通知」翻到（`JEV_TRIAGE_STATE_SUPPRESSED`）。
    """
    if impact != "immaterial":
        return False, "相关性未判为「不实质影响」，放行"
    if priority != 0:
        return False, "优先级未判为「不必通知」，放行"
    if confidence is None:
        return False, "未返回置信度，无法确认，放行（宁可多弹一条）"
    if confidence < JEV_TRIAGE_CONFIDENCE_FLOOR:
        return False, (
            f"置信度 {confidence:.2f} 低于地板 {JEV_TRIAGE_CONFIDENCE_FLOOR:.2f}，放行（宁可多弹一条）"
        )
    return True, f"判为低相关且不必通知（置信度 {confidence:.2f}），不弹站内、不外发，留痕可查"


def jev_triage_findings(shard_results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """把各分片的应答合并成逐条分诊结论：`{dedup_key: {...}}`。

    `shard_results` 每项：`{"evaluated": [...], "answers": {question_id: JevAnswer}, "error": str | None}`。

    合并规则（**宁可放行也不静默吞**）：
      - 分片带 `error` → 全部标 `unannotated` + 失败原因，**放行**（不压制）；
      - 题号缺答 / 选项未定义 → 同上；
      - 两道题都有有效答案 → 走 `_triage_decision`。

    `confidence` 取两题里的**较小值**：两题独立评估，取小是保守方向（置信度越低越不该压制）。
    """
    findings: dict[str, dict[str, Any]] = {}
    for result in shard_results:
        error = result.get("error")
        answers = result.get("answers") or {}
        for item in result.get("evaluated") or []:
            dedup_key = str(item.get("dedup_key") or "")
            if not dedup_key:
                continue
            if error:
                findings[dedup_key] = {
                    "impact": None, "impact_label": "未取得判定",
                    "priority": None, "priority_label": "未取得判定", "priority_percent": None,
                    "confidence": None, "suppressed": False,
                    "triage_state": JEV_TRIAGE_STATE_UNANNOTATED,
                    "note": f"本条所在分片未取得应答（{error}），未分诊——按放行处理",
                }
                continue
            index = item.get("index")
            material_answer = answers.get(f"n{index}_{JEV_TRIAGE_QUESTION_MATERIAL}")
            priority_answer = answers.get(f"n{index}_{JEV_TRIAGE_QUESTION_PRIORITY}")
            impact = getattr(material_answer, "choice", None)
            priority_raw = getattr(priority_answer, "score", None)
            if impact not in JEV_TRIAGE_IMPACT_OPTIONS or not isinstance(priority_raw, (int, float)):
                findings[dedup_key] = {
                    "impact": None, "impact_label": "未取得判定",
                    "priority": None, "priority_label": "未取得判定", "priority_percent": None,
                    "confidence": None, "suppressed": False,
                    "triage_state": JEV_TRIAGE_STATE_UNANNOTATED,
                    "note": "本条未取得完整应答（题号缺答或选项未定义），未分诊——按放行处理",
                }
                continue
            priority = round(float(priority_raw))
            priority = max(0, min(priority, len(JEV_TRIAGE_PRIORITY_LEVELS) - 1))
            confidences = [
                value for value in (
                    getattr(material_answer, "confidence", None),
                    getattr(priority_answer, "confidence", None),
                ) if isinstance(value, (int, float))
            ]
            confidence = float(min(confidences)) if confidences else None
            suppressed, note = _triage_decision(impact, priority, confidence)
            percent = round(priority / (len(JEV_TRIAGE_PRIORITY_LEVELS) - 1) * 100)
            findings[dedup_key] = {
                "impact": impact,
                "impact_label": JEV_TRIAGE_IMPACT_LABELS[impact],
                "priority": priority,
                "priority_label": JEV_TRIAGE_PRIORITY_LABELS[priority],
                "priority_percent": percent,
                "confidence": confidence,
                "suppressed": suppressed,
                "triage_state": (
                    JEV_TRIAGE_STATE_SUPPRESSED if suppressed else JEV_TRIAGE_STATE_NOTIFIED
                ),
                "note": note,
            }
    return findings


def jev_triage_summary(notifications: Sequence[Notification]) -> dict[str, Any]:
    """分诊统计行（纯函数，与落库方式解耦——测试可直接喂构造对象）。"""
    suppressed = notified = unannotated = 0
    impacts = {"material": 0, "uncertain": 0, "immaterial": 0}
    priorities = {"0": 0, "1": 0, "2": 0}
    for notification in notifications:
        triage = notification.triage
        if triage is None:
            unannotated += 1
            continue
        if triage.triage_state == JEV_TRIAGE_STATE_SUPPRESSED:
            suppressed += 1
        elif triage.triage_state == JEV_TRIAGE_STATE_NOTIFIED:
            notified += 1
        else:
            unannotated += 1
        if triage.impact in impacts:
            impacts[triage.impact] += 1
        if triage.priority is not None and str(triage.priority) in priorities:
            priorities[str(triage.priority)] += 1
    return {
        "total": len(notifications),
        "notified": notified,
        "suppressed": suppressed,
        "unannotated": unannotated,
        "impacts": impacts,
        "priorities": priorities,
    }
