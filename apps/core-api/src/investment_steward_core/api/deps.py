"""Core 路由的共享依赖：状态取用、会话鉴权、审计写入。

A06（架构改进路线图 2026-09-25）：这三个函数原先定义在 10k 行的 `api/app.py` 里，
被 232 处路由引用。按域拆解路由时子路由模块也需要它们，而直接 import `app.py`
会形成「app → routers → app」的循环依赖，因此抽到本模块：`app.py` 与 `api/routers/*`
都从这里取。

迁移**只搬位置、不改行为**：函数体、签名、异常与状态码逐字保留（`app.py` 以
`_state` / `_require_session` / `_audit` 别名导入，232 处调用点无需改动）。

`state` / `require_session` 的返回类型标注为 `Any`：真实类型是 `CoreState`，
而它定义在 `api/app.py`（依赖 `_plugin_manifests` 等模块级函数），在此处 import 会重新
引入循环。这是本次拆解的已知边界——把 `CoreState` 一并迁出属于下一批工作。
"""
from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Header, HTTPException, Request, status

from investment_steward_core.domain import AuditEvent

logger = logging.getLogger(__name__)


def state(request: Request) -> Any:
    """取当前应用的 CoreState（`app.state.core`）。"""
    return request.app.state.core


def require_session(
    request: Request,
    x_core_session_token: Annotated[str | None, Header()] = None,
) -> Any:
    """校验本地会话令牌，失败返回 401。所有业务路由都经此守卫。

    令牌比对必须整体包进 try：`secrets.compare_digest` 遇到含非 ASCII 字符的 `str`
    会抛 `TypeError`（"comparing strings with non-ASCII characters is not supported"），
    而 Starlette 以 latin-1 解码头值——任一非 ASCII 字节都会变成非 ASCII `str`。
    未捕获时该 TypeError 会一路逃逸到 Starlette 的兜底处理，表现为裸 500
    「Internal Server Error」，把「令牌不匹配」伪装成「服务器故障」，同时在 Core
    控制台刷 traceback。此处按「令牌无效」处理并返回 401。
    """
    core = state(request)
    if x_core_session_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid local session"
        )
    try:
        matched = secrets.compare_digest(x_core_session_token, core.settings.session_token)
    except TypeError:
        # 非 ASCII 头值：按不匹配处理，绝不让它变成 500。
        matched = False
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid local session"
        )
    return core


def audit(
    core: Any,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """写一条审计事件。

    E05（桌面端升级路线图 2026-09-18）：每自然日首次关键写入前触发一次在线备份
    （节流、失败静默）——补齐「长会话期间一次备份都不会产生」的缺口；检查有内存缓存，
    非每日首写零开销。
    """
    try:
        core.database.maybe_daily_backup()
    except Exception:
        # 原实现是静默 pass；这里补一条日志，保持「不阻断」语义的同时留下可诊断痕迹。
        logger.warning("每日备份触发失败（已忽略，不影响主写入）", exc_info=True)
    core.database.append_audit(
        AuditEvent(
            event_id=uuid4(),
            tenant_id=core.local_user_id,
            actor_id=core.local_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload or {},
        )
    )


def collaborators() -> Any:
    """返回 `api.app` 模块本身：按域拆出的路由经它**按调用时**取协作者实现。

    为什么必须绕这一层（A06 通知域批次实测）：本仓库测试统一 patch
    `investment_steward_core.api.app.<协作者>` 来注入替身（`resolve_store` 34 处、
    `evaluate_notifications` 14 处、`deliver_notification` 7 处、`digest_window` 5 处），
    而且**多数是在 `create_app()` 之后**才 patch（fixture 先建 app，测试体内再改）。

    因此子路由有两种做法都不成立：
    1. import 期把协作者绑进自己的命名空间 —— 补丁打在 app.py 上，子路由看不到；
    2. `build_*_router()` 时按值捕获 —— 捕获发生在 create_app 内，早于测试的 patch。

    唯一稳的做法就是每次调用时从 `api.app` 的命名空间取。代价是 `app.py` 必须把迁出去的、
    会被 patch 的符号继续 re-export（见 app.py 的「测试补丁点 re-export」段）。
    """
    from investment_steward_core.api import app as api_app

    return api_app
