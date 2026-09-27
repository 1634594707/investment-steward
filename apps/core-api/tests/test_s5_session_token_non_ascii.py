"""S5（用户视角路线图 2026-09-26）：非 ASCII 会话令牌必须 401，不得逃逸成 500。

背景：`api/deps.py::require_session` 用 `secrets.compare_digest` 比对令牌。该函数遇到
含非 ASCII 字符的 `str` 会抛 `TypeError`（"comparing strings with non-ASCII characters
is not supported"），而 Starlette 以 latin-1 解码头值——任一非 ASCII 字节都会变成非
ASCII `str`。改造前该 TypeError 没有任何捕获，也没有全局异常处理器（S4），于是以裸
500「Internal Server Error」返回，把「令牌不匹配」伪装成「服务器故障」，并在 Core
控制台刷 traceback。

本文件锁定三件事：

1. 非 ASCII 头值返回 **401**（与「无头」「错误令牌」同一形状）；
2. 正确令牌仍返回 **200**，修复没有误伤正常路径；
3. 失败路径不产生 ERROR 级日志。

注意：头值必须以**原始字节**发送。httpx 对 str 头值强制 ASCII
（`httpx._models._normalize_header_value`），非 ASCII 的 str 在客户端就被拒，根本到不了
服务端——那不是本用例要复现的场景。真实世界里 curl、代理改写、抓包重放都能发出非
ASCII 字节，那才是服务端必须扛住的情况。服务端 Starlette 以 latin-1 解码，
故这些字节在服务端呈现为非 ASCII `str`。
"""

from __future__ import annotations

import logging

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

NON_ASCII_TOKEN_BYTES: list[bytes] = [
    "你好".encode(),   # UTF-8 中文
    "tokén".encode(),  # 带重音字符
    b"\xe9",             # 单个高位字节 → latin-1 下是 U+00E9
    b"\xff",             # 单个高位字节 → latin-1 下是 U+00FF
    b"ok\xe9",            # ASCII 前缀 + 高位字节
]


def test_non_ascii_token_returns_401_not_500(client):
    """非 ASCII 令牌 → 401，且 detail 与其它未授权路径完全一致。"""
    test_client, _headers = client
    for raw in NON_ASCII_TOKEN_BYTES:
        response = test_client.get("/holdings", headers={"X-Core-Session-Token": raw})
        assert response.status_code == 401, f"{raw!r} 应为 401，实得 {response.status_code}"
        assert response.json()["detail"] == "invalid local session"


def test_missing_and_wrong_token_still_401(client):
    """既有语义不回退：无头与 ASCII 错误令牌仍是 401，正确令牌仍是 200。"""
    test_client, headers = client
    assert test_client.get("/holdings").status_code == 401
    assert test_client.get("/holdings", headers={"X-Core-Session-Token": "wrong"}).status_code == 401
    assert test_client.get("/holdings", headers=headers).status_code == 200


def test_non_ascii_token_logs_no_error(client, caplog):
    """非 ASCII 令牌属于「客户端发了坏令牌」，不该在服务端留下 ERROR 记录。"""
    test_client, _headers = client
    with caplog.at_level(logging.ERROR):
        for raw in NON_ASCII_TOKEN_BYTES:
            test_client.get("/holdings", headers={"X-Core-Session-Token": raw})
    assert not caplog.records, f"非 ASCII 令牌不应产生 ERROR 日志，实际：{caplog.records}"
