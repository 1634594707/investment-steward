"""B04 / H02（桌面端升级路线图 2026-09-18）：路由必须唯一注册。

锁定的缺陷：`POST /sync/pairing-code` 曾被注册两次且函数同名，FastAPI 按注册顺序取先者，
后一份（带 `ttl_seconds` 回传与异常兜底）**永不执行**——这类重复只在真机调用时才暴露，
而 `api/app.py` 有 210 条路由、一个文件 8 千行，靠眼睛看不出来。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


def _routes(tmp_path: Path) -> list[APIRoute]:
    app = create_app(CoreSettings(session_token="route-uniqueness-token", data_dir=tmp_path))
    return [route for route in app.routes if isinstance(route, APIRoute)]


def test_no_duplicated_method_path(tmp_path):
    keys = [
        f"{method} {route.path}"
        for route in _routes(tmp_path)
        for method in sorted(route.methods & {"GET", "POST", "PUT", "DELETE", "PATCH"})
    ]
    duplicated = sorted(key for key, count in Counter(keys).items() if count > 1)
    assert duplicated == [], f"重复注册的路由（后注册者永不执行）：{duplicated}"


def test_sync_pairing_code_registered_once(tmp_path):
    handlers = [route for route in _routes(tmp_path) if route.path == "/sync/pairing-code"]
    assert [sorted(route.methods) for route in handlers] == [["POST"]]
    # 生效的那一份必须是合并后的：带 ttl_seconds 回传与 relay 异常兜底
    source = handlers[0].endpoint
    assert source.__name__ == "create_sync_pairing_code"
    assert "ttl_seconds" in source.__doc__, "重复合并的回传字段丢失"


def test_route_surface_is_measurable(tmp_path):
    """210 条路由是 H04 拆分前的基线口径，掉到异常水平说明有端点被误删。"""
    assert len(_routes(tmp_path)) > 200
