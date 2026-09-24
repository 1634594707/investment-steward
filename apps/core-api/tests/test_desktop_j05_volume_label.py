"""J05（桌面端升级路线图 2026-09-18）：S1 量能三态标签与量比可复算的闸门测试。

背景（样报 002468 §1.5）：旧二值判定 `放量 if vol5 > vol20*1.1 else 缩量` 把量比 1.09
（156053/143122）标成「缩量」，模型只能在正文里反驳自家字段（样报 :25）。
单位实测（2026-09-19）：腾讯源 volume = 手（茅台当日 K 线量 24891 与 quote 成交量一致，
成交额/(量×100) = 1259.83 元/股 ≈ 现价 1257.12）；东财 push2his 本环境断连，单位未实测不写。
"""

from __future__ import annotations

from investment_steward_core.api.app import _s1_volume_segment, volume_state_label


def test_ratio_1_09_is_not_shrinking():
    """样报 156053/143122 ≈ 1.09 必须是「基本持平」，不再误标「缩量」。"""
    label, ratio = volume_state_label(156053.0, 143122.0)
    assert label == "基本持平"
    assert abs(ratio - 1.09) < 0.005


def test_three_state_thresholds_are_explicit():
    assert volume_state_label(120.0, 100.0)[0] == "放量"       # > 1.1
    assert volume_state_label(110.0, 100.0)[0] == "基本持平"   # = 1.1（含右端点）
    assert volume_state_label(90.0, 100.0)[0] == "基本持平"    # = 0.9（含左端点）
    assert volume_state_label(89.0, 100.0)[0] == "缩量"        # < 0.9


def test_segment_writes_ratio_and_unit_for_tencent():
    text = _s1_volume_segment(24891.0, 23512.0, "tencent")
    assert "量比 1.06" in text, "量比随文写出，形容词可对数字复算"
    assert "基本持平" in text
    assert "手" in text, "腾讯源单位实测为「手」，必须写入 S1"


def test_segment_omits_unit_for_unverified_provider():
    """东财单位未经实测：宁缺不假，不写单位，但三态与量比照常。"""
    text = _s1_volume_segment(30000.0, 25000.0, "eastmoney")
    assert "放量" in text and "量比 1.20" in text
    assert "手" not in text


def test_no_internal_contradiction_in_segment():
    """同一段内形容词与相邻数字不得互相打脸（样报 :25 的缺陷形态）。"""
    text = _s1_volume_segment(100.0, 100.0, "tencent")
    assert "量比 1.00" in text and "基本持平" in text
    assert "缩量" not in text and "放量" not in text
