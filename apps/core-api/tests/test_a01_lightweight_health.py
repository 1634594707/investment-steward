"""A01（架构改进路线图 2026-09-25）：就绪探测与业务概览分离的验收。

改造前 `/health` 会全量读取证据（`list_evidence` → 逐条 `model_validate`）、再对全量证据
排序算内容指纹，探活成本随研究资料积累线性增长；而它的三个调用方里只有首屏装载真的
需要业务字段。本文件断言三件事：

1. **探活不遍历证据**——用调用计数探针证明（不靠计时：计时断言在 CI 上不稳定，
   路线图也要求「记录实际延迟后再制定发布阈值」）；
2. **业务语义未丢**——CONTENT 通道内容指纹仍随证据集合变化，`/overview` 保留
   插件计数与插槽占用；
3. **故障时如实报未就绪**——存储打不开时返回 503 / `not_ready`，不谎报 ready，
   也不把异常抛成 500。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core.domain import Evidence, EvidenceType
from investment_steward_core.storage import database as db_module


def _evidence(tenant_id: UUID, marker: str) -> Evidence:
    """构造一条最小可用证据；marker 参与 content_hash，便于制造「内容变化」。"""
    return Evidence(
        evidence_id=uuid4(),
        tenant_id=tenant_id,
        subject_refs=["600519"],
        evidence_type=EvidenceType.QUOTE,
        source_name="A01 验收夹具",
        summary=f"探活成本验收证据 {marker}",
        content_hash=(marker * 64)[:32],
    )


def _local_user_id(test_client, headers) -> UUID:
    return UUID(test_client.get("/session", headers=headers).json()["user_id"])


def _spy_on_full_evidence_reads(monkeypatch) -> dict[str, int]:
    """给 `Database.list_evidence` 装计数探针（全量读取的唯一入口）。"""
    calls = {"count": 0}
    original = db_module.Database.list_evidence

    def spy(self, tenant_id):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return original(self, tenant_id)

    monkeypatch.setattr(db_module.Database, "list_evidence", spy)
    return calls


def test_health_probe_never_reads_evidence(client, monkeypatch):
    """探活路径对全量证据读取的调用次数必须恒为 0。"""
    test_client, headers = client
    calls = _spy_on_full_evidence_reads(monkeypatch)

    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert calls["count"] == 0, "就绪探测不得全量读取证据（A01 回归）"

    # S9（用户视角路线图 2026-09-26）改变了这一条断言，理由必须写清楚：
    # 原断言是 `calls["count"] >= 1`，注释写「CONTENT 通道指纹的语义就是全量证据聚合」。
    # 那句话把**语义**与**实现**混成了一件事：
    #   - 语义（指纹随全部证据变化）——由 test_overview_keeps_channel_fingerprint_semantics
    #     单独锁定，至今仍通过；
    #   - 实现（为此把每条 payload 读出、JSON 解析、Pydantic 校验）——S9 已去掉。
    # 指纹要的只是 (evidence_id, content_hash) 两个标量，而它们本就是 `evidence` 表的
    # 真实列。改为断言「概览仍算出指纹、但不再全量读取」，比原断言更强：
    # 既锁住语义没丢，又锁住成本不会随证据总量重新涨回来。
    overview = test_client.get("/overview", headers=headers)
    assert overview.status_code == 200
    assert overview.json()["channels"]["content"]["current"], "业务概览丢失了内容指纹计算"
    assert calls["count"] == 0, "概览不该再全量读取证据——只需两个标量列（S9 回归）"


def test_health_probe_cost_does_not_grow_with_evidence_volume(client, monkeypatch):
    """在独立测试库里把证据规模拉大，探活仍然不触碰全量读取。

    这是 A01 的核心回归：改造前探活成本 ∝ 证据条数，这里用「调用次数与规模无关」
    来锁定该性质（计时断言留给发布阈值，不放进 CI）。
    """
    test_client, headers = client
    database = test_client.app.state.core.database
    user_id = _local_user_id(test_client, headers)
    for index in range(300):
        database.insert_evidence(_evidence(user_id, f"scale-{index:04d}"))

    calls = _spy_on_full_evidence_reads(monkeypatch)
    for _ in range(5):
        assert test_client.get("/health").status_code == 200
    assert calls["count"] == 0


def test_overview_requires_session(client):
    """业务概览暴露数据域与插件清单，必须与其它业务端点同级鉴权。"""
    test_client, _ = client
    assert test_client.get("/overview").status_code == 401


def test_overview_keeps_channel_fingerprint_semantics(client):
    """CONTENT 通道内容指纹「条目或内容任一变化即变」的原语义必须保留。"""
    test_client, headers = client
    database = test_client.app.state.core.database
    user_id = _local_user_id(test_client, headers)

    before = test_client.get("/overview", headers=headers).json()
    database.insert_evidence(_evidence(user_id, "fingerprint-a"))
    middle = test_client.get("/overview", headers=headers).json()
    database.insert_evidence(_evidence(user_id, "fingerprint-b"))
    after = test_client.get("/overview", headers=headers).json()

    fingerprints = [
        payload["channels"]["content"]["current"] for payload in (before, middle, after)
    ]
    assert len(set(fingerprints)) == 3, f"内容指纹未随证据变化：{fingerprints}"
    # 概览的其余业务字段仍在位（迁移不得丢字段）。
    assert set(after["plugins"]) == {"enabled", "disabled", "available"}
    assert set(after["slots"]) == {"used", "cap"}
    assert "data_as_of" in after


def test_health_reports_not_ready_when_storage_fails(client, monkeypatch):
    """存储不可用时探活必须如实返回未就绪（503），而不是 500 或谎报 ready。"""
    test_client, _ = client

    def boom(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(db_module.Database, "ping", boom)
    response = test_client.get("/health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert "存储不可用" in payload["detail"]
