"""D1（研报产出与交付链路路线图 2026-09-27）：公告正文抓取，让 A 级证据有产生路径。

**要解决的问题**：报告里长期写着「本次无一手公告/财报原文（A 级 0 条）」——不是产品没有
公告，而是链路只取了**标题**（`SOURCE_PROFILES["S3"]` 只能判 B 级），而 A 股投资者最认的
恰恰是交易所披露原文。

**本文件锁定四件事**：

1. 正文抓取本身：形状预校验、失败形态（`{"success":0}` / 空 `notice_content`）一律抛
   `NoticeError`，**不返回空壳、不用标题充数**；TTL 缓存命中不重复请求；
2. 落库口径：取到正文 → `summary` 含全文、`source_dataset_version` 带 `+content`、
   `raw_locator.body_available=true`；取不到 → 仍是标题口径（两条路径都不编造）；
3. 升级出口：历史「仅标题」记录在拿到正文后**原地升级**（id 与内容哈希都不变，
   不产生重复证据）；
4. 报告链路：正文进入证据包时登记 A 级来源 `S3A`，`evidence_quality.tiers.A == ["S3A"]`；
   取不到时**不建 S3A**、`tiers.A == []`，且 note 如实说明本次为何没有 A 级。

模型与网络全部打桩（`model_client._post_json` + 两个公告接口），测试绝不联网、不花钱。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import client as client_fixture, run_stock_report  # noqa: F401  确保 fixture 可用
from investment_steward_core import news_evidence
from investment_steward_core import report_quality
from test_v31_repair_and_comparison import (  # noqa: F401  复用固定证据快照与样本
    _CannedModel,
    _good_payload,
    _setup,
)

_ART_CODE = "AN202608141827994407"
_ANN_TITLE = "甲股关于回购公司股份的公告"
_ATTACH_URL = "https://pdf.dfcfw.com/pdf/H2_AN202608141827994407_1.pdf"
_BODY_TEXT = (
    "证券简称：甲股 证券代码：600001\n"
    "甲股股份有限公司关于回购公司股份的公告\n\n"
    "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏。\n\n"
    "重要内容提示：\n"
    "●回购资金总额：不低于人民币 1.00 亿元，不超过人民币 2.00 亿元。\n\n"
    "特此公告。\n\n甲股股份有限公司董事会\n2026 年 9 月 2 日"
)


def _announcement(art_code: str = _ART_CODE) -> SimpleNamespace:
    return SimpleNamespace(
        notice_date_raw="2026-09-02",
        title=_ANN_TITLE,
        ann_type="回购",
        url="https://finance.eastmoney.com/a/x.html",
        art_code=art_code,
    )


@pytest.fixture(autouse=True)
def _clear_announcement_caches():
    """`news_evidence` 的 TTL 缓存/失败冷却是**模块级**的，会跨用例泄漏。

    不清理的话，「先成功后失败」的两个用例会互相污染：后一个用例打到前一个留下的缓存，
    拿到本该「取不到」的正文。这里在每个用例前后都清一遍，保证打桩即真相。
    """
    def _reset() -> None:
        news_evidence._cache.clear()
        news_evidence._fail_cache.clear()
        news_evidence._content_cache.clear()
        news_evidence._content_fail_cache.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# 1. 正文抓取本身（纯函数 + 打桩 HTTP）
# ---------------------------------------------------------------------------

def test_art_code_shape_is_prechecked_so_placeholder_never_triggers_a_request(monkeypatch):
    """形状不合格的 art_code 不发请求：既不浪费一次出网，也不拿占位号去撞运气。"""
    calls: list[str] = []

    def _record(art_code: str) -> dict[str, object]:
        calls.append(art_code)
        return {"data": {"notice_content": "正文"}}

    monkeypatch.setattr(news_evidence, "_ann_content_http_get", _record)
    items = [_announcement("A1"), _announcement(""), _announcement(_ART_CODE)]
    targets = news_evidence.announcement_body_targets(items)
    assert [item.art_code for item in targets] == [_ART_CODE]
    assert calls == [], "形状预校验必须发生在发起请求之前"


def test_body_limit_keeps_request_count_bounded(monkeypatch):
    """正文抓取只对最近 N 条发起——请求量不随公告条数线性放大。"""
    items = [_announcement(f"AN2026081418279944{index:02d}") for index in range(10)]
    assert len(news_evidence.announcement_body_targets(items)) == news_evidence.ANNOUNCEMENT_BODY_LIMIT
    assert len(news_evidence.announcement_body_targets(items, 2)) == 2


def test_content_failure_forms_all_raise_instead_of_returning_an_empty_shell(monkeypatch):
    """两种失败形态（无 data 节点 / notice_content 为空）都必须抛错，不许产出空壳。"""
    monkeypatch.setattr(news_evidence, "_ann_content_http_get", lambda _code: {"success": 0, "error": "x"})
    with pytest.raises(news_evidence.NoticeError):
        news_evidence.fetch_announcement_content("AN0000000000000001")

    monkeypatch.setattr(
        news_evidence, "_ann_content_http_get", lambda _code: {"data": {"notice_content": "   \n  "}}
    )
    with pytest.raises(news_evidence.NoticeError):
        news_evidence.fetch_announcement_content("AN0000000000000002")


def test_fetched_body_is_normalized_and_cached(monkeypatch):
    """正文归一（去排版缩进、最多一个空行）+ TTL 缓存命中不重复请求。"""
    calls: list[str] = []

    def _once(art_code: str) -> dict[str, object]:
        calls.append(art_code)
        return {
            "data": {
                "notice_content": "\r\n证券简称：甲股      证券代码：600001\n\n\n\n  重要内容提示：\n\n  ●时间\n\n",
                "attach_url_web": _ATTACH_URL,
            }
        }

    monkeypatch.setattr(news_evidence, "_ann_content_http_get", _once)
    first = news_evidence.fetch_announcement_content("AN0000000000000003")
    assert first.content == "证券简称：甲股 证券代码：600001\n\n重要内容提示：\n\n●时间"
    assert first.attach_url == _ATTACH_URL
    again = news_evidence.fetch_announcement_content("AN0000000000000003")
    assert again == first
    assert calls == ["AN0000000000000003"], "TTL 内二次取正文必须走缓存"


def test_batch_fetch_skips_failures_without_failing_the_batch(monkeypatch):
    """单条正文取不到只跳过该条——整批公告不因某一条失败而一起降级。"""
    items = [_announcement("AN0000000000000011"), _announcement("AN0000000000000012")]

    def _half(art_code: str) -> dict[str, object]:
        if art_code.endswith("11"):
            raise news_evidence.NoticeError("模拟该条正文不可用")
        return {"data": {"notice_content": "第二条正文"}}

    monkeypatch.setattr(news_evidence, "_ann_content_http_get", _half)
    bodies = news_evidence.fetch_announcement_bodies(items)
    assert list(bodies) == ["AN0000000000000012"]


# ---------------------------------------------------------------------------
# 2. 落库口径：取到正文 / 取不到正文
# ---------------------------------------------------------------------------

def _pull_announcements(test_client, headers, monkeypatch, *, with_body: bool) -> dict:
    """打桩两个公告接口后拉一次公告证据。

    这里直接打 `news_evidence` 的 HTTP 层（而不是 app 层的 `fetch_cn_announcements`），
    为的是让**真实的**取数→归一→哈希→落库链路全部跑到——升级出口只有在真实链路里才成立。
    """
    monkeypatch.setattr(news_evidence, "_ann_http_get", lambda _params: {
        "data": {"list": [{
            "title": _ANN_TITLE, "notice_date": "2026-09-02 09:00:00",
            "art_code": _ART_CODE, "columns_name": "回购",
        }]}
    })
    if with_body:
        monkeypatch.setattr(
            news_evidence, "_ann_content_http_get",
            lambda _code: {"data": {"notice_content": _BODY_TEXT, "attach_url_web": _ATTACH_URL}},
        )
    else:
        def _offline(_code: str) -> dict[str, object]:
            raise news_evidence.NoticeError("模拟正文接口不可用")

        monkeypatch.setattr(news_evidence, "_ann_content_http_get", _offline)
    return test_client.get("/evidence/announcements/600001", headers=headers).json()


def test_evidence_carries_full_text_when_body_is_available(client, monkeypatch):
    test_client, headers = client
    body = _pull_announcements(test_client, headers, monkeypatch, with_body=True)
    assert body["available"] is True and len(body["entries"]) == 1
    entry = body["entries"][0]
    assert _BODY_TEXT in entry["summary"], "正文全文必须落进证据 summary"
    assert entry["source_dataset_version"] == "np-anotice-stock+content"
    locator = json.loads(entry["raw_locator"])
    assert locator["body_available"] is True
    assert locator["attach_url"] == _ATTACH_URL
    assert locator["content_api"] == "https://np-cnotice-stock.eastmoney.com/api/content/ann"
    assert any("正文" in item for item in entry["limitations"])
    assert any("非摘要" in item for item in body["limitations"])


def test_evidence_stays_title_only_when_body_fetch_fails(client, monkeypatch):
    """正文取不到 → 标题口径照常入账，但**不许**声称有正文（不编造）。"""
    test_client, headers = client
    body = _pull_announcements(test_client, headers, monkeypatch, with_body=False)
    assert body["available"] is True and len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["summary"] == f"{_ANN_TITLE}・回购"
    assert entry["source_dataset_version"] == "np-anotice-stock"
    locator = json.loads(entry["raw_locator"])
    assert locator["body_available"] is False
    assert "attach_url" not in locator
    assert any("正文本次未取到" in item for item in entry["limitations"])
    assert any("1 条尝试、0 条成功" in item for item in body["limitations"])


def test_title_only_record_is_upgraded_in_place_without_duplicating(client, monkeypatch):
    """★ 升级出口：先入账仅标题 → 再拉到正文时**原地升级**，不产生第二条证据。

    身份哈希是「(标的, 标题, 公告日期)」（正文不参与，保证换号重发幂等），所以没有这个
    出口的话，老用户的账本会永久停在「仅标题」，与报告链路已经取到的全文不一致。
    """
    test_client, headers = client
    first = _pull_announcements(test_client, headers, monkeypatch, with_body=False)
    assert len(first["entries"]) == 1
    original = first["entries"][0]

    # 第一次正文取数失败会写入 90s 失败冷却（与列表接口同口径，刻意不立刻重试打源站）。
    # 「稍后重试」= 冷却过后再拉，故这里显式清掉冷却，模拟用户过一会儿重新拉取。
    news_evidence._content_fail_cache.clear()
    second = _pull_announcements(test_client, headers, monkeypatch, with_body=True)
    assert len(second["entries"]) == 1, "升级不产生新记录"
    upgraded = second["entries"][0]
    assert upgraded["evidence_id"] == original["evidence_id"], "是原地更新而不是新插入"
    assert upgraded["content_hash"] == original["content_hash"], "身份哈希不变（去重口径不受影响）"
    assert _BODY_TEXT in upgraded["summary"]

    # 账本里该公告只有一条，且已经是含正文的版本。
    ledger = test_client.get("/evidence", headers=headers).json()
    rows = [item for item in ledger if item["content_hash"] == original["content_hash"]]
    assert len(rows) == 1
    assert _BODY_TEXT in rows[0]["summary"]

    # 再拉一次：已是含正文版本 → 零新增、零改写（幂等）。
    third = _pull_announcements(test_client, headers, monkeypatch, with_body=True)
    assert third["entries"] == []


# ---------------------------------------------------------------------------
# 3. 报告链路：A 级来源 S3A 的产生与缺席
# ---------------------------------------------------------------------------

def _stub_announcements(monkeypatch, *, with_body: bool) -> None:
    import investment_steward_core.api.app as app_module

    monkeypatch.setattr(app_module, "fetch_cn_announcements", lambda symbol, limit=20: [_announcement()])
    if with_body:
        monkeypatch.setattr(
            app_module, "fetch_announcement_bodies",
            lambda items, limit=3: {
                _ART_CODE: news_evidence.AnnouncementBody(
                    art_code=_ART_CODE, content=_BODY_TEXT, attach_url=_ATTACH_URL
                )
            },
        )
    else:
        monkeypatch.setattr(app_module, "fetch_announcement_bodies", lambda items, limit=3: {})


def test_report_registers_grade_a_source_when_body_available(client, tmp_path, monkeypatch):
    """★ D1 验收本体：公告正文进入证据包 → A 级首次非空，且链接/口径都到位。"""
    _CannedModel(monkeypatch, [json.dumps(_good_payload(), ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    _stub_announcements(monkeypatch, with_body=True)

    body = run_stock_report(test_client, headers, **{"symbol": "600001"})
    assert body["ok"] is True, body
    assert "S3A" in body["source_keys"]
    assert "S3A" in body["available_citations"]
    assert body["evidence_quality"]["version"] == "v2"
    assert body["evidence_quality"]["tiers"]["A"] == ["S3A"]
    assert body["evidence_quality"]["tiers"]["B"].count("S3") == 1, "标题口径仍是 B 级"
    assert "S3A" in body["evidence_quality"]["note"]
    assert "转载" in body["evidence_quality"]["note"]
    # 来源元数据/日期/链接都按同一份取数结果登记（R2 的链接列才有值可填）
    assert body["source_asof"]["S3A"] == "2026-09-02"
    assert body["source_meta"]["S3A"]["provider"] == "东方财富"
    assert body["source_links"]["S3A"] == _ATTACH_URL
    assert body["source_errors"].get("announcement_body") is None, "全部取到时不该报降级"

    # 来源目录（可回查）里 S3A 的链接与口径限制都取到了真实值。
    detail = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()
    catalog = {row["source_id"]: row for row in detail["item"]["source_catalog"]}
    assert catalog["S3A"]["url"] == _ATTACH_URL
    assert catalog["S3A"]["quality"] == "A"
    assert "转载" in catalog["S3A"]["scope_note"]
    assert catalog["S3"]["quality"] == "B"


def test_report_has_no_grade_a_and_says_why_when_body_unavailable(client, tmp_path, monkeypatch):
    """取不到正文 → **不建 S3A**（不拿标题冒充全文），且 note 说明本次为何没有 A 级。"""
    _CannedModel(monkeypatch, [json.dumps(_good_payload(), ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    _stub_announcements(monkeypatch, with_body=False)

    body = run_stock_report(test_client, headers, **{"symbol": "600001"})
    assert body["ok"] is True, body
    assert "S3A" not in body["source_keys"], "没取到正文就不该有 S3A 这个来源"
    assert body["evidence_quality"]["tiers"]["A"] == []
    assert "S3A" not in body["source_links"]
    assert body["evidence_quality"]["note"].count("未取到公告正文全文") == 1
    assert body["evidence_quality"]["note"].count("S3") >= 1
    # 部分失败要如实写进 source_errors（本用例是「1 条尝试、0 条成功」）
    assert "1 条尝试、0 条成功" in body["source_errors"]["announcement_body"]


def test_body_segment_marks_truncation_explicitly(monkeypatch):
    """进提示词的正文按预算截断，但**必须显式标注**——读者要知道自己看到的是节选。"""
    import investment_steward_core.api.app as app_module

    long_body = news_evidence.AnnouncementBody(
        art_code=_ART_CODE, content="乙" * (app_module.ANNOUNCEMENT_BODY_CHARS + 200), attach_url=None
    )
    item = _announcement()
    segment, used, attach = app_module._announcement_body_segment(
        [item], {_ART_CODE: long_body}
    )
    assert used == [_ART_CODE] and attach == item.url
    assert "已按预算截断" in segment
    assert segment.count("乙") == app_module.ANNOUNCEMENT_BODY_CHARS


def test_source_profile_s3a_is_grade_a_with_same_coverage_as_s3():
    """S3A 与 S3 的差别只在「标题 vs 全文」，覆盖面完全一致（差别在可信度）。"""
    profile = report_quality.source_profile("S3A")
    assert profile["quality"] == report_quality.QUALITY_A
    assert profile["kind"] == "announcement"
    assert set(profile["coverage"]) == set(report_quality.source_profile("S3")["coverage"])
    assert profile["coverage"], "覆盖字段不能为空——它必须真的能支撑字段"
