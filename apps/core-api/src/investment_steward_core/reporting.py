from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_steward_core.domain import (
    BriefItem,
    Evidence,
    EvidenceRelation,
    EvidenceStatus,
    FreshnessPatrolResult,
    Holding,
    ResearchRun,
    RunStatus,
    Thesis,
    TodayBrief,
    WeeklyReview,
    WeeklySection,
)
from investment_steward_core.evidence_policy import as_utc, is_current_evidence
from investment_steward_core.instruments import subject_matches_instrument
from investment_steward_core.storage import Database


def _instrument_of_holding(holding: Holding) -> str:
    """Holding.instrument（如 CN:ETF:510300）与 Evidence.subject_refs 中的 instrument: 前缀对齐。"""
    return holding.instrument


def _evidence_for_instrument(evidence_list: list[Evidence], instrument: str) -> list[Evidence]:
    """筛选与指定 instrument 直接相关的证据（subject_refs 中带 instrument:<instrument>）。"""
    return [
        item
        for item in evidence_list
        if is_current_evidence(item)
        and any(subject_matches_instrument(subject, instrument) for subject in item.subject_refs)
    ]


def build_today_brief(
    db: Database, user_id: UUID, holdings: list[Holding], evidence_list: list[Evidence]
) -> TodayBrief:
    """Today Brief 日报：只讲与持仓/自选直接相关的变化；无持仓时不制造虚假个性化。"""
    brief_id = uuid4()
    now = datetime.now(UTC)

    if not holdings:
        return TodayBrief(
            brief_id=brief_id,
            user_id=user_id,
            generated_at=now,
            has_personalization=False,
            headline="今天没有与你持有标的相关的变化可汇报。",
            items=[],
            empty_reason="本地账本中暂无持仓/自选记录，不基于猜测生成个性化内容。",
        )

    items: list[BriefItem] = []
    order = 0
    for holding in holdings:
        instrument = _instrument_of_holding(holding)
        instrument_evidence = _evidence_for_instrument(evidence_list, instrument)
        fresh = instrument_evidence
        if not instrument_evidence:
            continue
        latest = max(instrument_evidence, key=lambda item: as_utc(item.collected_at))
        contradicting = [item for item in instrument_evidence if item.relation == EvidenceRelation.CONTRADICTING]

        title = f"{holding.label}：有 {len(fresh)} 条在案证据。"
        if contradicting:
            signal = "research"
            summary = f"发现 {len(contradicting)} 条相反证据，建议先研究澄清后再判断，不改变既有投资原则。"
        else:
            signal = "observe"
            summary = "证据未出现相反信号，维持观察，等待更多数据验证原判断。"
        items.append(
            BriefItem(
                display_order=order,
                title=title,
                summary=summary,
                related_instrument=instrument,
                signal=signal,
                evidence_refs=[latest.evidence_id, *[e.evidence_id for e in contradicting]],
                data_time=latest.collected_at,
            )
        )
        order += 1

    has_personalization = bool(items)
    if not has_personalization:
        return TodayBrief(
            brief_id=brief_id,
            user_id=user_id,
            generated_at=now,
            has_personalization=False,
            headline="你有持仓，但本地证据账本里尚无与它们直接相关的变化。",
            items=[],
            empty_reason="持仓已记录，但缺少与之匹配的证据；接入数据供应商后才会产生真实变化。",
        )

    headline = f"今天与你有直接关系的标的有 {len(items)} 项，可在下方查看对应的证据与建议动作。"
    return TodayBrief(
        brief_id=brief_id,
        user_id=user_id,
        generated_at=now,
        has_personalization=True,
        headline=headline,
        items=items,
    )


def build_weekly_review(
    db: Database,
    user_id: UUID,
    theses: list[Thesis],
    runs: list[ResearchRun],
) -> WeeklyReview:
    """Weekly Review 周报：逻辑变化、未完成研究、决定与结果、下周建议。原则变更只给提示，不走自动修改。"""
    now = datetime.now(UTC)
    period_label = f"本周复盘（截至 {now.date().isoformat()}）"

    thesis_changes = [
        f"「{item.instrument}」投资逻辑当前为 {item.status.value}："
        f"{item.original_statement[:60]}{'…' if len(item.original_statement) > 60 else ''}"
        for item in theses
    ]

    open_research = [
        f"研究「{run.user_question[:80]}」仍处于 {run.status.value}，尚未形成有据回答。"
        for run in runs
        if run.status not in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED)
    ]
    if not open_research:
        open_research = ["本周没有未完成的研究问题。"]

    decisions = db.list_decisions(user_id)
    decisions_summary = [
        f"「{item.theme}」决定记录：{item.decision_summary[:60]}{'…' if len(item.decision_summary) > 60 else ''}"
        + (f"｜结果：{item.outcome[:40]}" if item.outcome else "")
        for item in decisions
    ]
    if not decisions_summary:
        decisions_summary = ["本周没有新的决定记录。"]

    suggestions: list[str] = []
    if any(run.status not in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED) for run in runs):
        suggestions.append("先完成未解决的研究问题，再考虑是否调整投资原则。")
    if theses:
        suggestions.append("把「当时依据 → 结果 → 事后评价」补全到最近的决策记录。")
    if not suggestions:
        suggestions.append("暂无可生成的下周建议，可先建立一项持仓或自选，让日报与周报有依据。")

    sections = [
        WeeklySection(key="thesis", title="投资逻辑变化", lines=thesis_changes),
        WeeklySection(key="research", title="未完成研究", lines=open_research),
        WeeklySection(key="decisions", title="决定与结果对照", lines=decisions_summary),
        WeeklySection(key="next", title="下周建议", lines=suggestions),
    ]
    principle_change_reason = (
        "本周复盘未自动修改投资原则；若需要调整，应走显式确认流程。"
        if theses
        else "暂无已确认的投资逻辑，尚未生成原则调整建议。"
    )

    return WeeklyReview(
        review_id=uuid4(),
        user_id=user_id,
        generated_at=now,
        period_label=period_label,
        thesis_changes=thesis_changes,
        open_research=open_research,
        decisions_summary=decisions_summary,
        next_week_suggestions=suggestions,
        principle_change_reason=principle_change_reason,
        sections=sections,
    )


def build_freshness_patrol(db: Database, user_id: UUID) -> FreshnessPatrolResult:
    """证据新鲜度巡检：找出 `valid_until` 已到且未被标记 stale 的证据，只报告不改写账本。

    尊重证据不可变不变量（蓝图），巡检只产出审计用快照，不自动重拉、不修改 `status`。
    """
    now = datetime.now(UTC)
    evidence = db.list_evidence(user_id)
    stale_ids = [
        item.evidence_id
        for item in evidence
        if item.valid_until is not None and item.valid_until < now and item.status != EvidenceStatus.STALE
    ]
    return FreshnessPatrolResult(checked=len(evidence), stale=len(stale_ids), stale_ids=stale_ids)
