"""v35 方向研判风格主题与估值证据层测试（2026-09-14 路线图 D01）。

覆盖路线图 A/B/C 三组新增能力的打桩全链路（绝不真实联网）：

- A01 主题类型判定（产业/风格/复合/通用）与 generation_trace 留痕；
- A02 风格词 → 数据口径声明（可单独断言），无数据风格词进缺口不产生空证据；
- A03 复合主题双解析：横截面只含产业限定板块 + 板块行情快照并存；
- B01/B02 横截面聚合与低估度榜单（中位数只用正数样本、亏损股排除、排序口径）；
- B03 代表股分位附录与「市值前列代表股代理」口径声明；
- B04 预算封顶（板块数 × 每股数）；
- B05 候选池逐股估值回链（失败保留 + 标注缺失，不静默丢字段）；
- C01 提示词风格分支（口径声明前置 + 估值引用规则）；
- C02 估值断言引用闸门（无引用告警 / 带引用不误伤 / 多处违规降 needs_review）；
- C03 风格主题四类缺口各带补证动作，且不与「未命中行业板块清单」重复堆叠。

约定：行情/估值取数一律 monkeypatch；需要写模型凭据的用例走文件凭据后端
（monkeypatch `api_app.resolve_store`），不污染系统凭据管理器。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from investment_steward_core import direction_research as direction_engine
from investment_steward_core import valuation_evidence as ve

# ---------------------------------------------------------------------------
# 打桩数据
# ---------------------------------------------------------------------------


def _row(symbol, name, board_code, board_name, cap, pe, pb):
    return ve.CrossSectionRow(
        symbol=symbol,
        name=name,
        board_code=board_code,
        board_name=board_name,
        total_market_cap=cap,
        pe_ttm=pe,
        pb_mrq=pb,
        ps_ttm=pe / 3.0 if pe and pe > 0 else None,
    )


# 东财口径横截面打桩行：两个板块，含亏损股（PE ≤ 0）用于验证排除逻辑。
# 银行板块 PB 中位数明显低于电子板块 → 榜单第一名应为银行。
_CROSS_SECTION_ROWS = [
    # —— 银行板块（BOARD_CODE=BK0475）——
    # 市值前列三只，PB 低（0.45/0.55/0.65）；给足 6 只正 PE 样本，使银行能真正上榜。
    _row("601398", "工商银行", "BK0475", "银行", 2.5e12, 5.0, 0.45),
    _row("601939", "建设银行", "BK0475", "银行", 1.8e12, 5.2, 0.55),
    _row("601288", "农业银行", "BK0475", "银行", 1.6e12, 5.5, 0.65),
    _row("601988", "中国银行", "BK0475", "银行", 1.4e12, 5.8, 0.50),
    _row("600036", "招商银行", "BK0475", "银行", 1.0e12, 6.0, 0.85),
    _row("601166", "兴业银行", "BK0475", "银行", 4.0e11, 4.8, 0.52),
    # 亏损股：PE 为负，应被 PE 可比样本排除，PB 仍参与
    _row("600000", "浦发银行", "BK0475", "银行", 3.0e11, -3.0, 0.48),
    # —— 电子器件板块（BOARD_CODE=BK0448）——
    _row("002475", "立讯精密", "BK0448", "电子器件", 3.0e11, 25.0, 3.20),
    _row("300136", "信维通信", "BK0448", "电子器件", 2.0e11, 30.0, 2.80),
    _row("002241", "歌尔股份", "BK0448", "电子器件", 1.5e11, 28.0, 2.50),
    _row("600745", "闻泰科技", "BK0448", "电子器件", 1.2e11, 22.0, 2.10),
    _row("002456", "欧菲光", "BK0448", "电子器件", 8.0e10, 35.0, 2.00),
    _row("300433", "蓝思科技", "BK0448", "电子器件", 9.0e10, 26.0, 2.30),
]


# 新浪行业板块清单（v34 板块行情层），用于产业主题与复合主题「低估的银行」的产业限定。
_SECTOR_ROWS = [
    {
        "code": "new_jtys",
        "name": "交通运输",
        "member_count": 87,
        "change_pct": -0.04,
        "amount": 2.05887e10,
        "leader_symbol": "601872",
        "leader_name": "招商轮船",
        "leader_change_pct": 0.0,
    },
    {
        "code": "new_jrhy",
        "name": "金融行业",
        "member_count": 51,
        "change_pct": -0.30,
        "amount": 3.1e10,
        "leader_symbol": "601398",
        "leader_name": "工商银行",
        "leader_change_pct": 0.5,
    },
    {
        "code": "new_dzqj",
        "name": "电子器件",
        "member_count": 152,
        "change_pct": 0.23,
        "amount": 1.37789e11,
        "leader_symbol": "002475",
        "leader_name": "立讯精密",
        "leader_change_pct": 1.20,
    },
]

_SECTOR_MEMBERS = [
    {"symbol": "601398", "name": "工商银行", "price": 6.1, "change_pct": 0.5, "turnover": 2.0e9},
    {"symbol": "601939", "name": "建设银行", "price": 8.2, "change_pct": 0.2, "turnover": 1.5e9},
]


def _valuation_snapshot(
    symbol: str, name: str, *, board_name: str = "银行", board_code: str = "BK0475",
    pb_pct: float = 8.5, pe_pct: float = 12.3,
):
    """打桩个股估值快照（含 5 年分位与同业位次）。

    v36 A03：榜单主键改为「代表股 PB 分位中位数」，故分位（`pb_pct`/`pe_pct`）随调用可调，
    供需要区分板块排序的用例注入不同分位。
    """
    return ve.ValuationSnapshot(
        symbol=symbol,
        name=name,
        board_code=board_code,
        board_name=board_name,
        trade_date="2026-09-11",
        close_price=6.10,
        pe_ttm=5.0,
        pe_static=5.1,
        pb_mrq=0.48,
        ps_ttm=1.6,
        peg=None,
        pcf_ocf_ttm=None,
        total_market_cap=2.5e12,
        float_market_cap=1.0e12,
        total_shares=4.1e11,
        percentiles={"pe_ttm": pe_pct, "pb_mrq": pb_pct},
        percentile_window_bars=1215,
        percentile_window_from="2021-09-13",
        peers=(),
        peer_count=20,
        peer_median={"pe_ttm": 6.0, "pb_mrq": 0.55},
        peer_median_basis={"pe_ttm": 18, "pb_mrq": 18},
        peer_rank={"pe_ttm": 3, "pb_mrq": 2},
        peer_rank_basis={"pe_ttm": 18, "pb_mrq": 18},
    )


# ---------------------------------------------------------------------------
# 端点脚手架
# ---------------------------------------------------------------------------


def _direction_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_profile(test_client, headers) -> str:
    test_client.put(
        "/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers
    )
    created = test_client.post(
        "/model-profiles",
        json={
            "name": "方案A",
            "base_url": "https://api.a.com",
            "model": "model-a",
            "credential_ref": "model_api_key",
        },
        headers=headers,
    ).json()
    assert (
        test_client.post(
            f"/model-profiles/{created['profile_id']}/activate", headers=headers
        ).status_code
        == 200
    )
    return created["profile_id"]


def _sections() -> str:
    return "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件，避免空泛表述。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )


def _style_report(valuation_lines: dict | None = None) -> str:
    """风格主题打桩正文：默认无估值水位表述（不触发 C02），可注入带引用的估值句。"""
    overrides = valuation_lines or {}
    out: list[str] = []
    for name in direction_engine.REQUIRED_DIRECTION_SECTIONS:
        body = overrides.get(
            name,
            "本节按事实→推理→不确定性展开，给出可核对的数据与边界条件，避免空泛表述。",
        )
        out.append(f"## {name}\n{body}\n")
    return "".join(out)


def _payload(topic_title: str, report: str, *, pool: list[dict] | None = None) -> dict:
    return {
        "title": topic_title,
        "executive_summary": (
            "方向判断：以证据口径为准。最强证据：板块估值横截面 [E1]。"
            "最大不确定性：数据覆盖范围与样本条件。"
        ),
        "report": report,
        "core_judgments": [
            {"id": "J1", "text": "板块估值口径可由横截面核对", "kind": "industry", "confidence": "medium"},
            {"id": "J2", "text": "代表股代理口径下有可比样本", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "板块当日资金表现分化", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["估值源数据覆盖扩展"],
        "risks": ["样本数不足导致口径不可比"],
        "stock_pool": pool if pool is not None else [],
        "data_gaps": ["模型自述缺口：行业协会数据未接入"],
    }


def _stub_model(monkeypatch, payload_factory):
    from investment_steward_core import model_client as mc

    calls: list[str] = []

    def _fake(_base_url, payload, _key, _timeout):
        calls.append(json.dumps(payload, ensure_ascii=False))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(payload_factory(), ensure_ascii=False)
                    }
                }
            ]
        }

    monkeypatch.setattr(mc, "_post_json", _fake)
    return calls


def _stub_cross_section(monkeypatch, rows=None, *, fail: bool = False):
    """打桩横截面取数：`latest_valuation_trade_date` + `fetch_market_cross_section`。"""
    data = list(rows if rows is not None else _CROSS_SECTION_ROWS)
    if fail:
        def _boom(*_a, **_k):
            raise ve.ValuationError("上游断连（打桩）")

        monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
        monkeypatch.setattr(ve, "fetch_market_cross_section", _boom)
    else:
        monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
        monkeypatch.setattr(ve, "fetch_market_cross_section", lambda trade_date: list(data))
    ve.reset_cooldowns()


# ---------------------------------------------------------------------------
# A 组：主题类型判定与口径声明（纯函数）
# ---------------------------------------------------------------------------


def test_a01_classify_industry_style_composite_generic():
    """A01：四类主题判定——海运→产业、低估板块→风格、低估的银行→复合、未知→通用。"""
    assert (
        direction_engine.classify_topic("海运板块", _SECTOR_ROWS)["theme_kind"]
        == direction_engine.THEME_KIND_INDUSTRY
    )
    assert (
        direction_engine.classify_topic("低估板块", _SECTOR_ROWS)["theme_kind"]
        == direction_engine.THEME_KIND_STYLE
    )
    assert (
        direction_engine.classify_topic("低估的银行", _SECTOR_ROWS)["theme_kind"]
        == direction_engine.THEME_KIND_COMPOSITE
    )
    assert (
        direction_engine.classify_topic("某某新概念", _SECTOR_ROWS)["theme_kind"]
        == direction_engine.THEME_KIND_GENERIC
    )


def test_a02_style_calibers_declare_data_availability():
    """A02：每个风格词口径可单独断言；高股息/红利声明不做且不产生空证据。"""
    calibers = direction_engine.resolve_style_calibers(["低估", "高股息", "成长"])
    entries = {item["term"]: item for item in calibers["entries"]}
    assert entries["低估"]["status"] == direction_engine.STYLE_STATUS_AVAILABLE
    assert entries["高股息"]["status"] == direction_engine.STYLE_STATUS_UNAVAILABLE
    assert entries["成长"]["status"] == direction_engine.STYLE_STATUS_FRAMEWORK
    # 口径声明文本随证据下发（含指标与样本条件），不是藏在提示词里
    text = direction_engine.style_caliber_declaration(["低估"])
    assert "PE(TTM)/PB(MRQ)" in text and "20" in text
    # 无数据 / 仅框架风格词进缺口，可用风格词不产出缺口
    gaps = " ".join(calibers["gaps"])
    assert "高股息" in gaps and "成长" in gaps
    assert "「低估」" not in gaps
    assert calibers["available_terms"] == ["低估"]


def test_a03_composite_scope_limits_board_names():
    """A03：复合主题解析出产业限定板块名；纯风格主题无产业限定。"""
    scope = direction_engine.resolve_theme_scope("低估的银行", _SECTOR_ROWS)
    assert scope["theme_kind"] == direction_engine.THEME_KIND_COMPOSITE
    assert scope["limited"] is True
    assert "金融行业" in scope["sector_names"]
    assert scope["style_terms"] == ["低估"]

    style_scope = direction_engine.resolve_theme_scope("低估板块", _SECTOR_ROWS)
    assert style_scope["theme_kind"] == direction_engine.THEME_KIND_STYLE
    assert style_scope["limited"] is False
    assert style_scope["sector_names"] == []


# ---------------------------------------------------------------------------
# B 组：横截面聚合 / 榜单 / 代表股 / 预算（纯函数）
# ---------------------------------------------------------------------------


def test_b01_aggregate_medians_positive_sample_and_loss_exclusion():
    """B01：板块聚合——中位数只用正数样本、亏损股单列并从 PE 可比样本排除。"""
    aggregates = ve.aggregate_sector_valuations(list(_CROSS_SECTION_ROWS), trade_date="2026-09-11")
    by_code = {item.board_code: item for item in aggregates}
    bank = by_code["BK0475"]
    # 银行板块 7 只（6 正 PE + 1 亏损）
    assert bank.member_count == 7
    assert bank.loss_count == 1
    assert bank.pe_ttm_positive_count == 6
    assert bank.pb_mrq_positive_count == 7
    # PB 中位数 = sorted([0.45,0.48,0.50,0.52,0.55,0.65,0.85])[3] = 0.52
    assert abs((bank.pb_mrq_median or 0) - 0.52) < 1e-9
    # PE 中位数 = sorted([4.8,5.0,5.2,5.5,5.8,6.0]) 中间两项均值 = 5.35
    assert abs((bank.pe_ttm_median or 0) - 5.35) < 1e-9
    assert bank.comparable_count == bank.pb_mrq_positive_count


def test_b02_ranking_pb_first_then_pe_then_cap():
    """B02：榜单主键 PB 中位数升序（银行 PB 低于电子 → 银行第一），口径文本随证据下发。"""
    aggregates = ve.aggregate_sector_valuations(list(_CROSS_SECTION_ROWS), trade_date="2026-09-11")
    ranking = ve.rank_sector_valuations(aggregates)
    assert len(ranking) == 2
    assert ranking[0].aggregate.board_code == "BK0475"
    assert ranking[0].rank == 1 and ranking[1].rank == 2
    # 排序口径全文可读（含主/次键与同键规则）
    assert "PB" in ve.SECTOR_RANKING_CALIBER and "PE" in ve.SECTOR_RANKING_CALIBER
    text = ve.describe_sector_ranking_entry(ranking[0], total_boards=len(aggregates))
    assert "银行" in text and "中位数" in text


def test_b03_representative_selection_prefers_large_cap_and_proxy_disclaimer():
    """B03：代表股按市值前列选取；板块分位声明为「代表股代理，非全成分历史分位」。"""
    picked = ve.select_representative_stocks(list(_CROSS_SECTION_ROWS), "BK0475", limit=3)
    assert [item.symbol for item in picked] == ["601398", "601939", "601288"]
    assert "代理" in ve.SECTOR_PROXY_DISCLAIMER
    assert "非全成分历史分位" in ve.SECTOR_PROXY_DISCLAIMER


def test_b03_appendix_marks_incomparable_when_percentile_missing(monkeypatch):
    """B03：分位置空的股如实标注不可比，不猜测分位值。"""
    def _fake_fetch(symbol, *, history_years=5, peer_limit=20):
        snap = _valuation_snapshot(symbol, f"股{symbol}")
        # 让 PB 分位缺失（正数样本不足场景）
        return ve.ValuationSnapshot(
            **{**snap.__dict__, "percentiles": {"pe_ttm": None, "pb_mrq": None}}
        )

    appendix, failures = ve.fetch_board_percentile_appendix(
        list(_CROSS_SECTION_ROWS), "BK0475", "银行", limit=3, fetch=_fake_fetch
    )
    assert failures == []
    assert appendix.representative_count == 3
    assert appendix.comparable_count == 0
    assert len(appendix.incomparable_symbols) == 3
    assert appendix.disclaimer == ve.SECTOR_PROXY_DISCLAIMER
    # 无样本时中位数如实为空，不填 0
    assert appendix.pb_percentile_median is None


# ---------------------------------------------------------------------------
# C 组：引用闸门与缺口（纯函数）
# ---------------------------------------------------------------------------


def test_c02_unsupported_valuation_claim_triggers_warning():
    """C02：无引用的估值水位表述 → 告警 + 进 missing_information。"""
    report = _style_report(
        {"需求供给与价格": "行业估值处于历史中低位，具备修复空间，基本面稳健。"}
    )
    result = direction_engine.validate_direction(
        {**_payload("低估板块", report), "report": report},
        mode="standard",
        research_mode="evidence",
        evidence_ids={"E1"},
    )
    assert result["unsupported_valuation_claims"]
    assert any("估值水位表述" in w for w in result["quality_warnings"])
    assert result["missing_information"]


def test_c02_supported_claim_not_flagged_and_threshold_downgrade():
    """C02：带真实 [E1] 引用不误伤；≥3 处无引用降 needs_review。"""
    ok_report = _style_report(
        {"需求供给与价格": "板块 PB 中位数 0.52、PB 分位 8.5%，处于历史低位 [E1]。"}
    )
    ok = direction_engine.validate_direction(
        {**_payload("低估板块", ok_report), "report": ok_report},
        mode="standard",
        research_mode="evidence",
        evidence_ids={"E1"},
    )
    assert ok["unsupported_valuation_claims"] == []
    assert not any("估值水位表述" in w for w in ok["quality_warnings"])

    bad_report = _style_report({
        "需求供给与价格": "行业估值处于低位，破净个股较多，需逐项核对净资产口径。",
        "景气阶段": "估值修复可期，当前分位偏低，需观察下游订单能否兑现。",
        "竞争与替代": "板块处于高位，相对海外同业溢价明显，需核对可比口径。",
    })
    bad = direction_engine.validate_direction(
        {**_payload("低估板块", bad_report), "report": bad_report},
        mode="standard",
        research_mode="evidence",
        evidence_ids={"E1"},
    )
    assert len(bad["unsupported_valuation_claims"]) >= direction_engine.VALUATION_CLAIM_WARN_THRESHOLD
    assert bad["quality_status"] == "needs_review"


def test_c03_four_gap_classes_each_with_action_and_no_legacy_duplication():
    """C03：四类缺口各有独立条目与补证动作；不与「未命中行业板块清单」重复堆叠。"""
    entries = direction_engine.style_gap_entries(
        style_terms=["高股息", "低估"],
        cross_section_ok=False,
        sector_percentile_failures=["银行"],
        pool_valuation_failures=["工商银行(601398)"],
    )
    codes = [item["code"] for item in entries]
    assert codes == [
        direction_engine.GAP_CODE_STYLE_UNDATA,
        direction_engine.GAP_CODE_CROSS_SECTION_FAILED,
        direction_engine.GAP_CODE_SECTOR_PERCENTILE_UNAVAILABLE,
        direction_engine.GAP_CODE_POOL_VALUATION_MISSING,
    ]
    for item in entries:
        assert item["label"]
        # 每类缺口都有补证动作（重跑 / 扩展 / 降级说明）
        assert any(
            marker in item["text"]
            for marker in ("恢复后重跑", "后扩展", "本期不做", "升级为量化口径", "仅保留估值中位数")
        ), item["text"]
        assert direction_engine.LEGACY_GAP_SECTOR_UNMATCHED_MARKER not in item["text"]

    legacy = "主题「低估板块」未命中行业板块清单（风格/概念类主题无板块行情证据，缺口如实声明）"
    merged = direction_engine.merge_data_gaps(
        [legacy, legacy], [item["text"] for item in entries] + [legacy]
    )
    assert merged.count(legacy) == 1
    assert len(merged) == 1 + len(entries)


# ---------------------------------------------------------------------------
# 端点：风格主题全链路
# ---------------------------------------------------------------------------


def _install_plugins(test_client, headers):
    assert (
        test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code
        == 200
    )


def test_d01_style_topic_end_to_end(tmp_path, monkeypatch):
    """D01：风格主题「低估板块」→ evidence 模式 + sector_valuation 证据 + 候选池回链。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    pool = [
        {
            "symbol_raw": "601398",
            "name": "工商银行",
            "sector": "银行",
            "business_link": "国有大行存贷业务",
            "profit_path": "净息差与资产质量",
            "counter_evidence": "息差收窄压力",
        }
    ]
    calls = _stub_model(
        monkeypatch,
        lambda: _payload(
            "低估板块：估值水位与证据口径",
            _style_report({"需求供给与价格": "板块 PB 中位数 0.52、可比样本 7 只 [E1]。"}),
            pool=pool,
        ),
    )
    _install_plugins(test_client, headers)
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    monkeypatch.setattr(
        app_module, "fetch_cn_sector_members", lambda code, top=60: _SECTOR_MEMBERS[:top]
    )
    _stub_cross_section(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "fetch_valuation",
        lambda symbol, **kw: _valuation_snapshot(symbol, "工商银行"),
    )

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "低估板块", "question": "哪些板块估值处于低位", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True
    assert body["research_mode"] == "evidence"
    assert body["theme_kind"] == direction_engine.THEME_KIND_STYLE
    assert body["theme_kind_label"] == "风格主题"
    assert body["generation_trace"]["theme_kind"] == direction_engine.THEME_KIND_STYLE
    assert body["generation_trace"]["style_terms"] == ["低估"]

    # 证据含至少一个板块的估值中位数与样本数
    valuation_items = [
        item for item in body["evidence_snapshot"] if item["source_type"] == "sector_valuation"
    ]
    assert len(valuation_items) >= 1
    top = valuation_items[0]
    # v36 A03：榜单主键 = 代表股 PB 分位中位数（打桩两个板块分位同为 8.5%，
    # 由 tiebreak 决定先后；此处只断言「榜首是拿到分位附录的板块」这一实质口径）。
    assert top["sector_code"] in {"BK0475", "BK0448"}
    assert top.get("percentile_appendix"), "榜首板块应有代表股分位附录（A03 上榜前提）"
    assert "中位数" in top["content"]
    assert "可比样本" in top["content"] or "样本" in top["content"]
    assert top["retrieved_at"]
    # v36 A02：证据条目带系统参考分类与依据
    assert top["valuation_judgment"] in direction_engine.VALUATION_JUDGMENT_ORDER
    assert "PB 分位" in top["valuation_judgment_rationale"]

    # 代表股分位附录 + 代理口径声明
    appendix = top.get("percentile_appendix")
    assert appendix and appendix["representative_count"] >= 1
    assert "代理" in appendix["disclaimer"]
    assert appendix["lowest_three"]

    # 候选池估值回链
    candidate = body["stock_pool"][0]
    assert candidate["valuation_status"] == "attached"
    assert candidate["valuation_ref"]
    assert candidate["valuation"]["pb_mrq_percentile"] == 8.5

    # 提示词携带口径声明与横截面证据
    assert any("筛选口径声明" in blob and "sector_valuation" in blob for blob in calls)


