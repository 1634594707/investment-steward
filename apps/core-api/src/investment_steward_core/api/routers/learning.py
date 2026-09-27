"""学习闭环域路由：每日学习单元、学习目标、学习活动与「反思 → 原则草案」。

A06（架构改进路线图 2026-09-25）第三批：把 7 条路由与它们的请求模型从 `api/app.py`
整体迁出。**只搬位置、不改行为**：函数体、状态码与错误响应形状逐字保留；迁移前后的
真实路由清单由 `scripts/route_inventory.py` 比对，必须完全一致。

边界：本域只经 `learning` 服务模块与 `database` 读写学习单元/目标/活动，以及由反思
产出**投资原则草案**（draft）。红线不变——「学习完成」不会自动改变风险权限：产出的
始终是 draft，必须走 `/investment-policies/{id}/confirm` 由用户显式确认后才成为 active。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from investment_steward_core.domain import (
    InvestmentPolicyVersion,
    LearningActivity,
    LearningGoal,
    LearningUnit,
    LearningUnitType,
    PolicyStatus,
)
from investment_steward_core.learning import (
    build_current_learning_goal,
    build_today_learning_unit,
)


class LearningActivityRequest(BaseModel):
    unit_id: UUID
    unit_type: str = "exercise"
    bound_instrument: str | None = None
    bound_instrument_label: str | None = None
    objective: str = Field(min_length=1, max_length=2000)
    user_answer: str = Field(default="", max_length=10000)
    reflection: str = Field(default="", max_length=10000)


def build_learning_router(
    require_session: Callable[..., Any],
    audit: Callable[..., None],
) -> APIRouter:
    """构造学习闭环路由（依赖由 `api/app.py` 注入，见 `api/deps.py` 说明）。"""
    router = APIRouter()

    @router.get("/learning/unit/today", response_model=LearningUnit | None)
    def today_learning_unit(core: Any = Depends(require_session)) -> LearningUnit | None:
        unit = build_today_learning_unit(core.database, core.local_user_id)
        audit(core, "learning.unit.produced", "learning_unit", str(unit.unit_id) if unit else "none")
        return unit

    @router.get("/learning/goals/current", response_model=LearningGoal)
    def current_learning_goal(core: Any = Depends(require_session)) -> LearningGoal:
        goal = build_current_learning_goal(core.database, core.local_user_id)
        audit(core, "learning.goal.current", "learning_goal", str(goal.goal_id))
        return goal

    @router.get("/learning/activities", response_model=list[LearningActivity])
    def list_learning_activities(core: Any = Depends(require_session)) -> list[LearningActivity]:
        return core.database.list_learning_activities(core.local_user_id)

    @router.post(
        "/learning/activities", response_model=LearningActivity, status_code=status.HTTP_201_CREATED
    )
    def create_learning_activity(
        body: LearningActivityRequest, core: Any = Depends(require_session)
    ) -> LearningActivity:
        activity = LearningActivity(
            activity_id=uuid4(),
            user_id=core.local_user_id,
            unit_id=body.unit_id,
            unit_type=LearningUnitType(body.unit_type),
            bound_instrument=body.bound_instrument,
            bound_instrument_label=body.bound_instrument_label,
            objective=body.objective,
            user_answer=body.user_answer,
            reflection=body.reflection,
        )
        core.database.upsert_learning_activity(activity)
        audit(core, "learning.activity.created", "learning_activity", str(activity.activity_id))
        return activity

    @router.delete("/learning/activities/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_learning_activity(activity_id: UUID, core: Any = Depends(require_session)) -> None:
        if core.database.get_learning_activity(activity_id, core.local_user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="learning activity not found"
            )
        core.database.delete_learning_activity(activity_id, core.local_user_id)
        audit(core, "learning.activity.deleted", "learning_activity", str(activity_id))

    @router.get("/learning/reflections/export", response_model=list[LearningActivity])
    def export_learning_reflections(core: Any = Depends(require_session)) -> list[LearningActivity]:
        """导出：仅导用户自己的学习活动与反思（含绑定的标的与日期），便于备份。"""
        audit(core, "learning.reflections.exported", "learning_activity", "all")
        return core.database.list_learning_activities(core.local_user_id)

    @router.post(
        "/learning/activities/{activity_id}/propose-policy-change",
        response_model=InvestmentPolicyVersion,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_policy_change_from_learning(
        activity_id: UUID, core: Any = Depends(require_session)
    ) -> InvestmentPolicyVersion:
        """学习 → 矛盾检测 → 原则草案：由一次已保存反思产出一份 DRAFT 原则修订。

        「学习完成」不会自动改变风险权限：产出的始终是 draft，必须走
        /investment-policies/{id}/confirm 由用户显式确认后才成为 active。
        """
        activity = core.database.get_learning_activity(activity_id, core.local_user_id)
        if activity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="learning activity not found"
            )
        if not activity.reflection.strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有可用的反思内容，暂无法据此建议原则变更",
            )
        existing = core.database.list_policies(core.local_user_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="尚无已确认的投资原则可供修订，请先建立投资原则",
            )
        latest = max(existing, key=lambda item: item.version)
        draft = InvestmentPolicyVersion(
            policy_id=uuid4(),
            user_id=core.local_user_id,
            version=latest.version + 1,
            status=PolicyStatus.DRAFT,
            investment_goal=latest.investment_goal,
            horizon_years=latest.horizon_years,
            liquidity_needs=latest.liquidity_needs,
            allowed_markets=latest.allowed_markets,
            allowed_asset_classes=latest.allowed_asset_classes,
            risk_boundaries=latest.risk_boundaries,
            preferred_methods=latest.preferred_methods,
            excluded_methods=latest.excluded_methods,
            observation_conditions=latest.observation_conditions,
            invalidation_conditions=latest.invalidation_conditions,
            change_reason=f"学习反思建议修订（来自学习活动 {activity.activity_id}）",
            source_learning_activity_ids=[activity.activity_id],
        )
        core.database.insert_policy(draft)
        audit(
            core,
            "learning.proposed_policy_change",
            "investment_policy",
            str(draft.policy_id),
        )
        return draft

    return router
