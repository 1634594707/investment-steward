"""HTTP checks for macro coverage, data periods and saved weight versions; isolated storage."""
from types import SimpleNamespace

import pytest

from test_t03_scan_history_http import scan_http  # noqa: F401
from investment_steward_core import macro_feed
from investment_steward_core.api import app as app_module


@pytest.mark.parametrize("region", ["us", "cn", "eu", "jp", "in"])
def test_macro_http_keeps_observation_period_separate_from_snapshot_time(scan_http, monkeypatch, region):
    http, headers, _ = scan_http
    monkeypatch.setattr(macro_feed, "get_manual_series", lambda key: None)

    def series(db, credential, cache_key, series_id, **kwargs):
        annual = kwargs.get("source") == "worldbank"
        return {"observations": [{"obs_date": "2025" if annual else "2026-08-01", "value": 2.5}],
                "as_of": "2026-09-01T00:00:00+00:00", "dataset_version": f"test:{series_id}"}

    monkeypatch.setattr(macro_feed, "_resolve_series", series)
    assert http.get(f"/evidence/macro/{region}").status_code == 401
    first = http.get(f"/evidence/macro/{region}", headers=headers)
    second = http.get(f"/evidence/macro/{region}", headers=headers)
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["indicators"] == second.json()["indicators"], "Refreshing a snapshot must not relabel the data period"
    assert body["positioning"] is not None
    for row in body["indicators"]:
        assert row["as_of"] == "2026-09-01T00:00:00+00:00"
        assert row["frequency"] in ("annual", "monthly", "daily")
        if row["frequency"] == "annual":
            assert row["obs_date"] == "2025"
    if region == "jp":
        missing = {row["dim"] for row in body["positioning"]["dims"] if row["score"] is None}
        assert missing == {"growth", "monetary"}


def test_macro_http_zero_available_weight_keeps_readings_without_composite(scan_http, monkeypatch):
    http, headers, _ = scan_http
    monkeypatch.setattr(macro_feed, "get_manual_series", lambda key: None)
    monkeypatch.setattr(macro_feed, "_resolve_series", lambda *args, **kwargs: {
        "observations": [{"obs_date": "2025", "value": 2.5}],
        "as_of": "2026-09-01T00:00:00+00:00", "dataset_version": "test:annual",
    })
    saved = http.post("/evidence/macro/weights", headers=headers, json={
        "weights": {"growth": 1.0, "employment": 0.0, "inflation": 0.0, "monetary": 0.0},
        "note": "All weight on an unavailable dimension",
    })
    assert saved.status_code == 200, saved.text
    response = http.get("/evidence/macro/jp", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["positioning"] is None
    assert response.json()["indicators"], "No composite must not discard real observations"
    versions = http.get("/evidence/macro/weights", headers=headers).json()
    assert versions["active_version"] == saved.json()["version"]


@pytest.mark.parametrize("declared", [True, False])
def test_manual_monthly_readings_keep_declared_frequency_and_original_dates(scan_http, monkeypatch, declared):
    """Manual supplements need the same grouping metadata; a missing frequency stays unknown."""
    http, headers, _ = scan_http
    monkeypatch.setattr(app_module, "resolve_store", lambda db: SimpleNamespace(get=lambda key: None))
    original = macro_feed.get_manual_series
    if not declared:
        def without_frequency(key):
            payload = original(key)
            return {k: v for k, v in payload.items() if k != "frequency"} if payload else None
        monkeypatch.setattr(macro_feed, "get_manual_series", without_frequency)
    monkeypatch.setattr(macro_feed, "_resolve_series", lambda *args, **kwargs: None)
    for _ in range(2):
        response = http.get("/evidence/macro/cn", headers=headers)
        assert response.status_code == 200, response.text
        rows = {row["indicator"]: row for row in response.json()["indicators"]}
        assert set(rows) == {"survey_unemployment", "tsf"}
        for key, as_of in (("survey_unemployment", "2026-08-17T00:00:00Z"), ("tsf", "2026-08-14T00:00:00Z")):
            assert rows[key]["frequency"] == ("monthly" if declared else None)
            assert rows[key]["obs_date"] == "2026-07-01"
            assert rows[key]["as_of"] == as_of
            assert rows[key]["source"] == "手动补录（官方公开值）"
