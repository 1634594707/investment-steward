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


# T10（用户视角路线图 2026-09-26）：个股深研改后台任务后，测试侧的统一入口。
#
# 端点语义变了：POST /evidence/stock-research-report 现在**只建任务并返回 job_id**，
# 结果经 GET /evidence/stock-research-report/{job_id} 取。以前 20 多个测试文件直接把
# POST 的响应体当报告断言，现在改用本 helper：建任务 → 轮询到终态 → 返回与旧 POST
# 同形状的报告体。这样测试断言基本不用改，轮询细节集中在这一个地方。
JOB_POLL_INTERVAL_S = 0.02
JOB_POLL_TIMEOUT_S = 60.0


def start_stock_report_job(test_client, headers, **body):
    """建研报任务；返回 POST 的建任务响应（应含 job_id）。"""
    response = test_client.post("/evidence/stock-research-report", json=body, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "job_id" in payload, f"建任务响应缺 job_id：{payload}"
    return payload


def await_stock_report_job(test_client, headers, job_id):
    """轮询到终态，返回终态响应体（与改造前 POST 的报告体同形状 + job_id/state/stage）。"""
    import time

    deadline = time.monotonic() + JOB_POLL_TIMEOUT_S
    while True:
        response = test_client.get(f"/evidence/stock-research-report/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload.get("state") in ("done", "error", "cancelled"):
            return payload
        if time.monotonic() > deadline:
            raise AssertionError(f"研报任务在 {JOB_POLL_TIMEOUT_S}s 内未到终态：{payload}")
        time.sleep(JOB_POLL_INTERVAL_S)


def run_stock_report(test_client, headers, **body):
    """建任务 + 轮询终态，一步到位。测试里绝大多数场景用这个。"""
    started = start_stock_report_job(test_client, headers, **body)
    return await_stock_report_job(test_client, headers, started["job_id"])
