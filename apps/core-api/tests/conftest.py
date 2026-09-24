"""core-api 测试共享 fixture。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.config import CoreSettings

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_youzi_fixture(name: str) -> str:
    """读取席位证据夹具的**原始字节**并解码为 str（不解析、不改写）。

    中金所 XML 声明 UTF-8；东财 JSON 为 UTF-8。夹具一律按 UTF-8 严格解码，
    解不开就是夹具被污染，测试应当直接失败而不是静默替换字符。
    """
    return (FIXTURES / "youzi" / name).read_bytes().decode("utf-8")


def load_youzi_json(name: str) -> dict[str, object]:
    """读取东财夹具为 dict（含 code=9501/9201 这类失败响应，原样返回不抛错）。"""
    payload = json.loads(load_youzi_fixture(name))
    if not isinstance(payload, dict):
        raise TypeError(f"夹具 {name} 顶层不是对象")
    return payload


@pytest.fixture()
def youzi_fixture_text():
    """席位证据夹具读取器：`youzi_fixture_text("cffex_index_20260916.xml")`。"""
    return load_youzi_fixture


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    from investment_steward_core.api import create_app

    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}