def test_d01_style_topic_degradation_no_cross_section(tmp_path, monkeypatch):
    """D01：横截面取数失败 → 证据层降级 + C03 缺口声明；宏观/日历层不塌。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch, lambda: _payload("低估板块", _style_report()))
    _install_plugins(test_client, headers)
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    _stub_cross_section(monkeypatch, fail=True)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "低估板块", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True
    # 日历层兜底 → 仍为 evidence
    assert body["research_mode"] == "evidence"
    assert not any(
        item["source_type"] == "sector_valuation" for item in body["evidence_snapshot"]
    )
    gaps = " ".join(body["data_gaps"])
    assert direction_engine.GAP_CODE_CROSS_SECTION_FAILED in [
        item["code"] for item in body["style_gap_entries"]
    ]
    assert "横截面" in gaps
    # 缺口不与既有板块未命中缺口重复堆叠
    assert "未命中行业板块清单" not in gaps


def test_d01_composite_topic_limits_cross_section_to_industry(tmp_path, monkeypatch):
    """D01：复合主题「低估的银行」→ 横截面仅金融板块 + 板块行情快照并存。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch, lambda: _payload("低估的银行", _style_report()))
    _install_plugins(test_client, headers)
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    monkeypatch.setattr(
        app_module,
        "fetch_cn_sector_members",
        lambda code, top=60: _SECTOR_MEMBERS[:top] if code == "new_jrhy" else [],
    )
    _stub_cross_section(monkeypatch)
    monkeypatch.setattr(
        app_module, "fetch_valuation", lambda symbol, **kw: _valuation_snapshot(symbol, "银行股")
    )

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "低估的银行", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True
    assert body["theme_kind"] == direction_engine.THEME_KIND_COMPOSITE
    kinds = {item["source_type"] for item in body["evidence_snapshot"]}
    # 两层并存：v34 板块行情层 + v35 估值横截面层
    assert "sector_market" in kinds and "sector_valuation" in kinds
    valuation_items = [
        item for item in body["evidence_snapshot"] if item["source_type"] == "sector_valuation"
    ]
    # 横截面只含金融（银行）板块，电子板块被排除
    assert all(item["sector_code"] == "BK0475" for item in valuation_items)


