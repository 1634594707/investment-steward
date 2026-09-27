"""S4（用户视角路线图 2026-09-26）：未预期异常必须返回可读文案 + 诊断码。

改造前 `api/app.py` 没有任何 exception_handler：159 个 try / 54 个 except Exception
都是逐路由手写，漏网异常一路逃到 Starlette 的 ServerErrorMiddleware，返回**纯文本**
`Internal Server Error`——无 detail、无 request id，traceback 只进 Core 进程 stderr，
打包版用户永远看不到。「令牌不匹配」「数据损坏」「磁盘错误」在界面上长得一模一样。

本文件锁定：

1. 返回 **JSON** 而非纯文本，且带 `incident` 诊断码与请求路径；
2. 内部细节（异常消息/类型）**不外泄**给调用方，只进日志；
3. 异常确实被记录到日志（支持可反查）；
4. 正常路径与 4xx 行为不受影响。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

SECRET = "内部细节不应外泄的异常消息"


@pytest.fixture()
def boom_client(tmp_path):
    """带一个必定抛异常的路由的 TestClient。

    必须 `raise_server_exceptions=False`——TestClient 默认会把服务端异常重新抛到
    调用方，那样就测不到「真实客户端收到什么」了。
    """
    app = create_app(CoreSettings(session_token="t", data_dir=tmp_path))

    @app.get("/_test/boom")
    def _boom() -> dict[str, str]:
        raise ValueError(SECRET)

    @app.get("/_test/ok")
    def _ok() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app, raise_server_exceptions=False), {"X-Core-Session-Token": "t"}


def test_unhandled_exception_returns_json_with_incident(boom_client):
    test_client, headers = boom_client
    response = test_client.get("/_test/boom", headers=headers)
    assert response.status_code == 500
    # 关键：必须是 JSON 而非纯文本 "Internal Server Error"
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body.get("incident")
    assert body["path"] == "/_test/boom"
    assert "诊断码" in body["detail"]


def test_internal_details_do_not_leak(boom_client):
    test_client, headers = boom_client
    text = test_client.get("/_test/boom", headers=headers).text
    assert SECRET not in text, "异常消息不得出现在返回给客户端的响应里"
    assert "ValueError" not in text, "异常类型不得出现在返回给客户端的响应里"
    assert "Traceback" not in text


def test_exception_is_logged_with_the_same_incident(boom_client, caplog):
    """诊断码必须同时进日志，否则用户报了码也查不到。"""
    test_client, headers = boom_client
    with caplog.at_level(logging.ERROR):
        body = test_client.get("/_test/boom", headers=headers).json()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert body["incident"] in logged, "诊断码应出现在日志中，供支持反查"
    assert any(record.exc_info for record in caplog.records), "应记录完整 traceback"


def test_normal_and_client_error_paths_unaffected(boom_client):
    test_client, headers = boom_client
    assert test_client.get("/_test/ok", headers=headers).json() == {"ok": "yes"}
    # 401 仍是 401，不被兜底处理器吞成 500（业务路由的守卫走 HTTPException，不经兜底）
    assert test_client.get("/holdings", headers={"X-Core-Session-Token": "bad"}).status_code == 401
    assert test_client.get("/holdings").status_code == 401
    # 404 仍是 404
    assert test_client.get("/_test/nope", headers=headers).status_code == 404
