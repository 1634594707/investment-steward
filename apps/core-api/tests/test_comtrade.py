from __future__ import annotations

import json

from fastapi.testclient import TestClient
from investment_steward_core import comtrade_feed
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


def _client(tmp_path, monkeypatch):
    """必须隔离 Windows 凭据管理器：本机若保存过真实 comtrade_key，OS 级读取会让
    「无 key」前提失效，且 PUT 凭据会覆盖用户桌面真实密钥（测试环境与用户桌面共用凭据管理器）。"""
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    token = "comtrade-test-token"
    return TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path))), {"X-Core-Session-Token": token}


def test_comtrade_key_is_independent_and_missing_key_is_explicit(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)

    credentials = client.get("/credentials", headers=headers)
    assert credentials.status_code == 200
    assert "comtrade_key" not in {row["key_id"] for row in credentials.json()}

    response = client.get("/evidence/comtrade", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["data"] == []
    assert "comtrade_key" in body["degraded_reason"]


def test_comtrade_preview_reports_api_rows_without_exposing_key(tmp_path, monkeypatch):
    client, headers = _client(tmp_path, monkeypatch)
    secret = "comtrade-secret-value"
    assert client.put("/credentials/comtrade_key", headers=headers, json={"secret": secret}).status_code == 200

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            return json.dumps({"count": 1, "data": [{"reporterCode": 156, "primaryValue": 12.3}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["key"] = next((value for key, value in request.header_items() if key.lower() == "ocp-apim-subscription-key"), None)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(comtrade_feed.urllib.request, "urlopen", fake_urlopen)
    response = client.get("/evidence/comtrade?reporter_code=156&period=2025&max_records=10", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["count"] == 1
    assert body["data"][0]["primaryValue"] == 12.3
    assert secret not in response.text
    assert captured == {
        "url": "https://comtradeapi.un.org/public/v1/preview/C/A/HS?cmdCode=TOTAL&flowCode=M&partnerCode=0&partner2Code=0&motCode=0&maxRecords=10&reporterCode=156&period=2025",
        "key": secret,
        "timeout": 15,
    }