def test_d01_industry_topic_regression(tmp_path, monkeypatch):
    """D01：产业主题「海运板块」回归——不进估值横截面层，板块行情快照照常注入。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch, lambda: _payload("海运板块", _style_report()))
    _install_plugins(test_client, headers)
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    monkeypatch.setattr(
        app_module, "fetch_cn_sector_members", lambda code, top=60: _SECTOR_MEMBERS[:top]
    )
    _stub_cross_section(monkeypatch)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "海运板块", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True
    assert body["theme_kind"] == direction_engine.THEME_KIND_INDUSTRY
    kinds = {item["source_type"] for item in body["evidence_snapshot"]}
    assert "sector_valuation" not in kinds  # 产业主题不触发风格估值层
    assert body["style_gap_entries"] == []


def test_d01_pool_valuation_failure_kept_and_declared(tmp_path, monkeypatch):
    """D01（B05/C03）：候选池估值取数失败 → 候选保留 + 标注缺失 + 缺口补证动作。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    pool = [
        {
            "symbol_raw": "601398",
            "name": "工商银行",
            "sector": "银行",
            "business_link": "国有大行存贷业务",
            "profit_path": "净息差与资产质量",
        }
    ]
    _stub_model(
        monkeypatch, lambda: _payload("低估板块", _style_report(), pool=pool)
    )
    _install_plugins(test_client, headers)
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    _stub_cross_section(monkeypatch)

    def _boom(symbol, **kw):
        raise ve.ValuationError("估值源断连（打桩）")

    monkeypatch.setattr(app_module, "fetch_valuation", _boom)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "低估板块", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True
    candidate = body["stock_pool"][0]
    # 候选保留（不静默丢字段），但标注估值证据缺失
    assert candidate["symbol"] == "601398"
    assert candidate["valuation_status"] == "failed"
    assert candidate["valuation"] is None
    assert "估值证据缺失" in candidate["valuation_status_label"]
    codes = [item["code"] for item in body["style_gap_entries"]]
    assert direction_engine.GAP_CODE_POOL_VALUATION_MISSING in codes
    assert any("601398" in item["text"] for item in body["style_gap_entries"])


