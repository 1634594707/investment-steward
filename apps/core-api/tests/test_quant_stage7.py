"""阶段 7（§7.2）测试:SPEC v2 回测增量、Walk-forward、泄漏/披露检查、组合回测。"""

from __future__ import annotations

import pytest

from investment_steward_core import quant_experiments, quant_portfolio
from test_quant_pool import _bars


def _score_fn(bars):
    """确定性打分:收盘价相对窗口均值的位置（同 test_quant_experiments 约定）。"""
    closes = [float(b["close"]) for b in bars]
    out = []
    for i, close in enumerate(closes):
        window = closes[max(0, i - 7):i + 1]
        mean = sum(window) / len(window)
        out.append((close - mean) / mean if mean else 0.0)
    return out


# —— SPEC v2:停牌/涨跌停/调仓/复权 ——

def test_spec_v2_features_off_matches_v1_math():
    """特性全关时 v2 权益曲线与 v1 数学等价（新旧 spec_version 标注不同）。"""
    bars = _bars(120)
    values = _score_fn(bars)
    closes = [float(b["close"]) for b in bars]
    v1 = quant_experiments.run_backtest(values, closes, legacy_v1=True)
    v2 = quant_experiments.run_backtest(values, closes)
    assert v1["equity"] == v2["equity"]
    assert v1["baseline_buy_hold"] == v2["baseline_buy_hold"]
    assert v1["spec_version"] == 1 and v2["spec_version"] == "2"
    assert v2["suspension"]["adjust"] == "qfq(source)"
    # 真实样本外分段:train + oos 天数 = 总天数
    assert v2["splits"]["train"]["bars"] + v2["splits"]["out_of_sample"]["bars"] == v2["bars"]


def test_suspension_freezes_equity():
    bars = _bars(120)
    values = _score_fn(bars)
    closes = [float(b["close"]) for b in bars]
    volumes = [1000.0] * 120
    volumes[60] = 0.0  # bar 60 停牌 → step 59（次日结算冻结）与 step 60（无信号）受影响
    result = quant_experiments.run_backtest(values, closes, volumes=volumes)
    assert result["suspension"]["suspended_bars"] >= 1
    suspended_steps = [c for c in result["curve"] if c.get("suspended")]
    assert suspended_steps and all(c["return"] == 0.0 and c["cost"] == 0.0 for c in suspended_steps)


def test_price_limit_blocks_entries():
    closes = [10.0] * 40 + [11.0] + [12.0] * 59  # i=40 收盘 +10% 涨停,次日续涨
    values = [0.0] * 40 + [1.0] * 60  # 涨停日才转多 → 涨停日收盘禁止买入
    off = quant_experiments.run_backtest(values, closes)
    blocked = quant_experiments.run_backtest(values, closes, price_limit_pct=0.10)
    assert blocked["suspension"]["limit_blocked_entries"] >= 1
    assert blocked["equity"] < off["equity"]  # 错过 11→12 段,拦截改变了执行路径


def test_rebalance_every_holds_position():
    bars = _bars(120)
    values = _score_fn(bars)
    closes = [float(b["close"]) for b in bars]
    daily = quant_experiments.run_backtest(values, closes, rebalance_every=1)
    weekly = quant_experiments.run_backtest(values, closes, rebalance_every=5)
    assert weekly["total_turnover"] < daily["total_turnover"]
    weekly_curve = weekly["curve"]
    # 非调仓日 delta=0（position 不变）
    for prev, cur in zip(weekly_curve, weekly_curve[1:]):
        if (cur["step"] % 5) != 0:
            assert cur["position"] == prev["position"]


def test_adj_factors_change_returns():
    bars = _bars(120)
    values = _score_fn(bars)
    closes = [float(b["close"]) for b in bars]
    factors = [1.0 + 0.001 * i for i in range(120)]  # 逐日微调因子
    plain = quant_experiments.run_backtest(values, closes)
    adj = quant_experiments.run_backtest(values, closes, adj_factors=factors)
    assert adj["equity"] != plain["equity"]
    assert adj["suspension"]["adjust"] == "adj_factors"


def test_default_price_limit_heuristics():
    assert quant_experiments.default_price_limit("688001") == 0.20
    assert quant_experiments.default_price_limit("300750") == 0.20
    assert quant_experiments.default_price_limit("600519") == 0.10
    assert quant_experiments.default_price_limit("510300") == 0.10
    assert quant_experiments.default_price_limit("AAPL") is None


# —— Walk-forward ——

