"""Verify document identities through report jobs, storage and hydrated detail; no network or disk DB."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from conftest import run_stock_report
from test_t03_scan_history_http import scan_http  # noqa: F401
from test_v31_repair_and_comparison import _good_payload, _mock_sources
from investment_steward_core.api import app as app_module
from investment_steward_core.domain import ModelProfileStatus
from investment_steward_core.model_client import ModelReply
from investment_steward_core.news_evidence import AnnouncementBody
from investment_steward_core import lhb_feed


@pytest.mark.parametrize("missing_news_url", [False, True])
@pytest.mark.parametrize("collab", [False, True])
def test_report_job_and_detail_preserve_each_document_identity(scan_http, monkeypatch, missing_news_url, collab):
    http, headers, database = scan_http
    _mock_sources(monkeypatch)
    profile = SimpleNamespace(profile_id=uuid4(), model="test-model", status=ModelProfileStatus.ACTIVE,
                              base_url="https://example.invalid", credential_ref="synthetic")
    monkeypatch.setattr(database, "list_model_profiles", lambda: [profile])
    monkeypatch.setattr(app_module.model_client, "call_active_model", lambda *args, **kwargs:
                        ModelReply(content=json.dumps(_good_payload()), model="test-model", provider="test", latency_ms=0))
    news = [SimpleNamespace(title="第一篇新闻", published_raw="2026-09-01 12:30", content="摘要"),
            SimpleNamespace(title="第二篇新闻", published_raw="", content="摘要", url=None)]
    if not missing_news_url:
        news[0].url = "https://example.test/news-one"
    notices = [SimpleNamespace(art_code=f"AN000000000000000{i}", title=f"公告{i}",
                               notice_date_raw=f"2026-09-0{i}", ann_type="回购",
                               url=f"https://example.test/notice-{i}") for i in range(1, 4)]
    bodies = {
        notices[1].art_code: AnnouncementBody(art_code=notices[1].art_code, content="实际正文二", attach_url=None),
        notices[2].art_code: AnnouncementBody(art_code=notices[2].art_code, content="实际正文三", attach_url="https://example.test/body-three.pdf"),
    }
    monkeypatch.setattr(app_module, "fetch_cn_news", lambda *args, **kwargs: news)
    monkeypatch.setattr(app_module, "fetch_cn_announcements", lambda *args, **kwargs: notices)
    monkeypatch.setattr(app_module, "fetch_announcement_bodies", lambda *args, **kwargs: bodies)
    monkeypatch.setattr(lhb_feed, "fetch_billboard", lambda *args, **kwargs: [])
    monkeypatch.setattr(lhb_feed, "fetch_seats", lambda *args, **kwargs: [])
    if collab:
        replies = iter([{"observations": [], "gaps": []}, _good_payload(), {"risks": []}, _good_payload()])
        monkeypatch.setattr(app_module.model_client, "call_active_model", lambda *args, **kwargs:
                            ModelReply(content=json.dumps(next(replies)), model="test-model", provider="test", latency_ms=0))
        created = http.post("/evidence/collab-run", json={"symbol": "600001"}, headers=headers).json()
        assert created["ok"] is True, created
        for stage in app_module.collab_engine.STAGES:
            result = http.post("/evidence/collab-next", json={"run_id": created["run"]["run_id"]}, headers=headers).json()
            assert result["executed_stage"] == stage and result["executed_status"] == "done", result
        saved = database.get_ai_research_report(result["collab_report_id"])
        immediate_catalog = result["run"]["source_catalog"]
        result = {**saved, "persisted": result["persisted"]}
    else:
        result = run_stock_report(http, headers, symbol="600001", with_counter_check=False)
        immediate_catalog = result["source_catalog"]
    assert result["ok"] is True, result
    assert result["persisted"] is True
    assert "news" not in result["source_errors"], "Missing URL must not discard news evidence"
    documents = result["source_documents"]
    assert documents["S2"] == [
        {"title": news[0].title, "data_date": news[0].published_raw, "url": getattr(news[0], "url", "")},
        {"title": news[1].title, "data_date": "", "url": ""},
    ]
    assert len(documents["S3"]) == 3
    assert documents["S3A"] == [
        {"title": notices[1].title, "data_date": notices[1].notice_date_raw, "url": notices[1].url},
        {"title": notices[2].title, "data_date": notices[2].notice_date_raw, "url": bodies[notices[2].art_code].attach_url},
    ], "A failed body must stay title-only; each successful body keeps its own URL"
    saved = database.get_ai_research_report(result["report_id"])
    assert saved["source_documents"] == documents
    response = http.get(f"/ai-research/reports/{result['report_id']}", headers=headers)
    assert response.status_code == 200
    catalog = {row["source_id"]: row for row in response.json()["item"]["source_catalog"]}
    assert immediate_catalog == list(catalog.values()), "Immediate reading and historical detail must expose the same sources"
    for source_id in ("S2", "S3", "S3A"):
        assert catalog[source_id]["documents"] == documents[source_id]
    assert catalog["S3"]["quality"] == "B" and catalog["S3A"]["quality"] == "A"

    # Keep the exact successful HTTP view for browser/export acceptance; never rebuild identities by hand.
    audit_dir = Path(__file__).resolve().parents[3] / "docs/assets/history-audit-2026-10-25/r05"
    audit_dir.mkdir(parents=True, exist_ok=True)
    case = f"{'collab' if collab else 'stock'}-{'missing-news-url' if missing_news_url else 'news-url'}"
    (audit_dir / f"{case}.json").write_text(json.dumps({
        "scope": "Controlled network/model; real job or four collab stages, in-memory persistence and authenticated detail. No user database or external source truth.",
        "case": case,
        "immediate_catalog": immediate_catalog,
        "report": response.json()["item"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
