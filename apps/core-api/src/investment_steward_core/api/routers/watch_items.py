"""观察事项域路由（8.3）：状态机 + 检查只追加 + dedup 防重复。

A06（架构改进路线图 2026-09-25）：Core 按域拆解的第一批——把 5 条路由与它们的
请求模型从 `api/app.py` 整体迁出。**只搬位置、不改行为**：函数体、状态码与错误响应
形状逐字保留；迁移前后的真实路由清单由 `scripts/route_inventory.py` 比对，必须完全一致。

读写边界：本域只经 `longterm_service` 的应用逻辑与 `database` 的存储读写观察事项本身，
不直接触碰其它域的表。因此它是「读写边界清楚的领域」的第一选择。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from investment_steward_core import longterm as longterm_service


class WatchItemCreateRequest(BaseModel):
    """观察事项（8.3）：观察指标/失效条件映射为可追踪事项。"""

    title: str = Field(min_length=1, max_length=200)
    indicator: str = Field(min_length=1, max_length=400)
    condition_text: str = Field(min_length=1, max_length=1000)
    check_cycle: str = Field(default="weekly", pattern="^(daily|weekly|monthly|manual)$")
    source_kind: str = Field(default="", max_length=40)
    source_id: UUID | None = None
    dedup_key: str = Field(default="", max_length=120)


class WatchCheckRequest(BaseModel):
    """检查记录（8.3）：只追加；dedup_value 相同的检查不重复计提醒。"""

    observed: str = Field(min_length=1, max_length=2000)
    triggered: bool = False
    note: str = Field(default="", max_length=2000)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=20)
    dedup_value: str | None = Field(default=None, max_length=100)


class WatchTransitionRequest(BaseModel):
    target: str = Field(pattern="^(active|paused|triggered|closed)$")


def build_watch_items_router(
    require_session: Callable[..., Any],
    audit: Callable[..., None],
) -> APIRouter:
    """构造观察事项路由。

    `require_session` / `audit` 由 `api/app.py` 注入（见 `api/deps.py` 的说明）：
    子模块不反向 import app.py，避免循环依赖。
    """
    router = APIRouter()

    # —— 观察事项（8.3）：状态机 + 检查只追加 + dedup 防重复 ——
    @router.post("/watch-items", status_code=status.HTTP_201_CREATED)
    def create_watch_item(
        body: WatchItemCreateRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        item = longterm_service.create_watch_item(
            core.database,
            user_id=core.local_user_id,
            title=body.title.strip(),
            indicator=body.indicator.strip(),
            condition_text=body.condition_text.strip(),
            check_cycle=body.check_cycle,
            source_kind=body.source_kind.strip(),
            source_id=body.source_id,
            dedup_key=body.dedup_key.strip(),
        )
        audit(core, "watch_item.created", "watch_item", str(item.watch_id))
        return item.model_dump(mode="json")

    @router.get("/watch-items")
    def list_watch_items(
        status_filter: str | None = None, core: Any = Depends(require_session)
    ) -> list[dict[str, object]]:
        if status_filter is not None and status_filter not in {
            "active",
            "paused",
            "triggered",
            "closed",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法状态:{status_filter}",
            )
        items = core.database.list_watch_items(core.local_user_id, status_filter)
        return [item.model_dump(mode="json") for item in items]

    @router.post("/watch-items/{watch_id}/checks")
    def record_watch_check(
        watch_id: UUID, body: WatchCheckRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        try:
            item, check, written = longterm_service.record_watch_check(
                core.database, core.local_user_id, watch_id,
                observed=body.observed, triggered=body.triggered,
                note=body.note, evidence_refs=body.evidence_refs,
                dedup_value=body.dedup_value,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return {
            "watch_item": item.model_dump(mode="json"),
            "check": check.model_dump(mode="json"),
            "written": written,
        }

    @router.get("/watch-items/{watch_id}/checks")
    def list_watch_checks(
        watch_id: UUID, core: Any = Depends(require_session)
    ) -> list[dict[str, object]]:
        if core.database.get_watch_item(watch_id, core.local_user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"观察事项不存在:{watch_id}"
            )
        return [c.model_dump(mode="json") for c in core.database.list_watch_checks(watch_id)]

    @router.post("/watch-items/{watch_id}/transition")
    def transition_watch_item(
        watch_id: UUID, body: WatchTransitionRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        try:
            item = longterm_service.transition_watch_item(
                core.database, core.local_user_id, watch_id, body.target
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        audit(core, "watch_item.transition", "watch_item", str(watch_id))
        return item.model_dump(mode="json")

    return router