def test_walk_forward_folds_consistent_and_honest():
    bars = _bars(240)
    values = _score_fn(bars)
    closes = [float(b["close"]) for b in bars]
    report = quant_experiments.walk_forward(values, closes, folds=3)
    assert report["folds"] == 3
    assert len(report["fold_reports"]) == 3
    # 折间不重叠:fold1.test_end == fold2.test_start + 1
    r1, r2 = report["fold_reports"][0], report["fold_reports"][1]
    assert r2["test_start"] == r1["test_end"] - 1  # 共享一根衔接 K 线用于首日收益
    assert 0.0 <= report["oos_consistency"] <= 1.0
    assert "无参数拟合" in report["honest_note"]


def test_walk_forward_insufficient_sample():
    with pytest.raises(ValueError, match="样本不足"):
        quant_experiments.walk_forward([0.1] * 30, [10.0] * 30, folds=3)


# —— 泄漏/披露检查 ——

def test_lookahead_bias_clean_fn_passes():
    bars = _bars(120)
    report = quant_experiments.detect_lookahead_bias(_score_fn, bars)
    assert report["uses_future_data"] is False
    assert report["mismatch_count"] == 0


def test_lookahead_bias_future_peeking_detected():
    bars = _bars(120)
    closes = [float(b["close"]) for b in bars]

    def peeking(bars_):
        closes_ = [float(b["close"]) for b in bars_]
        n = len(closes_)
        return [(closes_[min(i + 1, n - 1)] - c) / c for i, c in enumerate(closes_)]  # 用次日收盘

    report = quant_experiments.detect_lookahead_bias(peeking, bars)
    assert report["uses_future_data"] is True
    assert report["mismatch_count"] > 0


def test_disclosure_alignment_report():
    bars = _bars(40)
    events = [
        {"event_id": "e1", "disclosure_date": "2026-06-05", "used_from_bar": 6},  # 合规:bar6 日期=06-07
        {"event_id": "e2", "disclosure_date": "2026-06-20", "used_from_bar": 2},  # 违规:先于披露使用
        {"event_id": "e3", "disclosure_date": "2026-12-31"},  # 窗口外,只报可用点
    ]
    report = quant_experiments.check_disclosure_alignment(bars, events)
    assert report["events_checked"] == 3
    assert report["violations"] == 1
    by_id = {r["event_id"]: r for r in report["reports"]}
    assert by_id["e1"]["violation"] is False
    assert by_id["e2"]["violation"] is True
    assert by_id["e3"]["first_usable_bar"] is None


# —— 组合回测 ——

def test_portfolio_backtest_caps_and_metrics():
    symbol_returns = {
        "A": [0.01, -0.02, 0.015, 0.005, -0.01, 0.02],
        "B": [-0.005, 0.01, -0.008, 0.012, 0.004, -0.006],
        "C": [0.02, 0.01, -0.015, 0.003, 0.008, 0.01],
    }
    result = quant_portfolio.run_portfolio_backtest(
        symbol_returns,
        {"A": 0.8, "B": 0.1, "C": 0.1},  # A 超上限,应被截断
        industries={"A": "tech", "B": "finance", "C": "tech"},
        max_weight=0.4,
        max_industry_weight=0.5,
        result_hash=True,
    )
    assert result["spec_version"] == "1"
    assert all(w <= 0.4 + 1e-9 for w in result["weights_applied"].values())
    # tech 行业敞口不超过 0.5;触限余额持现金
    assert result["industry_exposure"]["tech"] <= 0.5 + 1e-6
    assert result["caps"]["trace"]["single_capped"], "A 应触发单标的截断记录"
    assert result["caps"]["remainder_policy"] == "cash"
    assert result["cash_weight"] > 0
    assert abs(sum(result["weights_applied"].values()) + result["cash_weight"] - 1.0) < 1e-6
    assert 0 < result["concentration_hhi"] <= 1.0
    assert result["max_drawdown"] >= 0.0
    assert result["avg_pairwise_correlation"] is not None
    assert result["result_hash"].startswith(tuple("0123456789abcdef"))


def test_portfolio_backtest_deterministic_and_validates():
    symbol_returns = {"A": [0.01, 0.02], "B": [0.005, -0.01]}
    r1 = quant_portfolio.run_portfolio_backtest(symbol_returns, {"A": 1, "B": 1})
    r2 = quant_portfolio.run_portfolio_backtest(symbol_returns, {"A": 1, "B": 1})
    assert r1 == r2
    with pytest.raises(ValueError, match="长度不一致"):
        quant_portfolio.run_portfolio_backtest({"A": [0.01, 0.02], "B": [0.005]}, {"A": 1, "B": 1})
    with pytest.raises(ValueError, match="至少需要 2 个"):
        quant_portfolio.run_portfolio_backtest({"A": [0.01, 0.02]}, {"A": 1})
