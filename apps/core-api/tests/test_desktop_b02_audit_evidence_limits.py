"""B02（桌面端升级路线图 2026-09-18）：/audit、/evidence 上限与分页的端点级测试。

覆盖：默认上限（不再整表返回）、offset 翻页、q 过滤（action/resource_type/resource_id）、
order 三种排序、with_total 信封、evidence_type 过滤；/export/all 仍走全量（不受上限影响）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain.models import AuditEvent, Evidence, EvidenceType

TOKEN = "b02-limits-token"


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _seed_audit(database, tenant_id, count: int = 12) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(count):
        database.append_audit(
            AuditEvent(
                event_id=uuid4(),
                tenant_id=tenant_id,
                action=f"plugin.{'install' if index % 2 == 0 else 'update'}",
                resource_type="plugin_installation",
                resource_id=f"official.plugin-{index:02d}",
                payload={"seq": index},
                created_at=start + timedelta(hours=index),
            )
        )


def _seed_evidence(database, tenant_id, count: int = 7) -> None:
    for index in range(count):
        database.insert_evidence(
            Evidence(
                evidence_id=uuid4(),
                tenant_id=tenant_id,
                subject_refs=[f"00{index}"],
                evidence_type=EvidenceType.QUOTE if index % 2 == 0 else EvidenceType.MACRO,
                source_name=f"源 {index}",
                summary=f"证据摘要 {index}",
                content_hash=f"hash-{index:016d}",
                collected_at=datetime(2026, 9, 10, tzinfo=UTC) + timedelta(hours=index),
            )
        )


def test_audit_default_cap_offset_and_total(tmp_path):
    http, headers = _client(tmp_path)
    core = http.app.state.core
    _seed_audit(core.database, core.local_user_id, count=12)

    plain = http.get("/audit", headers=headers)
    items = plain.json()
    assert isinstance(items, list), "裸列表形态保持（boot 兼容）"
    assert len(items) == 12, "12 条 < 默认上限 100，不截断"

    page = http.get("/audit", params={"limit": 5, "offset": 0, "with_total": True}, headers=headers).json()
    assert len(page["items"]) == 5 and page["total"] == 12 and page["offset"] == 0

    second = http.get("/audit", params={"limit": 5, "offset": 5, "with_total": True}, headers=headers).json()
    assert len(second["items"]) == 5
    # created_at 倒序：第一页是 seq 11..7，第二页接 seq 6..2。
    assert {row["payload"]["seq"] for row in second["items"]} == {6, 5, 4, 3, 2}, "created_at 倒序翻页连续"

    hard = http.get("/audit", params={"limit": 10_000}, headers=headers).json()
    assert len(hard) == 12, "12 条全量内不截断；单页上限 500 只在更大规模时生效"


def test_audit_q_filter_and_orders(tmp_path):
    http, headers = _client(tmp_path)
    core = http.app.state.core
    _seed_audit(core.database, core.local_user_id, count=12)

    by_action = http.get("/audit", params={"q": "update", "with_total": True}, headers=headers).json()
    # q 与旧前端过滤同口径扫 action/resource_type/resource_id：'update' 只命中 6 条 plugin.update，
    # 'install' 会连 resource_type=plugin_installation 一起命中（12 条），所以选 'update' 做区分。
    assert by_action["total"] == 6 and len(by_action["items"]) == 6

    by_resource = http.get("/audit", params={"q": "plugin-07", "with_total": True}, headers=headers).json()
    assert by_resource["total"] == 1 and by_resource["items"][0]["resource_id"] == "official.plugin-07"

    asc = http.get("/audit", params={"order": "time-asc", "limit": 3}, headers=headers).json()
    assert [row["payload"]["seq"] for row in asc] == [0, 1, 2]

    by_action_sort = http.get("/audit", params={"order": "action", "limit": 4}, headers=headers).json()
    actions = [row["action"] for row in by_action_sort]
    assert actions == sorted(actions), "action 排序为字典序"
    assert actions[0] == "plugin.install"


def test_evidence_cap_offset_type_filter(tmp_path):
    http, headers = _client(tmp_path)
    core = http.app.state.core
    _seed_evidence(core.database, core.local_user_id, count=7)

    plain = http.get("/evidence", headers=headers)
    assert isinstance(plain.json(), list) and len(plain.json()) == 7

    page = http.get("/evidence", params={"limit": 3, "offset": 0, "with_total": True}, headers=headers).json()
    assert len(page["items"]) == 3 and page["total"] == 7

    second = http.get("/evidence", params={"limit": 3, "offset": 3, "with_total": True}, headers=headers).json()
    assert len(second["items"]) == 3 and len({item["evidence_id"] for item in page["items"] + second["items"]}) == 6

    quotes = http.get("/evidence", params={"evidence_type": "quote", "with_total": True}, headers=headers).json()
    assert quotes["total"] == 4 and {item["evidence_type"] for item in quotes["items"]} == {"quote"}


def test_export_all_still_full_despite_caps(tmp_path):
    """B02 只限列表端点：/export/all 走 db.list_* 全量口径，不受新上限影响。"""
    http, headers = _client(tmp_path)
    core = http.app.state.core
    _seed_audit(core.database, core.local_user_id, count=12)
    _seed_evidence(core.database, core.local_user_id, count=7)

    exported = http.get("/export/all", headers=headers).json()
    assert len(exported["evidence"]) == 7
