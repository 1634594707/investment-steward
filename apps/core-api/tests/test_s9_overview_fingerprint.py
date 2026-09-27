"""S9（用户视角路线图 2026-09-26）：/overview 不应为算指纹而全量装载证据。

改造前 `overview()` 调 `list_evidence()`：把**整条 payload** 读出来、逐条 `json.loads`、
再逐条 Pydantic `model_validate`，只为了拿 `evidence_id` 与 `content_hash` 两个标量。
而这两列本就是 `evidence` 表的真实列（`database.py` 的建表语句），根本不必读 payload。
结果是首屏装载的耗时与内存随证据总数线性放大，且 `/overview` 每次手动刷新都重付一次。

本文件锁定：
1. **指纹值逐字节不变**——这是硬约束：指纹用于更新检测，算法一改，历史对比即失效；
2. `/overview` 不再调用 `list_evidence()`（用替身把它变成会炸的哨兵）。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from investment_steward_core import channels
from investment_steward_core.api.app import _latest_datetime


def _evidence_row(evidence_id: str, content_hash: str):
    """构造一个带完整 payload 的 Evidence 替身（验证旧路径确实需要 model_validate）。"""
    return SimpleNamespace(
        evidence_id=evidence_id,
        content_hash=content_hash,
        source_url="https://example.invalid/x",
        title="t",
        # 故意塞一个非标字段：旧路径 model_validate 会因多余/非法字段而抛，
        # 新路径根本看不到 payload，因此不受影响。
        payload_blob="x" * 4096,
    )


def test_fingerprint_identical_for_rows_and_minimal_rows():
    """核心约束：EvidenceFingerprintRow 算出的指纹与完整 Evidence 对象**完全一致**。"""
    ids = [str(uuid4()) for _ in range(50)]
    full = [_evidence_row(i, f"h{i}") for i in ids]
    minimal = [channels.EvidenceFingerprintRow(i, f"h{i}") for i in ids]
    assert channels._content_fingerprint(full) == channels._content_fingerprint(minimal)


def test_fingerprint_is_order_independent_and_change_sensitive():
    """顺序无关（两边都排序）；任一 id 或 hash 变化即变。"""
    ids = [str(uuid4()) for _ in range(10)]
    hashes = [f"h{i}" for i in range(10)]
    forward = [channels.EvidenceFingerprintRow(i, h) for i, h in zip(ids, hashes, strict=True)]
    backward = list(reversed(forward))
    assert channels._content_fingerprint(forward) == channels._content_fingerprint(backward)

    changed_hash = list(forward)
    changed_hash[3] = channels.EvidenceFingerprintRow(ids[3], "h3-changed")
    assert channels._content_fingerprint(forward) != channels._content_fingerprint(changed_hash)

    added = forward + [channels.EvidenceFingerprintRow(str(uuid4()), "h-new")]
    assert channels._content_fingerprint(forward) != channels._content_fingerprint(added)


def test_overview_does_not_load_full_evidence(client, monkeypatch):
    """`/overview` 不得再调 list_evidence()（那会全量读 payload + 逐条模型校验）。"""
    from investment_steward_core.storage import database as db_module

    test_client, headers = client

    def _boom(*_a, **_k):
        raise AssertionError("/overview 仍调用了 list_evidence()：会把整条 payload 读出来并逐条模型校验")

    monkeypatch.setattr(db_module.Database, "list_evidence", _boom)

    response = test_client.get("/overview", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    # CONTENT 通道指纹仍应存在（不是把它删掉来「解决」性能问题）
    assert body["channels"]["content"]["current"], "CONTENT 通道指纹不应为空"


def test_fingerprint_rows_match_full_evidence_on_real_db(client):
    """在真实库上比对：轻量行路径与全量路径给出的指纹一致。"""
    from investment_steward_core.domain import Evidence, EvidenceType

    test_client, _headers = client
    core = test_client.app.state.core
    tenant = core.local_user_id
    ids = []
    for i in range(5):
        # 字段照抄 domain/models.py 的 Evidence 实际定义，不猜
        evidence = Evidence(
            evidence_id=uuid4(),
            tenant_id=tenant,
            subject_refs=["600001"],
            evidence_type=EvidenceType.QUOTE,
            source_name="测试源",
            summary=f"测试证据 {i}",
            content_hash=f"hash-{i}-{'x' * 12}",
        )
        core.database.insert_evidence(evidence)
        ids.append((str(evidence.evidence_id), evidence.content_hash))

    full = core.database.list_evidence(tenant)
    minimal = [
        channels.EvidenceFingerprintRow(e, h)
        for e, h in core.database.list_evidence_fingerprint_rows(tenant)
    ]
    assert len(minimal) == len(full) == 5
    assert channels._content_fingerprint(full) == channels._content_fingerprint(minimal)
    assert {e for e, _ in minimal} == {e for e, _ in ids}


def test_latest_evidence_collected_at_matches_full_scan(client):
    """`latest_evidence_collected_at` 与全量扫描取 max(collected_at) 口径一致。"""
    test_client, _headers = client
    core = test_client.app.state.core
    tenant = core.local_user_id
    from investment_steward_core.domain import Evidence, EvidenceType

    for i in range(4):
        core.database.insert_evidence(
            Evidence(
                evidence_id=uuid4(),
                tenant_id=tenant,
                subject_refs=["600001"],
                evidence_type=EvidenceType.QUOTE,
                source_name="测试源",
                summary=f"证据 {i}",
                content_hash=f"hash-{i}-{'y' * 12}",
            )
        )

    expected = _latest_datetime(core.database.list_evidence(tenant), "collected_at", "updated_at", "created_at")
    actual = core.database.latest_evidence_collected_at(tenant)
    assert actual is not None
    assert actual.isoformat() == expected


def test_overview_does_not_scan_evidence_twice(client, monkeypatch):
    """/overview 两条证据路径（指纹 + data_as_of）都不得再全量装载。"""
    from investment_steward_core.domain import Evidence, EvidenceType
    from investment_steward_core.storage import database as db_module

    test_client, headers = client
    core = test_client.app.state.core
    # 先落一条证据：空账本时 data_as_of.evidence 本就应为 None，那样测不出东西
    core.database.insert_evidence(
        Evidence(
            evidence_id=uuid4(),
            tenant_id=core.local_user_id,
            subject_refs=["600001"],
            evidence_type=EvidenceType.QUOTE,
            source_name="测试源",
            summary="证据",
            content_hash="hash-overview-probe",
        )
    )
    monkeypatch.setattr(
        db_module.Database, "list_evidence",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("/overview 又全量装载证据了")),
    )
    response = test_client.get("/overview", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data_as_of"]["evidence"] is not None, "evidence 的「数据截至」不能因为优化而丢失"
