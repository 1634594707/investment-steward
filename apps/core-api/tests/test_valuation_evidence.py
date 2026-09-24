"""估值证据测试（v24 延伸：档 A 报表派生 + 档 B 当前估值 + 档 C 历史分位/同业相对）。

红线：
- **分位只对正数样本计算**：亏损股（PE ≤ 0）不可比，必须置 None 并写明原因，
  绝不把负值塞进分位序列冒充「低估」；
- **TTM 必须按同月日做累计差分**（最新累计 + 上年年报 − 去年同期），缺基数即置 None 并说明，
  禁止用「单期年化」冒充 TTM；
- **同业中位数/位次只用正数样本**，亏损股单列计数，不参与比较（否则中位数被 -400 倍 PE 拉偏）；
- **估值来源（S5）独立降级**：取数失败只记 source_errors，绝不用财务数据冒充估值；
- 证据文本必须真的进入送模型的 prompt，并带显式 [S#] 前缀（否则模型无从正确引用）。

本文件全部用合成数据，绝不真实联网。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import derived_metrics as dm
from investment_steward_core import valuation_evidence as ve


def _row(statement_type: str, period_end: str, items: dict[str, str]) -> SimpleNamespace:
    """合成一行报表（字段与 `FinancialStatementRow` 契约一致，供纯函数消费）。"""
    return SimpleNamespace(
        statement_type=statement_type,
        period_end=period_end,
        items=items,
        published_raw=period_end,
        identity=f"{statement_type}-{period_end}",
    )


def _snapshot(**overrides) -> ve.ValuationSnapshot:
    base = dict(
        symbol="600519", name="贵州茅台", board_code="016165", board_name="白酒Ⅱ",
        trade_date="2026-09-10", close_price=1285.13, pe_ttm=19.73, pe_static=19.52,
        pb_mrq=6.39, ps_ttm=9.27, peg=-4.76, pcf_ocf_ttm=13.49,
        total_market_cap=1.6065e12, float_market_cap=1.6065e12, total_shares=1.25e9,
    )
    base.update(overrides)
    return ve.ValuationSnapshot(**base)  # type: ignore[arg-type]


# ---- A. 分位与位次口径 ----


def test_percentile_rank_only_uses_positive_sample():
    series = [10.0] * 10 + [20.0] * 10 + [-99.0] * 10  # 20 个正数、10 个负值
    assert ve.percentile_rank(series, 15.0) == 50.0     # 正数里 10 个 < 15
    assert ve.percentile_rank(series, 5.0) == 0.0
    assert ve.percentile_rank(series, 25.0) == 100.0
    assert ve.percentile_rank(series, -1.0) is None     # 当前值为负 → 分位无经济含义
    assert ve.percentile_rank(series, None) is None


def test_percentile_rank_requires_min_samples():
    assert ve.percentile_rank([1.0, 2.0, 3.0], 2.0) is None  # 样本 < 20 不给分位


def test_rank_of_excludes_loss_making_peers():
    values = [10.0, 20.0, -5.0, 30.0]
    assert ve._rank_of(values, 20.0) == (2, 3)  # 负数被排除：可比 3 只，本股第 2
    assert ve._rank_of(values, -1.0) is None    # 亏损股自身不可比


def test_median_uses_positive_sample_only():
    values = [-400.0, -150.0, 10.0, 20.0, 30.0]
    assert ve._median([v for v in values if v > 0]) == 20.0


# ---- B. 档 A：报表派生（TTM 差分 / ROE / 含金量） ----


def test_derive_ttm_uses_cumulative_difference():
    rows = [
        _row("income", "2026-06-30", {"revenue": "1000", "net_profit": "120"}),
        _row("income", "2025-12-31", {"revenue": "1800", "net_profit": "200"}),
        _row("income", "2025-06-30", {"revenue": "900", "net_profit": "100"}),
        _row("balance", "2026-06-30", {"total_equity": "5000"}),
        _row("cash_flow", "2026-06-30", {"operating_cash_flow": "150"}),
        _row("cash_flow", "2025-12-31", {"operating_cash_flow": "300"}),
        _row("cash_flow", "2025-06-30", {"operating_cash_flow": "120"}),
    ]
    m = dm.derive_fundamental_metrics(rows, total_shares=100.0, latest_price=10.0)
    assert m.latest_period == "2026-06-30"
    assert m.ttm_revenue == 1900.0            # 1000 + 1800 − 900（累计差分，非单期年化）
    assert m.ttm_net_profit == 220.0
    assert m.ttm_operating_cash_flow == 330.0
    assert m.cash_conversion == 1.5           # 330 / 220
    assert m.roe_ttm == pytest.approx(4.4)    # 220 / 5000 × 100
    assert "最新净资产" in m.roe_basis         # 无去年同期净资产 → 用最新期并标注口径
    assert m.derived_eps_ttm == 2.2           # 220 / 100
    assert m.derived_pe_ttm == pytest.approx(4.55)


def test_derive_ttm_missing_prior_base_is_none_not_annualised():
    rows = [
        _row("income", "2026-06-30", {"revenue": "1000", "net_profit": "120"}),
        _row("balance", "2026-06-30", {"total_equity": "5000"}),
    ]
    m = dm.derive_fundamental_metrics(rows, total_shares=100.0, latest_price=10.0)
    assert m.ttm_net_profit is None   # 缺去年同期/上年年报基数 → 不臆造
    assert m.roe_ttm is None
    assert any("TTM" in note for note in m.notes)


def test_derive_roe_uses_average_equity_when_available():
    rows = [
        _row("income", "2026-06-30", {"net_profit": "120"}),
        _row("income", "2025-12-31", {"net_profit": "200"}),
        _row("income", "2025-06-30", {"net_profit": "100"}),
        _row("balance", "2026-06-30", {"total_equity": "5000"}),
        _row("balance", "2025-06-30", {"total_equity": "3000"}),
    ]
    m = dm.derive_fundamental_metrics(rows)
    assert m.ttm_net_profit == 220.0
    assert m.roe_ttm == pytest.approx(5.5)  # 220 / ((5000+3000)/2) × 100
    assert "平均净资产" in m.roe_basis


def test_derive_annual_period_is_ttm_directly():
    rows = [
        _row("income", "2026-12-31", {"net_profit": "10"}),
        _row("balance", "2026-12-31", {"total_equity": "1000"}),
        _row("cash_flow", "2026-12-31", {"operating_cash_flow": "3"}),
    ]
    m = dm.derive_fundamental_metrics(rows)
    assert m.ttm_net_profit == 10.0       # 年报累计即 TTM
    assert m.cash_conversion == 0.3
    text = dm.describe_derived(m)
    assert "含金量不足" in text            # 0.3 < 0.8 触发提示
    assert "无数据" in text                # 未给总股本 → 派生 PE 段如实缺省


def test_derive_without_rows_raises():
    with pytest.raises(dm.DerivedMetricsError):
        dm.derive_fundamental_metrics([])


# ---- C. 档 B/C：估值文本渲染 ----


def test_describe_valuation_renders_all_sections_and_gaps():
    snap = _snapshot(
        percentiles={"pe_ttm": 6.7, "pb_mrq": 4.2, "ps_ttm": 3.6},
        percentile_window_bars=1250, percentile_window_from="2021-07-19",
        peer_count=18, peer_median={"pe_ttm": 27.85, "pb_mrq": 2.23},
        peer_median_basis={"pe_ttm": 13}, peer_rank={"pe_ttm": 5}, peer_rank_basis={"pe_ttm": 13},
        peer_loss_count=5,
        peers=(ve.PeerValuation("600809", "山西汾酒", 13.9, 3.72, 3.96),),
    )
    text = ve.describe_valuation(snap)
    assert "PE(TTM) 19.73" in text
    assert "6.7%" in text and "1250" in text
    assert "27.85" in text and "第 5/13" in text
    assert "亏损股：5 只" in text
    assert "不含股息率" in text            # 数据源无该字段，必须如实声明而非留白
    assert "山西汾酒" in text


def test_describe_valuation_marks_missing_valuation_as_无数据():
    text = ve.describe_valuation(_snapshot(pe_ttm=None, pb_mrq=None, ps_ttm=None))
    assert "无数据" in text


# ---- D. 端到端：证据真的进 prompt，且 S5 可被引用 / 独立降级 ----


def test_valuation_and_derived_reach_model_prompt(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report_json = json.dumps({
        "title": "甲股：估值处于历史低位",
        "report": ("### 技术面\n缓涨 [S1]\n### 消息面\n无新增 [S2]\n"
                   "### 基本面\nPE(TTM) 19.73，处近五年 6.7% 分位 [S5]\n### 综合判断\n偏便宜 [S5]"),
        "citations": ["S1", "S5"],
        "limitations": ["公开来源摘要口径"],
        "confidence": "medium",
        "valuation": {"pe_ttm": "19.73", "pe_percentile": "近5年 6.7% 分位",
                      "peer_position": "低于同业中位 27.85", "verdict": "偏便宜", "basis": "S5 分位"},
    }, ensure_ascii=False)
    payloads: list[str] = []
    # v27 起本端点会追加一次「反方检查」短调用：第 2 次返回合法 JSON，
    # 让报告链路走完整路径；证据文本断言则锁定**第 1 次**（报告）调用的 payload。
    counter_json = json.dumps({
        "strongest_support": "PE(TTM) 处近五年 6.7% 分位 [S5]",
        "strongest_counter": "分位偏低不等于便宜：S5 只给静态分位，未覆盖盈利下修方向。",
        "necessary_conditions": ["盈利不再下修"],
        "most_misread_sentence": {"quote": "偏便宜", "why": "易被读成买入建议"},
        "verdict_robustness": "mixed",
    }, ensure_ascii=False)

    def _fake(*args, **kwargs):
        payloads.append(json.dumps(
            [str(item) for item in args] + [str(value) for value in kwargs.values()], ensure_ascii=False
        ))
        return {"choices": [{"message": {"content": report_json if len(payloads) == 1 else counter_json}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)
    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    assert set(body["source_keys"]) == {"S2", "S3", "S4", "S5"}
    assert "S5" in body["available_citations"]
    # J01（2026-09-18 桌面端路线图）：模型只给分位/同业比较、既没给 valuation_status
    # 也没给正常化盈利 → 状态确定性判为「无法判断」，此时贵贱结论词必须一并归一，
    # 否则会出现「结论=偏便宜」与「估值状态=无法判断」并排打脸（样报 002468）。
    # 模型原话不删，留在 verdict_raw / verdict_note。
    assert body["valuation"]["verdict"] == "无数据"
    assert body["valuation"]["verdict_raw"] == "偏便宜"
    assert "缺少正常化盈利支撑" in body["valuation"]["verdict_note"]
    assert body["valuation"]["valuation_status"] == "undetermined"
    # 证据文本必须真实进入 prompt，且带显式编号前缀（只对报告调用断言，反方检查是另一次调用）
    report_blob = payloads[0]
    assert "[S5]" in report_blob
    assert "估值快照" in report_blob
    assert "派生指标" in report_blob
    assert "PE(TTM) 19.73" in report_blob
    # S4 内联派生段（同源于财报，不新开来源编号）
    assert "ROE(TTM)" in report_blob


def test_valuation_failure_degrades_without_touching_citations(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)

    def _raise(*_a, **_k):
        raise ve.ValuationError("估值接口不可用")

    monkeypatch.setattr("investment_steward_core.api.app.fetch_valuation", _raise)
    report_json = json.dumps({
        "title": "甲股：估值无数据",
        "report": "### 技术面\n缓涨 [S1]\n### 消息面\n无 [S2]\n### 基本面\n估值无数据\n### 综合判断\n观察 [S1]",
        "citations": ["S1", "S5"],  # S5 本次取数失败 → 必须被剔除（不得引用不存在的来源）
        "confidence": "low",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_json}}]})

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    assert "valuation" in body["source_errors"]        # 失败如实记录
    assert "S5" not in body["available_citations"]     # 失败来源不可被引用
    assert body["citation_dropped"] == ["S5"]          # 虚构引用被硬闸门剔除
    assert set(body["source_keys"]) == {"S2", "S3", "S4"}
