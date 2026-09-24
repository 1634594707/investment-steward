"""v23 证据查重测试：拉取幂等（换号重发/重复拉取不再重复入库）+ 历史重复清理端点。

红线：
- 公告哈希归一（symbol+标题+日期，art_code 不参与）：同公告换稿号只入账一条；
- 新闻哈希归一（symbol+标题，稿件号/摘要长度不参与）；
- POST /evidence/dedup 按（类型+标题+发布日期）分组清历史重复，保留最早入账一条。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import _file_client


def _announcement_items():
    """同标题同日期、不同 art_code（源站换号重发场景）。"""
    return [
        SimpleNamespace(notice_date_raw="2026-09-01 08:00", title="关于重大诉讼的进展公告", ann_type="诉讼", url="http://x/1", art_code="A1"),
        SimpleNamespace(notice_date_raw="2026-09-01 08:00", title="关于重大诉讼的进展公告", ann_type="诉讼", url="http://x/2", art_code="A2"),
    ]


def test_pull_announcements_dedups_reissued_codes(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    import investment_steward_core.api.app as app_module

    monkeypatch.setattr(app_module, "fetch_cn_announcements", lambda symbol, limit=20: _announcement_items())

    first = test_client.get("/evidence/announcements/600001", headers=headers).json()
    assert first["available"] is True, first
    # 同标题同日期的两条（不同 art_code）只入账一条
    assert len(first["entries"]) == 1

    second = test_client.get("/evidence/announcements/600001", headers=headers).json()
    assert second["available"] is True
    # 重复拉取零新增（拉取幂等）
    assert len(second["entries"]) == 0


def test_pull_news_dedups_same_title(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    import investment_steward_core.api.app as app_module

    items = [
        SimpleNamespace(published_raw="2026-09-01", title="甲股中标大单", content="内容正文A" * 30, code="N1", url="http://n/1", media="证券时报"),
        SimpleNamespace(published_raw="2026-09-01", title="甲股中标大单", content="另一段摘要" * 20, code="N2", url="http://n/2", media="上证报"),
    ]
    monkeypatch.setattr(app_module, "fetch_cn_news", lambda symbol, limit=20: items)

    body = test_client.get("/evidence/news/600001", headers=headers).json()
    assert body["available"] is True
    # 同标题新闻（不同稿件号/不同摘要）只入账一条
    assert len(body["entries"]) == 1


def _seed_evidence(db, tenant, *, hash_value: str, title: str, collected_at: str):
    from investment_steward_core.domain import Evidence

    db.insert_evidence(
        Evidence(
            evidence_id=uuid4(),
            tenant_id=tenant,
            subject_refs=["instrument:CN:600001"],
            evidence_type="announcement",
            source_name="中国市场公告（东方财富）",
            license_status="local-personal-use",
            observed_at=collected_at,
            collected_at=collected_at,
            published_at=collected_at,
            schema_version="1.0",
            summary=f"{title}・诉讼",
            content_hash=hash_value,
            relation="unknown",
            freshness="as_of_fetch",
            quality_flags=[],
            limitations=[],
            status="active",
        )
    )


def test_evidence_dedup_removes_historical_duplicates(client, tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from pathlib import Path

    from investment_steward_core.storage.database import Database

    test_client, headers = _file_client(tmp_path, monkeypatch)
    tenant = uuid5(NAMESPACE_URL, str(Path(tmp_path).resolve()))
    db2 = Database(Path(tmp_path) / "steward.sqlite3")
    now = datetime.now(UTC).isoformat()
    # 历史重复：同类型+同标题+同日期，哈希不同（旧哈希含稿号时代入账的两条）
    _seed_evidence(db2, tenant, hash_value="old-hash-1-000000000000000000000000", title="关于重大诉讼的进展公告", collected_at=now)
    _seed_evidence(db2, tenant, hash_value="old-hash-2-000000000000000000000000", title="关于重大诉讼的进展公告", collected_at=now)
    _seed_evidence(db2, tenant, hash_value="unique-hash-00000000000000000000", title="另一条不相关公告", collected_at=now)

    body = test_client.post("/evidence/dedup", headers=headers).json()
    assert body["ok"] is True, body
    assert body["removed"] == 1  # 只删同组多余的一条，保留最早入账
    assert body["remaining"] == 2

    again = test_client.post("/evidence/dedup", headers=headers).json()
    assert again["removed"] == 0 and again["remaining"] == 2  # 幂等：再跑无可清
