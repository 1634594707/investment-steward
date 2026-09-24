"""v31 修复流水线（§2.4）+ 固定证据快照对照（§八.7）端点级测试。

- 修复策略按失败类型定向：缺摘要只写摘要 / 缺一节只补该节 / 缺两节以上重跑全文；
  修复后重跑同一闸门；失败回滚原稿；每次修复升 report_revision 并留痕。
- §八.7 对照：同一批固定证据快照（盈利/亏损/周期/缺数据）× 各篇幅模式，
  比较完整性判定、模式贯穿、引用一致性，以及响应与落库 payload 的一致性
  （页面与导出同源，数字不在闸门/落库/回看链路中失真）。模型输出全部打桩，绝不联网。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用


def _file_client(tmp_path, monkeypatch):
    """强制文件凭据后端的 client（与 test_stock_research_tools 同口径）。"""
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_active_profile(test_client, headers) -> None:
    test_client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    assert test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers).status_code == 200


def _synth_bars(count: int = 60) -> list[dict[str, object]]:
    rows = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99,
            "close": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "volume": 1_000_000 + index * 1000,
        })
    return rows


def _mock_sources(monkeypatch, *, financial_rows: list[SimpleNamespace] | None = None, fail_valuation: bool = False) -> None:
    """固定证据快照：K 线/新闻/公告/财务/估值全部打桩（绝不联网）。"""
    import investment_steward_core.api.app as app_module
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider")
    )
    monkeypatch.setattr(
        app_module, "fetch_cn_news",
        lambda symbol, limit=20: [SimpleNamespace(published_raw="2026-09-01", title="甲股中标大单", content="内容正文")],
    )
    monkeypatch.setattr(
        app_module, "fetch_cn_announcements",
        lambda symbol, limit=20: [SimpleNamespace(notice_date_raw="2026-09-02", title="关于回购的公告", ann_type="回购", url="http://x", art_code="A1")],
    )
    monkeypatch.setattr(app_module, "fetch_financials", lambda symbol, limit_per_type=8: financial_rows or _profit_rows())
    if fail_valuation:
        def _raise(*a, **k):
            raise RuntimeError("估值源不可用")
        monkeypatch.setattr(app_module, "fetch_valuation", _raise)
    else:
        monkeypatch.setattr(
            app_module, "fetch_valuation",
            lambda symbol, **kwargs: ValuationSnapshot(
                symbol=symbol, name="甲股", board_code="016165", board_name="测试行业",
                trade_date="2026-09-10", close_price=10.0, pe_ttm=19.73, pe_static=19.52,
                pb_mrq=6.39, ps_ttm=9.27, peg=1.0, pcf_ocf_ttm=13.49,
                total_market_cap=1e11, float_market_cap=1e11, total_shares=1e10,
                percentiles={"pe_ttm": 6.7, "pb_mrq": 4.2, "ps_ttm": 3.6},
                percentile_window_bars=1250, percentile_window_from="2021-07-19",
            ),
        )


def _profit_rows() -> list[SimpleNamespace]:
    """盈利快照：营收/净利同比增长，现金流为正。"""
    return [
        SimpleNamespace(statement_type="income", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"revenue": "1000000000", "net_profit": "120000000"}, identity="h-income-2606"),
        SimpleNamespace(statement_type="income", period_end="2025-06-30", published_raw="2025-08-30",
                        items={"revenue": "900000000", "net_profit": "100000000"}, identity="h-income-2506"),
        SimpleNamespace(statement_type="cash_flow", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"operating_cash_flow": "150000000"}, identity="h-cash-2606"),
    ]


def _loss_rows() -> list[SimpleNamespace]:
    """亏损快照：净利为负且亏损扩大，经营现金流为负。"""
    return [
        SimpleNamespace(statement_type="income", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"revenue": "800000000", "net_profit": "-90000000"}, identity="h-income-2606"),
        SimpleNamespace(statement_type="income", period_end="2025-06-30", published_raw="2025-08-30",
                        items={"revenue": "850000000", "net_profit": "-30000000"}, identity="h-income-2506"),
        SimpleNamespace(statement_type="cash_flow", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"operating_cash_flow": "-120000000"}, identity="h-cash-2606"),
    ]


def _cyclical_rows() -> list[SimpleNamespace]:
    """周期快照：单季营收大起大落。"""
    return [
        SimpleNamespace(statement_type="income", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"revenue": "1500000000", "net_profit": "200000000"}, identity="h-income-2606"),
        SimpleNamespace(statement_type="income", period_end="2025-06-30", published_raw="2025-08-30",
                        items={"revenue": "600000000", "net_profit": "-50000000"}, identity="h-income-2506"),
        SimpleNamespace(statement_type="cash_flow", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"operating_cash_flow": "210000000"}, identity="h-cash-2606"),
    ]


class _CannedModel:
    """FIFO 打桩：按调用次序返回预设响应，并记录调用次数。"""

    def __init__(self, monkeypatch, payloads: list[str]):
        from investment_steward_core import model_client as mc
        self.calls: list[str] = []
        self._payloads = list(payloads)
        monkeypatch.setattr(mc, "_post_json", self)

    def __call__(self, *a, **k):
        self.calls.append("")
        payload = self._payloads.pop(0) if self._payloads else "{}"
        return {"choices": [{"message": {"content": payload}}]}

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _good_report_text() -> str:
    """完整正文：四小节各有实质内容，数字与固定证据快照逐位一致（营收 10.00 亿）。"""
    return (
        "### 技术面\n"
        "股价沿 MA20 缓涨，60 日区间涨幅稳健，量能温和放大，趋势结构完好，关键支撑 6.10。[S1]\n\n"
        "### 消息面\n"
        "已落地事实：公司公告回购 [S3]；市场传闻与情绪：中标大单新闻带动关注度 [S2]。\n\n"
        "### 基本面\n"
        "营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正，利润质量健康；"
        "PE(TTM) 19.73，近5年 6.7% 分位，低于同业中位，估值偏便宜。[S4][S5]\n\n"
        "### 综合判断\n"
        "多空矛盾在于趋势向好但估值分位已低、赔率有限；综合判断偏多，核心跟踪回购落地节奏。\n"
    )


def _good_payload() -> dict:
    return {
        "title": "甲股：缓涨趋势配合基本面稳健",
        "executive_summary": (
            "方向性结论偏多：技术面缓涨趋势完好。最强基本面依据是营收净利双增且现金流为正。"
            "最重要技术面依据是量价配合与 MA20 支撑有效。最大反证是估值分位虽低但赔率有限。"
        ),
        "report": _good_report_text(),
        "citations": ["S1", "S2", "S3", "S4", "S5"],
        "limitations": ["公开来源摘要口径", "行情数据为日线粒度", "同业样本口径以数据商为准"],
        "confidence": "medium",
        "conclusion": {"statement": "趋势偏多但赔率有限", "direction": "偏多", "core_conflict": "趋势与赔率", "confidence": "medium"},
        "claims": [{
            "claim_id": "C1", "text": "营收 10.00 亿同比增长",
            "sources": ["S4"], "requires": ["revenue"], "importance": "high",
            "confidence": "high", "missing": [],
        }],
    }


def _tech_only_payload() -> dict:
    """坏样本：只有技术面（方案原始 501 字案例形态），正文带 [S1] 行内引用。"""
    return {
        "title": "甲股：技术面速览",
        "executive_summary": "技术面走弱。",
        "report": "### 技术面\n" + "K线走弱，跌破支撑 [S1]，量能萎缩，短线观望为宜，等待企稳信号再确认方向。" * 10,
        "citations": ["S1"],
        "limitations": ["口径限制"],
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# §2.4 修复流水线
# ---------------------------------------------------------------------------

def _setup(client, tmp_path, monkeypatch, financial_rows=None):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch, financial_rows=financial_rows)
    return test_client, headers


def test_repair_missing_section_single(client, tmp_path, monkeypatch):
    """缺一节 → 只重新生成该节；修复后 revision=2 且留痕，状态转好。"""
    broken = _good_payload()
    broken["report"] = broken["report"].replace(
        "### 基本面\n营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正，利润质量健康；"
        "PE(TTM) 19.73，近5年 6.7% 分位，低于同业中位，估值偏便宜。[S4][S5]\n\n",
        "### 基本面\n见上文。\n\n",
    )
    broken["citations"] = ["S1", "S2", "S3"]
    repaired_section = {
        "section": "基本面",
        "content": "### 基本面\n营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正；"
                   "PE(TTM) 19.73，近5年 6.7% 分位，估值偏便宜。[S4][S5]",
    }
    canned = _CannedModel(monkeypatch, [
        json.dumps(broken, ensure_ascii=False),
        json.dumps(repaired_section, ensure_ascii=False),
    ])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert canned.call_count == 2  # 初稿 + 一次小节修复（无反方检查）
    assert body["repair"]["attempted"] is True
    assert body["repair"]["strategy"] == "section_only"
    assert body["repair"]["repaired"] is True
    assert body["report_revision"] == 2
    assert body["revision_history"][0]["strategy"] == "section_only"
    assert body["revision_history"][0]["result"] in ("complete", "needs_review")
    assert "营收 10.00 亿同比增长" in body["report"]
    assert "### 基本面" in body["report"]
    assert "S4" in body["citations"] and "S5" in body["citations"]  # 合并后引用清单纳入新小节真实引用
    assert body["quality_status"] in ("complete", "needs_review")
    assert body["is_draft"] is False


def test_repair_full_rerun_for_two_missing(client, tmp_path, monkeypatch):
    """缺两节以上 → 重跑完整 draft；重跑稿直接过闸门。"""
    broken = _tech_only_payload()
    canned = _CannedModel(monkeypatch, [
        json.dumps(broken, ensure_ascii=False),
        json.dumps(_good_payload(), ensure_ascii=False),
    ])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert canned.call_count == 2
    assert body["repair"]["strategy"] == "full_rerun"
    assert body["repair"]["repaired"] is True
    assert body["report_revision"] == 2
    assert body["quality_status"] in ("complete", "needs_review")
    assert body["report"] == _good_report_text()


def test_repair_summary_only_keeps_report_text(client, tmp_path, monkeypatch):
    """缺摘要 → 只生成摘要，正文一个字都不动。"""
    payload = _good_payload()
    payload["executive_summary"] = ""
    summary_text = "结论偏多：缓涨趋势完好。基本面依据：营收净利双增现金流为正。技术面依据：量价配合支撑有效。反证：估值赔率有限。"
    canned = _CannedModel(monkeypatch, [
        json.dumps(payload, ensure_ascii=False),
        summary_text,
    ])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert canned.call_count == 2
    assert body["repair"]["strategy"] == "summary_only"
    assert body["report"] == _good_report_text()
    assert body["executive_summary"] == summary_text
    assert body["report_revision"] == 2


def test_repair_failure_keeps_draft(client, tmp_path, monkeypatch):
    """修复输出不可解析 → 保留原稿，状态仍 incomplete，revision 不升，留痕如实。"""
    canned = _CannedModel(monkeypatch, [
        json.dumps(_tech_only_payload(), ensure_ascii=False),
        "这不是 JSON",
    ])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert canned.call_count == 2  # 初稿 + 一次修复尝试（输出不可解析被如实留痕）
    assert body["ok"] is True
    assert body["repair"]["attempted"] is True
    assert body["repair"]["repaired"] is False
    assert body["report_revision"] == 1
    assert body["revision_history"] == []
    assert body["quality_status"] == "incomplete"
    assert body["is_draft"] is True


def test_repair_rollback_on_fake_citations(client, tmp_path, monkeypatch):
    """重跑稿引用全部失真 → 回滚原稿（无引用不发布），不引入虚构引用。"""
    fake = _good_payload()
    fake["citations"] = ["S9"]
    fake["report"] = fake["report"].replace("[S1]", "[S9]")
    canned = _CannedModel(monkeypatch, [
        json.dumps(_tech_only_payload(), ensure_ascii=False),
        json.dumps(fake, ensure_ascii=False),
    ])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert canned.call_count == 2  # 初稿 + 一次修复尝试
    assert body["ok"] is True
    assert body["report_revision"] == 1
    assert body["repair"]["repaired"] is False
    assert "回滚" in body["repair"]["detail"]
    assert body["report"] != fake["report"]  # 保留的是原稿
    assert set(body["citations"]) == {"S1"}


def test_auto_repair_disabled(client, tmp_path, monkeypatch):
    """with_auto_repair=false → 不发起修复调用（成本敏感场景）。"""
    canned = _CannedModel(monkeypatch, [json.dumps(_tech_only_payload(), ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False, "with_auto_repair": False},
        headers=headers,
    ).json()
    assert canned.call_count == 1
    assert body["repair"]["attempted"] is False
    assert body["quality_status"] == "incomplete"
    assert body["is_draft"] is True


# ---------------------------------------------------------------------------
# §八.7 固定证据快照对照（同一批快照 × 各篇幅模式）
# ---------------------------------------------------------------------------

_SNAPSHOTS = {
    "profit": _profit_rows(),
    "loss": _loss_rows(),
    "cyclical": _cyclical_rows(),
}
_MODES = ["quick", "standard", "deep"]


@pytest.mark.parametrize("snapshot_name", sorted(_SNAPSHOTS))
@pytest.mark.parametrize("mode", _MODES)
def test_fixed_snapshot_good_report_across_modes(client, tmp_path, monkeypatch, snapshot_name, mode):
    """好样本 × 三模式 × 三类财务快照：完整、模式贯穿、数字不在此链路失真、落库与响应一致。"""
    canned = _CannedModel(monkeypatch, [json.dumps(_good_payload(), ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch, financial_rows=_SNAPSHOTS[snapshot_name])
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "mode": mode, "with_counter_check": False},
        headers=headers,
    ).json()
    assert canned.call_count == 1  # 完整报告不触发任何修复调用
    assert body["ok"] is True
    assert body["quality_status"] in ("complete", "needs_review")
    assert body["missing_sections"] == []
    assert body["report_mode"] == mode
    assert body["generation_trace"]["report_mode"] == mode
    assert body["generation_trace"]["prompt_version"]
    assert body["report"] == _good_report_text()  # 闸门不改写正文 → 数字不失真
    assert body["is_draft"] is False
    # 落库 payload 与响应一致（页面回看与导出同源）
    record = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()["item"]
    assert record["report"] == body["report"]
    assert record["quality_status"] == body["quality_status"]
    assert record["report_mode"] == mode
    assert record["is_draft"] is False


@pytest.mark.parametrize("snapshot_name", sorted(_SNAPSHOTS))
def test_fixed_snapshot_bad_report_is_draft_in_response_and_storage(client, tmp_path, monkeypatch, snapshot_name):
    """坏样本（只有技术面）：三快照一致判 incomplete，修复关不掉闸门，草稿落库且不混入正式。"""
    canned = _CannedModel(monkeypatch, [json.dumps(_tech_only_payload(), ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch, financial_rows=_SNAPSHOTS[snapshot_name])
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_auto_repair": False, "with_counter_check": False},
        headers=headers,
    ).json()
    assert canned.call_count == 1
    assert body["quality_status"] == "incomplete"
    assert body["is_draft"] is True
    assert {b["code"] for b in body["quality_blockers"]} == {"missing_section"}
    assert body["citation_consistency"]["text_refs"]
    record = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()["item"]
    assert record["is_draft"] is True
    assert record["missing_sections"] == body["missing_sections"]


def test_fixed_snapshot_missing_data_target_no_model_call(client, tmp_path, monkeypatch):
    """缺数据标的：五类来源全失败 → 不调模型不生成（不编造），对照基准之一。"""
    import investment_steward_core.api.app as app_module
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)

    def _raise(*a, **k):
        raise RuntimeError("网络不可用")

    for name in ("fetch_cn_kline", "fetch_cn_news", "fetch_cn_announcements", "fetch_financials", "fetch_valuation"):
        monkeypatch.setattr(app_module, name, _raise)
    canned = _CannedModel(monkeypatch, [])
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001"},
        headers=headers,
    ).json()
    assert canned.call_count == 0
    assert body["ok"] is False and body["stage"] == "evidence_unavailable"


def test_fixed_snapshot_citation_inconsistency_flagged(client, tmp_path, monkeypatch):
    """引用一致性对照：清单漏登记正文引用（S5 可用）→ 告警而非虚构引用。"""
    payload = _good_payload()
    payload["citations"] = ["S1", "S2"]
    canned = _CannedModel(monkeypatch, [json.dumps(payload, ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert canned.call_count == 1  # 告警不触发修复（修复只对 incomplete）
    assert body["ok"] is True
    assert set(body["citation_consistency"]["undeclared_used"]) >= {"S4", "S5"}
    assert any("引用清单不一致" in warning for warning in body["quality_warnings"])
