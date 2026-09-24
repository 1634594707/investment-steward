"""R02（2026-09-18 排版与研报呈现一致性路线图）：S1 的两个「高点」不得同名。

钉住不变量：`_s1_price_range_segment` 里，收盘区间用**最高收盘**、回撤用**盘中最高价**，
两者各自标清且数值可复算——同一篇里不再出现「区间高点」一词指代两个不同口径。
"""

from __future__ import annotations

from investment_steward_core.api import app as api_app


def test_s1_range_segment_distinguishes_closing_and_intraday_highs():
    # 复现样报 B `:21`：收盘 6.19、最高收盘 8.76、盘中最高 9.17 → 回撤 −32.5%（对 9.17）。
    closes = [5.0, 8.76, 6.19]
    highs = [5.2, 9.17, 6.3]
    drawdown = (closes[-1] / max(highs) - 1) * 100  # −32.49…
    segment = api_app._s1_price_range_segment(len(closes), closes, highs, None, drawdown)
    # 旧的歧义标签必须消失。
    assert "区间高点" not in segment
    # 收盘区间 = 最高收盘 8.76；回撤基准点名盘中最高 9.17。
    assert "收盘区间 5.0~8.76" in segment
    assert "盘中最高 9.17" in segment
    assert "回撤 -32.5%" in segment
    # 两口径确为不同的数（8.76 收盘 vs 9.17 盘中），且回撤对盘中最高可复算。
    assert api_app._round_number(max(closes), 2) != api_app._round_number(max(highs), 2)
    assert round((closes[-1] / 9.17 - 1) * 100, 1) == -32.5
    assert round((closes[-1] / 8.76 - 1) * 100, 1) != -32.5


def test_s1_range_segment_omits_drawdown_without_highs():
    closes = [5.0, 6.19]
    segment = api_app._s1_price_range_segment(len(closes), closes, [], None, None)
    assert "收盘区间 5.0~6.19" in segment
    assert "回撤" not in segment
    assert "盘中最高" not in segment