def test_d01_budget_truncation_declared(tmp_path, monkeypatch):
    """D01（B04）：代表股预算封顶——截面板块数按榜单顺序截断且覆盖范围可见。"""
    from investment_steward_core.api import app as app_module

    monkeypatch.setattr(ve, "SECTOR_PROXY_BOARD_LIMIT", 1)
    monkeypatch.setattr(ve, "SECTOR_PROXY_STOCK_BUDGET", 2)
    try:
        test_client, headers = _direction_client(tmp_path, monkeypatch)
        _setup_profile(test_client, headers)
        _stub_model(monkeypatch, lambda: _payload("低估板块", _style_report()))
        _install_plugins(test_client, headers)
        monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
        _stub_cross_section(monkeypatch)
        monkeypatch.setattr(
            app_module, "fetch_valuation", lambda symbol, **kw: _valuation_snapshot(symbol, "股")
        )

        body = test_client.post(
            "/evidence/direction-research",
            json={"topic": "低估板块", "with_counter_check": False},
            headers=headers,
        ).json()
        assert body["ok"] is True
        valuation_items = [
            item for item in body["evidence_snapshot"] if item["source_type"] == "sector_valuation"
        ]
        # v36 A03：榜单主键改为「代表股 PB 分位中位数」，分位不可得的板块**不上榜**。
        # 本打桩给每个板块的预算仅够 1 只代表股（SECTOR_PROXY_STOCK_BUDGET=2，
        # 银行占 1 只后预算用尽），故只有拿到了分位的银行上榜；另一板块不进榜单，
        # 预算截断改由 unavailable 声明体现（而非「上榜但无附录」）。
        assert len(valuation_items) >= 1
        with_appendix = [item for item in valuation_items if item.get("percentile_appendix")]
        assert len(with_appendix) == 1
        assert with_appendix[0]["sector_code"] == "BK0475"
        # 未获取到分位的板块在 unavailable 中声明截断
        assert "截断" in (body["evidence_unavailable"] or "") or "未获取" in (
            body["evidence_unavailable"] or ""
        )
    finally:
        monkeypatch.undo()
