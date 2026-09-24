"""v21 数据可信度修复回归护栏（P0-01/02/03 + M1-01）。

对应 docs/project-update-plan-2026-09-07.zh-CN.md 工作包：
- P0-01: /quant/parameter-sets/{id}/lineage 重复装饰器对拍（app.py 981 行真实路由 vs 旧 1177 行死装饰器）。
- P0-02: /evidence/macro/research-evidence 落库（v21 迁移 macro_research_evidence 表），重启不丢。
- P0-03: POST /evidence/macro/analysis 空桩删除。
- M1-01: builtin 回退清单不再声明 official.notification-rules（能力实为 core 原生通知引擎，
  cards.py 的 review.plan 卡仅以该 id 作来源署名，不要求目录中存在此插件）。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence, EvidenceRelation, EvidenceStatus, EvidenceType


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    token = "v21-token"
    return TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path))), {
        "X-Core-Session-Token": token
    }


def _research_payload() -> dict[str, object]:
    return {
        "event_id": "ukraine",
        "claim": "黑海航运受扰抬升全球粮食成本",
        "mechanism_steps": ["航运受扰", "粮食价格上行"],
        "instrument_or_market": "CN:农业ETF",
        "direction": "bullish",
        "time_window": "2026Q3-2026Q4",
        "geographies": ["CN"],
        "source_refs": ["https://example.com/grain-report"],
    }


def _seed_evidence(client: TestClient, headers: dict[str, str]) -> None:
    """预置一条证据，使「证据列表」与「谱系（未知 id → 空链）」可区分。"""
    tenant_id = client.get("/session", headers=headers).json()["user_id"]
    evidence = Evidence(
        evidence_id=uuid4(),
        tenant_id=tenant_id,
        subject_refs=["instrument:CN:ETF:510300"],
        evidence_type=EvidenceType.ANALYSIS,
        source_name="v21-sentinel",
        summary="谱系路由对拍哨兵证据",
        content_hash=uuid4().hex,
        relation=EvidenceRelation.SUPPORTING,
        status=EvidenceStatus.ACTIVE,
        valid_until=datetime.now(UTC) + timedelta(days=1),
    )
    resp = client.post("/evidence", headers=headers, json=evidence.model_dump(mode="json"))
    assert resp.status_code == 201, resp.text


def test_lineage_route_serves_lineage_not_evidence_list(tmp_path):
    """P0-01 对拍：预置证据后访问谱系路径，未知 artifact_id 必须返回空谱系，
    绝不允许返回 /evidence 的证据列表（旧 1177 行重复装饰器的回归哨兵）。"""
    client, headers = _client(tmp_path)
    _seed_evidence(client, headers)
    assert client.get("/evidence", headers=headers).json(), "前置：证据账本应有记录"
    lineage = client.get(
        "/quant/parameter-sets/v21-nonexistent-artifact/lineage", headers=headers
    )
    assert lineage.status_code == 200, lineage.text
    assert lineage.json() == [], f"谱系路径被证据列表劫持：{lineage.json()}"


def test_macro_research_evidence_survives_reopen(tmp_path):
    """P0-02：登记的宏观研究证据落库，重启（对同一 data_dir 重建 app）后不丢；
    event 过滤保持 v20 语义。"""
    client, headers = _client(tmp_path)
    created = client.post(
        "/evidence/macro/research-evidence", headers=headers, json=_research_payload()
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["id"]

    # 模拟 Core 重启：v20 的内存字典是模块级状态，必须显式清空才等价于新进程；
    # v21 落库后该属性不存在，此操作为无副作用 no-op。
    import investment_steward_core.api.app as app_module

    in_memory = getattr(app_module, "_MACRO_RESEARCH_EVIDENCE", None)
    if in_memory is not None:
        in_memory.clear()

    client2, headers2 = _client(tmp_path)
    listed = client2.get("/evidence/macro/research-evidence", headers=headers2).json()
    assert [x["id"] for x in listed] == [item["id"]], "重启后研究证据丢失"
    assert listed[0]["claim"] == "黑海航运受扰抬升全球粮食成本"

    hit = client2.get(
        "/evidence/macro/research-evidence",
        headers=headers2,
        params={"event": "ukraine"},
    ).json()
    assert [x["id"] for x in hit] == [item["id"]]
    miss = client2.get(
        "/evidence/macro/research-evidence",
        headers=headers2,
        params={"event": "iran"},
    ).json()
    assert miss == []


def test_macro_research_evidence_requires_refs_and_window(tmp_path):
    """P0-02：source_refs / time_window 必填校验与 v20 行为一致（422）。"""
    client, headers = _client(tmp_path)
    no_refs = _research_payload()
    no_refs["source_refs"] = []
    assert client.post("/evidence/macro/research-evidence", headers=headers, json=no_refs).status_code == 422

    no_window = _research_payload()
    no_window["time_window"] = ""
    assert client.post("/evidence/macro/research-evidence", headers=headers, json=no_window).status_code == 422


def test_macro_analysis_stub_is_gone(tmp_path):
    """P0-03：POST /evidence/macro/analysis 空桩已删除（v20 恒返 pending_manual_trigger）。

    删除后该路径仍会命中 GET /evidence/macro/{region} 的路径模板（method 不符 → 405），
    故以「非 200 且响应体不含 pending_manual_trigger」为空桩已删的判据。
    """
    client, headers = _client(tmp_path)
    resp = client.post("/evidence/macro/analysis", headers=headers, json={"any": "input"})
    assert resp.status_code in (404, 405), f"空桩仍存在：{resp.status_code} {resp.text}"
    assert "pending_manual_trigger" not in resp.text


def test_builtin_fallback_no_longer_declares_notification_rules():
    """M1-01：builtin 回退清单不再声明 official.notification-rules。"""
    from investment_steward_core.api.app import _builtin_plugin_manifests

    ids = [m.plugin_id for m in _builtin_plugin_manifests()]
    assert "official.notification-rules" not in ids, f"回退清单仍声明已废弃插件：{ids}"
