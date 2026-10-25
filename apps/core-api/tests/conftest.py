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


# —— G1（研报产出与交付链路路线图 2026-09-27）：端点级样本的「已达标」摘要与结论 ——
#
# 闸门新增两个**会额外调用模型**的修复出口（详见 report_quality.repair_strategy_of）：
#   1. 摘要落在目标区间 [150, 165] 之外 → 一次「只重写摘要」调用（D04 上界 + G1 下界）；
#   2. 缺一句话结论（conclusion.statement）→ 一次定向补写调用。
#
# 打桩模型通常只准备「初稿 + 反方检查」两次响应，多出来的调用会让 FIFO 桩返回 `{}`，
# 或让 `len(payloads)` / `call_count` 断言失真（典型症状：「4 != 2」）。
# 所以**凡自带正文样本、要断言调用次数或调用内容**的用例，摘要与结论都必须已达标；
# 专测「过短/缺失会被修复」的用例则**不要**用这两个常量（那正是它要制造的状态）。
SAMPLE_SUMMARY_IN_BAND = (
    "方向性结论偏多：技术面缓涨趋势完好、量价配合健康，中期趋势结构未被破坏。"
    "最强的基本面依据是营收与净利双双同比增长、经营现金流为正，利润质量较为健康，未见明显的财务瑕疵。"
    "最重要的技术面依据是 MA20 支撑有效、量能温和放大、回踩不破位。"
    "最大的风险与反证是估值分位虽处低位但向上赔率有限，且回购落地节奏存在不确定性。"
)
SAMPLE_CONCLUSION = {
    "statement": "趋势偏多但估值赔率有限，回购落地节奏是最关键的跟踪变量。",
    "direction": "偏多",
    "core_conflict": "趋势向好与赔率有限",
    "confidence": "medium",
}
