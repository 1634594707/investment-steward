"""UICard 通道（G1-4）：把既有账本数据派生为插槽卡，统一走 renderer 分发。

- 只派生「账本里真实存在」的信息（简报条目、学习单元、在研问题、需复核通知），
  不编造任何内容；无数据时返回空列表（不制造假卡）。
- L0 静默槽（notification.global）永不产卡，仅靠 /notifications/pending 与证据账本承载。
- 与遗留 `GET /brief/today`（BriefItem）双读共存：brief/today 保持原契约供已接前端，
  本通道按插槽仲裁后输出带 renderer 的 UICard。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from investment_steward_core.domain import (
    ActionMode,
    CardSeverity,
    Evidence,
    RunStatus,
    UICard,
    UiCardRenderer,
)
from investment_steward_core.learning import build_today_learning_unit
from investment_steward_core.notifications import evaluate_notifications, notification_dedup_key
from investment_steward_core.reporting import build_today_brief
from investment_steward_core.slots import resolve_renderer
from investment_steward_core.storage import Database

if TYPE_CHECKING:
    from investment_steward_core.config import CoreSettings

_L0_SILENT_SLOT = "notification.global"


def _action_mode_and_actions(signal: str | None) -> tuple[ActionMode, list[str]]:
    """把日报条目的信号字面量映射为 action_mode 与 supported_actions（对齐 ActionMode）。"""
    if signal in ("review_plan",):
        return ActionMode.REVIEW_PLAN, ["review_policy"]
    if signal in ("research",):
        return ActionMode.RESEARCH, ["create_plan"]
    if signal in ("observe",):
        return ActionMode.OBSERVE, ["open_evidence"]
    return ActionMode.NO_ACTION, []


def _merge_evidence_summaries(refs: list[UUID], evidence: list[Evidence]) -> list[str]:
    titles = [item.summary for item in evidence if item.evidence_id in refs]
    return titles[:3]


def cards_for_slot(
    db: Database, user_id: UUID, slot: str, settings: CoreSettings
) -> list[UICard]:
    """按插槽产出 UICard；L0 静默槽恒为空；无真实数据返回空列表。"""
    if slot == _L0_SILENT_SLOT:
        return []

    if slot == "today.brief":
        holdings = db.list_holdings(user_id)
        evidence = db.list_evidence(user_id)
        brief = build_today_brief(db, user_id, holdings, evidence)
        level = "L3"
        cards: list[UICard] = []
        for item in brief.items:
            action_mode, actions = _action_mode_and_actions(item.signal)
            severity = {
                "research": CardSeverity.ATTENTION,
                "review_plan": CardSeverity.WARNING,
            }.get(item.signal, CardSeverity.INFO)
            cards.append(
                UICard(
                    card_id=f"brief-{item.display_order}",
                    title=item.title,
                    summary=item.summary,
                    severity=severity,
                    evidence_refs=item.evidence_refs,
                    source_plugin=None,
                    slot=slot,
                    renderer=UiCardRenderer(resolve_renderer(level)) if resolve_renderer(level) != "silent" else None,
                    created_at=item.data_time or datetime.now(UTC),
                    action_mode=action_mode,
                    supported_actions=actions,
                    limitations=["日报条目由核心从本地证据账本派生，非插件输出。"],
                )
            )
        return cards

    if slot == "today.learning":
        cards: list[UICard] = []
        unit = build_today_learning_unit(db, user_id)
        if unit is not None:
            cards.append(
                UICard(
                    card_id=f"learning-{unit.unit_id}",
                    title=unit.title,
                    summary=unit.objective,
                    severity=CardSeverity.INFO,
                    evidence_refs=[],
                    source_plugin=unit.source_plugin_id,
                    slot=slot,
                    renderer=UiCardRenderer.SUMMARY_ROW,
                    created_at=unit.data_time,
                    action_mode=ActionMode.OBSERVE,
                    supported_actions=["start_learning"],
                    limitations=["学习单元由学习插件产出，仅入复盘流，不改变投资原则。"],
                )
            )
        # 研读图书馆荐读计划自动出现在今天页复盘流（G5-2）；仍只属学习流，不改原则。
        plans = db.list_library_plans()
        if plans:
            latest = plans[0]
            cards.append(
                UICard(
                    card_id=f"libraryplan-{latest.plan_id}",
                    title=f"今日荐读 · {latest.source_chapter}",
                    summary=f"{latest.rationale} 任务：{latest.daily_task}",
                    severity=CardSeverity.INFO,
                    evidence_refs=[],
                    source_plugin="official.reading-library",
                    slot=slot,
                    renderer=UiCardRenderer(latest.renderer),
                    created_at=latest.created_at,
                    action_mode=ActionMode.OBSERVE,
                    supported_actions=["start_learning"],
                    limitations=["荐读计划只进学习流，不改投资原则（G5-4）。"],
                )
            )
        return cards

    if slot == "research.board":
        runs = db.list_research_runs(user_id)
        active = [
            run
            for run in runs
            if run.status
            not in (
                RunStatus.COMPLETED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.STALE,
            )
        ]
        if not active:
            return []
        run = max(active, key=lambda item: item.created_at)
        return [
            UICard(
                card_id=f"research-{run.run_id}",
                title="研究问题待处理",
                summary=f"「{run.user_question[:120]}{'…' if len(run.user_question) > 120 else ''}」仍处于 {run.status.value}。",
                severity=CardSeverity.ATTENTION,
                evidence_refs=run.evidence_refs,
                source_plugin=None,
                slot=slot,
                renderer=UiCardRenderer.CARD,
                created_at=run.created_at,
                action_mode=ActionMode.RESEARCH,
                supported_actions=["create_plan"] if run.evidence_refs else [],
                limitations=["研究卡来自研究链路在研问题，非买卖建议。"],
            )
        ]

    if slot == "review.plan":
        notifications = evaluate_notifications(db, user_id)
        # JV08：被语义分诊判为低相关的条目不该在这里变成卡片——否则「不再弹通知」
        # 会在卡片槽位上漏出来，两处口径不一致。压制状态从**已落库**的通知上读
        # （`evaluate_notifications` 返回的是新构造对象，本身不带 triage）。
        suppressed_keys = {
            notification_dedup_key(item)
            for item in db.list_notifications(user_id)
            if item.triage is not None and item.triage.suppressed
        }
        reviewable = [
            n for n in notifications
            if n.action_mode == ActionMode.REVIEW_PLAN
            and notification_dedup_key(n) not in suppressed_keys
        ]
        if not reviewable:
            return []
        cards = []
        for n in sorted(reviewable, key=lambda item: item.created_at)[:1]:
            cards.append(
                UICard(
                    card_id=f"review-{n.notification_id}",
                    title=n.title,
                    summary=n.summary,
                    severity=CardSeverity.WARNING,
                    evidence_refs=n.evidence_refs,
                    source_plugin="official.notification-rules",
                    slot=slot,
                    renderer=UiCardRenderer.SUMMARY_ROW,
                    created_at=n.created_at,
                    action_mode=ActionMode.REVIEW_PLAN,
                    supported_actions=["review_policy"],
                    limitations=["由已确认条件与新证据触发，需人工复核。"],
                )
            )
        return cards

    # invest.market_view / app.library 当前无真实派生来源，返回空，避免编造行情或书目卡。
    return []
