from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_steward_core.domain import (
    Holding,
    LearningActivity,
    LearningGoal,
    LearningUnit,
    LearningUnitType,
    Thesis,
)
from investment_steward_core.storage import Database

_WEEKLY_TARGET = 3
_PLUGIN_ID = "official.investing-learning"
_PLUGIN_RELEASE = "0.1.0"


def _current_week_label(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _thesis_for_instrument(theses: list[Thesis], instrument: str) -> Thesis | None:
    return next((item for item in theses if item.instrument == instrument), None)


def _build_exercise_for(
    user_id: UUID, holding: Holding, thesis: Thesis | None, now: datetime
) -> LearningUnit:
    if thesis is not None and (thesis.supporting_conditions or thesis.invalidation_conditions):
        conditions = thesis.supporting_conditions + thesis.invalidation_conditions
        return LearningUnit(
            unit_id=uuid4(),
            user_id=user_id,
            unit_type=LearningUnitType.EXERCISE,
            title=f"核对「{holding.label}」的投资逻辑条件",
            objective="用证据核对自身持仓的投资逻辑条件，练习只做判断、不下买卖指令。",
            content=(
                f"以下是你在投资逻辑中确认过的 {len(conditions)} 条观察/失效条件。请逐条用自己的话重述，"
                "并结合你能看到的证据，判断当前更接近「被支持」「被证伪」还是「证据不足」。"
            ),
            guidance=(
                "不给出买卖建议；把每条条件拆成「如果看到什么，就说明接近触发」。"
                "若证据不足以判断，如实写「证据不足」，不要补出你自己都不知道的结论。"
            ),
            bound_instrument=holding.instrument,
            bound_instrument_label=holding.label,
            related_conditions=conditions,
            source_plugin_id=_PLUGIN_ID,
            source_plugin_release=_PLUGIN_RELEASE,
            data_time=now,
        )
    return LearningUnit(
        unit_id=uuid4(),
        user_id=user_id,
        unit_type=LearningUnitType.EXERCISE,
        title=f"为「{holding.label}」起草观察框架",
        objective="为自选/持仓标的建立可被证伪的观察指标与失效条件，练习只做判断、不下买卖指令。",
        content=(
            "这一步不改变任何投资原则，只练习为一个标的写出「可以核对」的观察指标，"
            "以及「若发生则说明原判断失效」的条件。"
        ),
        guidance=(
            "提示：观察指标应可量化（如估值分位、盈利同比、波动率区间），失效条件要写到"
            "「看到什么就算触发」，避免含糊的「表现不好」。完成后如实保存你的文字。"
        ),
        bound_instrument=holding.instrument,
        bound_instrument_label=holding.label,
        related_conditions=[],
        source_plugin_id=_PLUGIN_ID,
        source_plugin_release=_PLUGIN_RELEASE,
        data_time=now,
    )


def build_today_learning_unit(db: Database, user_id: UUID) -> LearningUnit | None:
    """今日学习单元：无持仓/自选时返回 None（插槽空态），有则绑定真实标的产出练习。"""
    holdings = db.list_holdings(user_id)
    if not holdings:
        return None
    theses = db.list_theses(user_id)
    anchor = holdings[0]
    thesis = _thesis_for_instrument(theses, anchor.instrument)
    return _build_exercise_for(user_id, anchor, thesis, datetime.now(UTC))


def build_current_learning_goal(db: Database, user_id: UUID) -> LearningGoal:
    """本周学习目标：以本周已完成学习活动数为进度，目标为完成 N 个与自身持仓相关的练习。"""
    now = datetime.now(UTC)
    period_label = _current_week_label(now)
    year, week, _ = now.isocalendar()

    completed = 0
    for activity in db.list_learning_activities(user_id):
        ad = activity.completed_at
        if (ad.year, ad.isocalendar()[1]) == (year, week):
            completed += 1

    existing = db.list_learning_goals(user_id)
    goal = next((item for item in existing if item.period_label == period_label), None)
    if goal is None:
        goal = LearningGoal(
            goal_id=uuid4(),
            user_id=user_id,
            period_label=period_label,
            target_count=_WEEKLY_TARGET,
            completed_count=completed,
            updated_at=now,
        )
    else:
        goal = goal.model_copy(update={"completed_count": completed, "updated_at": now})
    db.upsert_learning_goal(goal)
    return goal


def upsert_activity_from_reflection(
    db: Database, user_id: UUID, activity: LearningActivity
) -> LearningActivity:
    """保存一次学习活动与反思（绑定用户自身持仓）；删除/导出复用 storage 方法。"""
    db.upsert_learning_activity(activity)
    return activity