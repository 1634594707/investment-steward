"""确定性因子挖掘测试:StackVM 求值/健壮性/确定性挖掘/参数集形态。"""

from __future__ import annotations

import pytest

from investment_steward_core import quant_factors


def _bars(count: int = 120) -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    for i in range(count):
        phase = i % 16
        price = 10.0 + phase * 0.25 if phase < 8 else 12.0 - (phase - 8) * 0.25
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": round(price - 0.02, 3),
            "high": round(price + 0.06, 3),
            "low": round(price - 0.06, 3),
            "close": round(price, 3),
            "volume": 1000.0 + i,
        })
    return bars


def test_evaluate_tokens_tanh_formula():
    values = quant_factors.evaluate_tokens(["ret_5", "tanh"], _bars(40))
    assert len(values) == 40
    assert all(-1 <= v <= 1 for v in values)


def test_evaluate_tokens_div_by_zero_safe():
    values = quant_factors.evaluate_tokens(["ret_1", "0", "div"], _bars(30))
    assert all(v == 0.0 for v in values)


def test_evaluate_tokens_illegal_formula_raises():
    with pytest.raises(ValueError):
        quant_factors.evaluate_tokens(["add"], _bars(30))
    with pytest.raises(ValueError):
        quant_factors.evaluate_tokens(["no_such_feature"], _bars(30))


def test_mine_is_deterministic_and_outputs_parameter_sets():
    first = quant_factors.mine(_bars(250))
    second = quant_factors.mine(_bars(250))
    assert first == second  # 同输入同结果(确定性)
    for item in first["top"]:
        assert "formula_tokens" in item and "train_ic" in item and "valid_ic" in item
        assert abs(item["valid_ic"]) >= 0.02
    # 参数集形态:JSON 可序列化(分享池阶段 A)
    import json
    json.dumps(first["top"], ensure_ascii=False)
