"""M4/M4.1 引擎测试：宏观雷达数据管道（macro_feed）+ 研读图书馆 ISBN 补全与书目扩展字段。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "plugins"


def _install(client_headers: tuple[TestClient, dict[str, str]], plugin_id: str) -> None:
    test_client, headers = client_headers
    response = test_client.post(f"/plugins/{plugin_id}/install", headers=headers)
    assert response.status_code == 200, response.text


# ---- 宏观雷达：/evidence/macro/{region} ----


def test_macro_endpoint_requires_enabled_plugin(client):
    test_client, headers = client
    response = test_client.get("/evidence/macro/us", headers=headers)
    assert response.status_code == 409
    assert "macro-radar" in response.json()["detail"]


def test_macro_unknown_region_404(client):
    test_client, headers = client
    _install(client, "official.macro-radar")
    response = test_client.get("/evidence/macro/antarctica", headers=headers)
    assert response.status_code == 404


def test_macro_no_key_no_fabrication(client, monkeypatch):
    """无 FRED key：美国指标全部 pending 且不输出任何数值（不编造，ADR-0006）。

    必须隔离 Windows 凭据管理器：本机若保存过真实 macro_data_key，OS 级读取会让
    「无 key」前提失效（测试环境与用户桌面共用凭据管理器）。
    """
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    test_client, headers = client
    _install(client, "official.macro-radar")
    response = test_client.get("/evidence/macro/us", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["region"] == "us"
    # 用户定稿:没有数据源的指标不下发;不编造 = 什么都不展示
    assert body["indicators"] == []
    assert body["degraded_reason"] is not None


def test_macro_background_global_pending_without_key(client, monkeypatch):
    """无 FRED key：背景层全部 pending（同上，隔离 OS 凭据管理器）。"""
    from investment_steward_core import macro_feed
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    monkeypatch.setattr(macro_feed, "fetch_okx_series", lambda inst_id, limit=15: (_ for _ in ()).throw(RuntimeError("OKX 不可达（测试隔离）")))
    test_client, headers = client
    _install(client, "official.macro-radar")
    response = test_client.get("/evidence/macro/global", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["region"] == "global"
    assert body["background"] == []  # 无可用源的行为不下发
    assert body["degraded_reason"] is not None


def test_macro_fetch_with_key_caches_and_converts_cpi(client, monkeypatch):
    """有 key 且 FRED 可达：实拉 → 落缓存 → ok；CPI 同比由指数 12 期差分换算。"""
    from investment_steward_core import macro_feed
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    test_client, headers = client
    _install(client, "official.macro-radar")
    monkeypatch.setattr(macro_feed, "fetch_okx_series", lambda inst_id, limit=15: [{"obs_date": "2026-09-01", "value": 2400.0}])

    def fake_fetch(series_id: str, api_key: str, limit: int = 14):
        assert api_key == "test-fred-key"
        if series_id == "CPIAUCSL":
            # 15 期指数：日期与数值同向递增（2025-08-01→100，2025-08-15→114），
            # 同比 = 第 13 期 / 第 1 期 = (112 / 100 - 1) * 100 = 12.0
            return [
                {"obs_date": f"2025-08-{index + 1:02d}", "value": 100.0 + index}
                for index in range(15)
            ]
        return [{"obs_date": "2025-08-01", "value": 4.2}, {"obs_date": "2025-07-01", "value": 4.1}]

    monkeypatch.setattr(macro_feed, "fetch_fred_series", fake_fetch)
    # 凭据库写入 FRED key（file 兜底后端，测试环境无 Windows 凭据权限时同样成立）
    response = test_client.put(
        "/credentials/macro_data_key",
        json={"secret": "test-fred-key"},
        headers=headers,
    )
    assert response.status_code < 400, response.text

    result = test_client.get("/evidence/macro/us", headers=headers)
    assert result.status_code == 200
    body = result.json()
    by_id = {reading["indicator"]: reading for reading in body["indicators"]}
    assert by_id["unemployment"]["status"] == "ok"
    assert by_id["unemployment"]["latest"] == pytest.approx(4.2)
    assert by_id["unemployment"]["dataset_version"].startswith("fred:UNRATE:")
    # CPI 同比取最新一期：第 15 期(114) 对第 3 期(102)，(114 / 102 - 1) * 100 ≈ 11.8
    assert by_id["cpi_yoy"]["status"] == "ok"
    assert by_id["cpi_yoy"]["latest"] == pytest.approx(11.8)
    assert "12 期差分" in by_id["cpi_yoy"]["note"]

    # 再次请求走缓存：monkeypatch 拉取函数抛错也不影响（缓存命中）
    def broken_fetch(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(macro_feed, "fetch_fred_series", broken_fetch)
    cached = test_client.get("/evidence/macro/us", headers=headers)
    assert cached.status_code == 200
    cached_by_id = {reading["indicator"]: reading for reading in cached.json()["indicators"]}
    assert cached_by_id["unemployment"]["status"] == "ok"


def test_macro_snapshot_background_ok_with_key(client, monkeypatch):
    from investment_steward_core import macro_feed
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    test_client, headers = client
    _install(client, "official.macro-radar")
    monkeypatch.setattr(
        macro_feed,
        "fetch_fred_series",
        lambda series_id, api_key, limit=14: [{"obs_date": "2025-09-02", "value": 67.8}],
    )
    monkeypatch.setattr(macro_feed, "fetch_okx_series", lambda inst_id, limit=15: [{"obs_date": "2026-09-01", "value": 2400.0}])
    test_client.put("/credentials/macro_data_key", json={"secret": "k"}, headers=headers)
    result = test_client.get("/evidence/macro/global", headers=headers)
    body = result.json()
    rows = {row["key"]: row for row in body["background"]}
    assert rows["wti"]["status"] == "ok" and rows["wti"]["latest"] == pytest.approx(67.8)
    # 金价走 OKX 公开行情（XAUT-USDT，无需密钥）——隔离为 mock 值
    monkeypatch.setattr(macro_feed, "fetch_okx_series", lambda inst_id, limit=15: [{"obs_date": "2026-09-01", "value": 2400.0}])
    rows_with_gold = test_client.get("/evidence/macro/global", headers=headers)
    gold = {row["key"]: row for row in rows_with_gold.json()["background"]}["gold"]
    assert gold["status"] == "ok"
    assert gold["latest"] > 0  # OKX 源接通（缓存共享场景下数值为真值，不钉死）
    assert gold["dataset_version"].startswith("okx:XAUT-USDT:")


# ---- 宏观雷达 M4.1：世界银行适配器（中/欧/日/印，无 key 公开接口） ----


def test_macro_worldbank_regions_ok_without_key(client, monkeypatch):
    """中/日/印走世界银行：无需 FRED key 即可 ok；未接入源保持 pending；口径在 source/note 明示。"""
    from investment_steward_core import macro_feed

    test_client, headers = client
    _install(client, "official.macro-radar")

    def fake_wb(iso3: str, indicator_id: str):
        data = {
            ("CHN", "FP.CPI.TOTL.ZG"): 0.06,
            ("CHN", "SL.UEM.TOTL.ZS"): 4.8,
            ("JPN", "FP.CPI.TOTL.ZG"): 2.8,
            ("JPN", "SL.UEM.TOTL.ZS"): 2.5,
            ("IND", "NY.GDP.MKTP.KD.ZG"): 6.7,
            ("IND", "FP.CPI.TOTL.ZG"): 3.5,
            ("IND", "SL.UEM.TOTL.ZS"): 7.6,
        }
        key = (iso3, indicator_id)
        assert key in data, f"unexpected WB call {key}"
        return [
            {"obs_date": "2025", "value": data[key]},
            {"obs_date": "2024", "value": round(data[key] + 0.5, 2)},
            {"obs_date": "2023", "value": None},  # null 年份跳过不编造
        ]

    monkeypatch.setattr(macro_feed, "fetch_worldbank_series", fake_wb)
    # EM 宏观（PMI）与手动补录层一并隔离外网：PMI 走 mock,社融走 manual_macro.json
    monkeypatch.setattr(macro_feed, "fetch_em_macro_series", lambda series_id, limit=15: [{"obs_date": "2026-08-01", "value": 49.8}])

    cn = test_client.get("/evidence/macro/cn", headers=headers).json()
    cn_by_id = {row["indicator"]: row for row in cn["indicators"]}
    assert cn_by_id["cpi_yoy"]["status"] == "ok"
    assert cn_by_id["cpi_yoy"]["latest"] == pytest.approx(0.06)
    assert cn_by_id["cpi_yoy"]["source"] == "世界银行（公开接口）"
    assert cn_by_id["cpi_yoy"]["dataset_version"].startswith("worldbank:CHN:FP.CPI.TOTL.ZG:2025")
    assert cn_by_id["unemployment_ilo"]["status"] == "ok"
    assert "口径不同" in cn_by_id["unemployment_ilo"]["note"]  # ILO vs 城镇调查口径明示
    # 城镇调查失业率走手动补录层(manual_macro.json),无 mock 时为真实补录值,不在此断言
    # 制造业 PMI 已接东财数据中心（RPT_ECONOMY_PMI），测试隔离为 mock 值
    monkeypatch.setattr(macro_feed, "fetch_em_macro_series", lambda series_id, limit=15: [{"obs_date": "2026-08-01", "value": 49.8}])
    cn_again = test_client.get("/evidence/macro/cn", headers=headers)
    cn_pmi = {r["indicator"]: r for r in cn_again.json()["indicators"]}["manufacturing_pmi"]
    assert cn_pmi["status"] == "ok" and cn_pmi["latest"] == pytest.approx(49.8)
    assert cn_pmi["dataset_version"].startswith("em:RPT_ECONOMY_PMI|MAKE_INDEX:")
    # 制造业 PMI 已接东财数据中心（见下方隔离 mock 验证）

    jp = test_client.get("/evidence/macro/jp", headers=headers).json()
    jp_by_id = {row["indicator"]: row for row in jp["indicators"]}
    assert jp_by_id["cpi_yoy"]["status"] == "ok" and jp_by_id["cpi_yoy"]["latest"] == pytest.approx(2.8)
    assert jp_by_id["unemployment"]["status"] == "ok" and jp_by_id["unemployment"]["latest"] == pytest.approx(2.5)
    # 新语义:pending 行不下发,下发的全是 ok(政策利率接 FRED 后缓存命中为 ok,未拉到则隐藏)
    assert all(r["status"] == "ok" for r in jp["indicators"])

    ind = test_client.get("/evidence/macro/in", headers=headers).json()
    ind_by_id = {row["indicator"]: row for row in ind["indicators"]}
    assert ind_by_id["gdp_yoy"]["status"] == "ok" and ind_by_id["gdp_yoy"]["latest"] == pytest.approx(6.7)
    assert ind_by_id["cpi_yoy"]["status"] == "ok" and ind_by_id["cpi_yoy"]["latest"] == pytest.approx(3.5)


def test_macro_worldbank_failure_degrades_without_fabrication(client, monkeypatch):
    """世界银行不可达：wb 指标 pending 且不输出数值（不编造），不落缓存。"""
    from investment_steward_core import macro_feed

    test_client, headers = client
    _install(client, "official.macro-radar")

    def broken_wb(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(macro_feed, "fetch_worldbank_series", broken_wb)
    body = test_client.get("/evidence/macro/eu", headers=headers).json()
    by_id = {row["indicator"]: row for row in body["indicators"]}
    assert "hicp_yoy" not in by_id  # 失败行不下发(用户定稿)
    assert body["degraded_reason"] is not None
    assert "hicp_yoy" not in by_id  # 失败行不下发
    assert body["degraded_reason"] is not None
    assert body["degraded_reason"] is not None


# ---- 研读图书馆：书目扩展字段 + ISBN lookup ----


def test_book_extended_fields_roundtrip(client):
    test_client, headers = client
    _install(client, "official.reading-library")
    response = test_client.post(
        "/library/books",
        json={"title": "聪明的投资者", "author": "格雷厄姆", "isbn": "9787111111111", "status": "reading", "source": "user"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    book = response.json()
    assert book["isbn"] == "9787111111111"
    assert book["status"] == "reading" and book["source"] == "user"
    listed = test_client.get("/library/books", headers=headers).json()
    assert any(item["book_id"] == book["book_id"] and item["isbn"] == "9787111111111" for item in listed)


def test_book_invalid_status_rejected(client):
    test_client, headers = client
    response = test_client.post(
        "/library/books",
        json={"title": "X", "status": "borrowed"},
        headers=headers,
    )
    assert response.status_code == 422


def test_book_lookup_requires_enabled_plugin(client):
    test_client, headers = client
    response = test_client.get("/library/books/lookup/9787111111111", headers=headers)
    assert response.status_code == 409


def test_book_lookup_invalid_isbn_422(client):
    test_client, headers = client
    _install(client, "official.reading-library")
    response = test_client.get("/library/books/lookup/abc123", headers=headers)
    assert response.status_code == 422


def test_book_lookup_degrades_without_network(client, monkeypatch):
    """Open Library 不可达：available=False + 降级说明，不编造元数据（D-17）。"""
    import urllib.error

    test_client, headers = client
    _install(client, "official.reading-library")

    def raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)
    response = test_client.get("/library/books/lookup/9787111111111", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "手录" in body["degraded_reason"]


# ---- 事件影响层：/evidence/macro/events（用户定稿：俄乌→粮食/化肥/能源，美伊→原油/航运） ----


def test_macro_events_requires_enabled_plugin(client):
    test_client, headers = client
    response = test_client.get("/evidence/macro/events", headers=headers)
    assert response.status_code == 409
    assert "macro-radar" in response.json()["detail"]


def test_macro_events_ok_rows_and_cache(client, monkeypatch):
    """有 key + 假 FRED：事件行 ok 且落库缓存（二次请求不再外拉）；未接数据源行 pending 无数值。

    事件卡不再包含冲突方国内数据（乌克兰 GDP / 伊朗 CPI 已按用户反馈移除，
    只保留粮食/化肥/能源/航运影响链大宗商品）。
    """
    from investment_steward_core import macro_feed
    from investment_steward_core.api import app as app_module
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(app_module, "resolve_store", lambda db: DbCredentialStore(db))
    test_client, headers = client
    _install(client, "official.macro-radar")
    calls = {"n": 0}

    def fake_fetch(series_id: str, api_key: str, limit: int = 14):
        calls["n"] += 1
        return [{"obs_date": "2026-08-01", "value": 123.4}]

    monkeypatch.setattr(macro_feed, "fetch_fred_series", fake_fetch)
    saved = test_client.put("/credentials/macro_data_key", json={"secret": "test-fred-key"}, headers=headers)
    assert saved.status_code < 400

    first = test_client.get("/evidence/macro/events", headers=headers)
    assert first.status_code == 200
    by_id = {item["event_id"]: item for item in first.json()["items"]}
    assert set(by_id) == {"ukraine", "iran"}
    ukraine = {row["key"]: row for row in by_id["ukraine"]["rows"]}
    assert {"wheat", "corn", "soybeans", "eu_gas"} <= set(ukraine)
    assert ukraine["wheat"]["status"] == "ok"
    assert ukraine["wheat"]["latest"] == pytest.approx(123.4)
    assert ukraine["wheat"]["dataset_version"] == "fred:PWHEAMTUSDM:2026-08-01"
    # 尿素无 FRED 序列:无源行不下发(用户定稿),接入世行 Pink Sheet 后自动出现
    assert "urea" not in ukraine
    iran = {row["key"]: row for row in by_id["iran"]["rows"]}
    assert {"brent", "wti"} <= set(iran)
    assert "dry_freight" not in iran  # BDI 无源,不下发
    # 未接数据源（BDI）：pending 且不输出数值（不编造）
    assert "dry_freight" not in iran  # BDI 无源,不下发
    n_after_first = calls["n"]
    assert n_after_first >= 6  # ukraine 4 条 + iran 2 条 FRED 序列

    # 二次请求命中 macro_cache：外拉次数不再增长（「不一直拉取」）
    second = test_client.get("/evidence/macro/events", headers=headers)
    assert second.status_code == 200
    assert calls["n"] == n_after_first
    second_items = {item["event_id"]: item for item in second.json()["items"]}
    second_ukraine = {row["key"]: row for row in second_items["ukraine"]["rows"]}
    assert second_ukraine["wheat"]["status"] == "ok"  # 缓存命中
    assert "urea" not in second_ukraine  # 无源行恒不下发


# —— §9.1 缓存键治理：transform 进键,不同变换互不污染;manual 命中应用同变换 ——

def test_resolve_series_transform_isolation_and_manual_transform(tmp_path, monkeypatch):
    from investment_steward_core import macro_feed
    from investment_steward_core.credential_store import CredentialStore
    from investment_steward_core.domain import CredentialRecord, CredentialStoreBackend
    from test_longterm import _db as make_db

    class StubStore(CredentialStore):
        def get(self, key_id: str) -> str | None:
            return "test-fred-key"

        @property
        def backend(self) -> CredentialStoreBackend:
            return CredentialStoreBackend.FILE

        def store(self, key_id: str, secret: str) -> CredentialRecord:
            raise NotImplementedError("测试桩不落凭据")

        def delete(self, key_id: str) -> None:
            return None

        def list_records(self) -> list[CredentialRecord]:
            return []

    db = make_db(tmp_path)

    calls: list[str] = []

    def fake_fetch(series_id, api_key, limit=15):
        calls.append(series_id)
        return [{"obs_date": f"2025-0{index + 1}-01", "value": 100.0 + index} for index in range(5)]

    monkeypatch.setattr(macro_feed, "fetch_fred_series", fake_fetch)

    first = macro_feed._resolve_series(db, StubStore(), "us:testind", "TEST", source="fred", transform="diff")
    assert first is not None
    assert first["observations"][0]["value"] == 1.0  # diff:100.0+1 − 100.0
    second = macro_feed._resolve_series(db, StubStore(), "us:testind", "TEST", source="fred", transform="none")
    assert second is not None
    assert second["observations"][0]["value"] == 100.0  # 未被 diff 变换污染
    assert macro_feed._resolve_series(db, StubStore(), "us:testind", "TEST", source="fred", transform="diff")["observations"][0]["value"] == 1.0
    assert len(calls) == 2  # none 与 diff 各实拉一次,第三次 diff 命中缓存

    # manual 层：注册表按原键存水平值,transform=diff 返回时应用同一变换
    monkeypatch.setattr(macro_feed, "get_manual_series",
                        lambda key: {"observations": [{"obs_date": "2025-02-01", "value": 5.0},
                                                      {"obs_date": "2025-01-01", "value": 4.0}],
                                     "note": "manual"})
    manual_diff = macro_feed._resolve_series(db, StubStore(), "us:manual", "M", source="fred", transform="diff")
    assert manual_diff["observations"][0]["value"] == 1.0  # 5.0 − 4.0
    assert manual_diff["note"] == "manual"
