"""阶段 8 · cn-market-data 公告证据能力（纯 stdlib，不引重依赖）。

用模块级 `_ann_http_get` 打桩为确定性数据，验证：取数→入账为 `Announcement`
类型 Evidence（含来源定位 raw_locator、来源 URI、ADR-0004 的 local-personal-use
授权标注、稳定内容哈希）；插件被撤销时端点被阻断；取数失败时 available=False 且
不生成任何证据（不编造，ADR-0004 不变量 #4）。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import news_evidence
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

_ANN_FIXTURE = {
    "data": {
        "list": [
            {
                "title": "公司关于重大合同签订的公告",
                "notice_date": "2026-08-20 09:30:00",
                "art_code": "20280820000123",
                "columns_name": "重大合同",
            },
            {
                "title": "年度业绩预增公告",
                "notice_date": "2026-08-18",
                "art_code": "20280818000456",
                "columns_name": "业绩预告",
            },
        ]
    }
}


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


# —— 纯函数：内容哈希 / 时间解析 / 来源定位 ——


def test_content_hash_is_stable_and_sensitive():
    assert news_evidence.content_hash_for("600000", "a1", "标题", "2026-08-20") == news_evidence.content_hash_for(
        "600000", "a1", "标题", "2026-08-20"
    )
    assert news_evidence.content_hash_for(
        "600000", "a1", "标题", "2026-08-20"
    ) != news_evidence.content_hash_for("600000", "a1", "标题", "2026-08-21")


def test_parse_notice_timestamp_variants():
    assert news_evidence.parse_notice_timestamp("") is None
    from datetime import UTC, datetime

    parsed = news_evidence.parse_notice_timestamp("2026-08-20 09:30:00")
    assert parsed == datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    # 未知格式不猜测 → None
    assert news_evidence.parse_notice_timestamp("not-a-date") is None


def test_raw_locator_keeps_original_positioning():
    ann = news_evidence.Announcement(
        symbol="600000", title="标题", notice_date_raw="2026-08-20",
        art_code="20280820000123", ann_type="重大合同",
        url="https://finance.eastmoney.com/a/20280820000123.html",
    )
    loc = json.loads(news_evidence.raw_locator_for("https://np-anotice-stock.eastmoney.com/api/security/ann", "600000", ann))
    assert loc["art_code"] == "20280820000123"
    assert loc["symbol"] == "600000"


# —— 契约：端点入账为 Announcement 证据 ——


def test_endpoint_produces_announcement_evidence(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(news_evidence, "_ann_http_get", lambda _params: _ANN_FIXTURE)

    response = test_client.get("/evidence/announcements/600000", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert len(body["entries"]) == 2
    first = body["entries"][0]
    assert first["evidence_type"] == "announcement"
    assert first["source_name"] == "中国市场公告（东方财富）"
    assert first["license_status"] == "local-personal-use"
    assert first["subject_refs"] == ["instrument:CN:600000"]
    assert first["source_uri"].startswith("https://finance.eastmoney.com/a/")
    raw = json.loads(first["raw_locator"])
    assert raw["art_code"] == "20280820000123"
    assert len(first["content_hash"]) >= 16

    # 已入账到本地证据账本，可读回。
    evidence = test_client.get("/evidence", headers=headers).json()
    ids = {item["evidence_id"] for item in evidence}
    assert all(item["evidence_id"] in ids for item in body["entries"])


def test_endpoint_blocks_when_plugin_revoked(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(news_evidence, "_ann_http_get", lambda _params: _ANN_FIXTURE)
    assert test_client.post("/plugins/official.cn-market-data/revoke", headers=headers).status_code == 200
    assert test_client.get("/evidence/announcements/600000", headers=headers).status_code == 409


def test_endpoint_degrades_without_fabricating_on_failure(monkeypatch, client):
    test_client, headers = client
    before = len(test_client.get("/evidence", headers=headers).json())

    def _offline(_params: dict[str, object]) -> dict[str, object]:
        raise news_evidence.NoticeError("模拟公告源不可达")

    monkeypatch.setattr(news_evidence, "_ann_http_get", _offline)
    response = test_client.get("/evidence/announcements/600001", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["entries"] == []
    # 不生成任何证据（不编造）。
    assert len(test_client.get("/evidence", headers=headers).json()) == before


# —— 阶段 8 · 新闻检索证据（东财搜索 JSONP，纯 stdlib） ——

_NEWS_PAYLOAD = {
    "result": {
        "cmsArticleWebOld": [
            {
                "title": "<em>某公司</em>公布一季度业绩",
                "content": "业绩摘要内容\u3000含全角空格去噪",
                "date": "2026-09-01 08:00:00",
                "code": "202609010001",
                "mediaName": "上海证券报",
            },
            {
                "title": "行业观察：板块轮动",
                "content": "行业动态速览",
                "date": "2026-08-30",
                "code": "202608300002",
                "mediaName": "证券时报",
            },
        ]
    }
}


def test_clean_text_strips_highlight_and_fullwidth_space():
    assert news_evidence._clean_text("<em>重点</em>新闻\u3000去噪") == "重点新闻去噪"


def test_content_hash_for_news_stable_and_sensitive():
    # v23 查重归一：同标题同稿件即同一条（稿件号/正文摘要不参与哈希，换号重发不再重复入账）
    a = news_evidence.content_hash_for_news("600000", "c1", "标题", "正文")
    assert a == news_evidence.content_hash_for_news("600000", "c1", "标题", "正文")
    assert a == news_evidence.content_hash_for_news("600000", "c2", "标题", "其它正文")
    assert a != news_evidence.content_hash_for_news("600000", "c1", "另一标题", "正文")


def test_endpoint_produces_news_evidence(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(news_evidence, "_fetch_news_payload", lambda _symbol, _limit: _NEWS_PAYLOAD)

    response = test_client.get("/evidence/news/600000", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert len(body["entries"]) == 2
    first = body["entries"][0]
    assert first["evidence_type"] == "analysis"  # 模型无 NEWS 字面量，取最近义证据桶
    assert first["source_name"] == "中国市场新闻（东方财富）"
    assert first["license_status"] == "local-personal-use"
    assert first["subject_refs"] == ["instrument:CN:600000"]
    assert first["source_uri"] == "https://finance.eastmoney.com/a/202609010001.html"
    assert "<em>" not in first["summary"]  # 高亮标签已剥离
    assert first["summary"] == "某公司公布一季度业绩"
    assert len(first["content_hash"]) >= 16


def test_endpoint_news_blocks_when_plugin_revoked(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(news_evidence, "_fetch_news_payload", lambda _symbol, _limit: _NEWS_PAYLOAD)
    assert test_client.post("/plugins/official.cn-market-data/revoke", headers=headers).status_code == 200
    assert test_client.get("/evidence/news/600000", headers=headers).status_code == 409


def test_endpoint_news_degrades_without_fabricating_on_failure(monkeypatch, client):
    test_client, headers = client
    before = len(test_client.get("/evidence", headers=headers).json())

    def _offline(_symbol: str, _limit: int) -> dict[str, object]:
        raise news_evidence.NewsError("模拟新闻源不可达")

    monkeypatch.setattr(news_evidence, "_fetch_news_payload", _offline)
    response = test_client.get("/evidence/news/600001", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["entries"] == []
    assert len(test_client.get("/evidence", headers=headers).json()) == before