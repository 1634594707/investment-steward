"""冻结证据的时效随查看日期变化，采集时间不能证明数据新鲜。"""
from datetime import date

from investment_steward_core import report_quality
from test_t03_scan_history_http import scan_http  # noqa: F401


def test_health_rechecks_frozen_dates_without_mutating_report():
    report = {"source_keys": ["S1", "S4"], "source_asof": {"S1": "2026-10-19", "S4": "2026-06-30"},
              "source_meta": {"S1": {"retrieved_at": "2026-10-26T01:00:00Z", "provider": "test"}}}
    first = report_quality.report_source_health(report, today=date(2026, 10, 26))
    later = report_quality.report_source_health(report, today=date(2026, 10, 27))
    assert [row["state"] for row in first["sources"]] == ["within_threshold", "within_threshold"]
    assert later["sources"][0]["state"] == "stale"
    assert later["sources"][1]["state"] == "within_threshold"
    assert later["sources"][0]["cache_age_days"] is None
    assert "source_health" not in report


def test_failure_and_unknown_dates_never_become_healthy_from_recent_collection():
    result = report_quality.report_source_health({
        "source_keys": ["S1", "S2", "S3", "S4"],
        "source_errors": {"S5": "连接失败"},
        "source_asof": {"S2": "invalid", "S3": "2026-10-27", "S4": "2026-02-30"},
        "source_meta": {"S1": {"retrieved_at": "2026-10-26T00:00:00Z"}},
    }, today=date(2026, 10, 26))
    assert [row["state"] for row in result["sources"]] == ["unknown_date"] * 4 + ["failed"]
    assert result["sources"][-1]["error"] == "连接失败"
    assert all(row["source_id"] != "S6" for row in result["sources"]), "未上榜不是缺失来源"


def test_legacy_missing_source_inventory_is_unknown_not_a_clean_bill():
    assert report_quality.report_source_health({}, today=date(2026, 10, 26))["sources"] == []


def test_detail_replaces_old_health_view_without_rewriting_archive(scan_http):  # noqa: F811
    http, headers, database = scan_http
    report = {"report_id": "source-health-history", "kind": "stock", "subject": "000001",
              "generated_at": "2020-01-01T00:00:00Z", "source_keys": ["S1"],
              "source_asof": {"S1": "2020-01-01"},
              "source_health": {"checked_on": "2020-01-01", "sources": []}}
    database.insert_ai_research_report(report)
    before = database.get_ai_research_report(report["report_id"])
    response = http.get(f"/ai-research/reports/{report['report_id']}", headers=headers)
    assert response.status_code == 200
    health = response.json()["item"]["source_health"]
    assert health["checked_on"] != "2020-01-01"
    assert health["sources"][0]["state"] == "stale"
    assert database.get_ai_research_report(report["report_id"]) == before


def test_health_summary_is_opt_in_page_scoped_and_matches_detail(scan_http, monkeypatch):  # noqa: F811
    http, headers, database = scan_http
    for index in range(3):
        database.insert_ai_research_report({
            "report_id": f"health-{index}", "kind": "stock", "subject": f"00000{index}",
            "generated_at": f"2020-01-0{index + 1}T00:00:00Z", "report": "private report body",
            "source_keys": ["S1"], "source_asof": {"S1": "2020-01-01"},
            "source_errors": {"S2": "连接失败"},
        })
    before = database.export_payload_tables()["ai_research_reports"]
    reads = []
    original = type(database).get_ai_research_reports_by_ids

    def read_page(db, ids):
        reads.append(ids)
        return original(db, ids)

    monkeypatch.setattr(type(database), "get_ai_research_reports_by_ids", read_page)
    path = "/ai-research/reports?kind=stock&limit=1&offset=1"
    ordinary = http.get(path, headers=headers).json()
    assert "source_health" not in ordinary["items"][0], "默认列表继续保持轻量契约"
    assert reads == [], "未开启健康摘要时不读取额外正文 payload"
    result = http.get(path + "&include_source_health=true", headers=headers).json()
    assert result["total"] == 3 and len(result["items"]) == 1
    item = result["items"][0]
    assert item["report_id"] == "health-1"
    assert reads == [["health-1"]], "只读取当前页，不对全库报告逐条查详情"
    detail = http.get(f"/ai-research/reports/{item['report_id']}", headers=headers).json()["item"]
    assert item["source_health"] == detail["source_health"]
    assert [row["state"] for row in item["source_health"]["sources"]] == ["stale", "failed"]
    assert "report" not in item and "source_errors" not in item
    counts = http.get(path + "&count_only=true&include_source_health=true", headers=headers).json()
    assert counts["items"] == [] and counts["total"] == 3
    assert reads == [["health-1"]], "仅计数请求不读取来源快照"
    assert database.export_payload_tables()["ai_research_reports"] == before
    assert http.get(path + "&include_source_health=true").status_code == 401
