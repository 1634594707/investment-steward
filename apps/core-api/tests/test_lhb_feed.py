"""东财龙虎榜内核（P2）夹具测试——**全程不联网**。

重点是三类「合规缺口」的确定性防护：
数据商统计字段不许落库（铁律 6）、桶不许当游资、主键必须稳定。
"""

from __future__ import annotations

import json

import pytest
from conftest import load_youzi_json
from investment_steward_core import lhb_feed as lf

DAY = "20260916"


@pytest.fixture(autouse=True)
def _clear_caches():
    lf.reset_caches()
    yield
    lf.reset_caches()


# —— 上报列表 ——


def test_parse_billboard_matches_fixture_size():
    rows = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    assert len(rows) == 71


def test_billboard_row_carries_explanation_and_net_amount():
    rows = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    top = rows[0]
    # 首行随夹具字节而定，不硬编码个股：夹具已按 2026-09-16 重新抓取（首行 000592），
    # 旧断言锁的是上一版字节的首行 002281。这里改为「首行是当日真实行」的不变量断言。
    assert top.security_code == "000592"
    assert top.trading_day == DAY
    assert top.billboard_net_amt == pytest.approx(456626115.65)
    assert top.explanation


def test_billboard_trade_date_normalised():
    rows = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    assert {row.trading_day for row in rows} == {DAY}


# —— 铁律 6：数据商统计绝不落库 ——


@pytest.mark.parametrize("field", lf.FORBIDDEN_FIELDS)
def test_forbidden_fields_absent_from_row_dataclass(field: str):
    """`RISE_PROBABILITY_3DAY` / `TOTAL_BUYER_SALESTIMES_3DAY` 不得成为我方字段。"""
    assert field not in lf.BillboardRow.__dataclass_fields__
    assert field not in lf.SeatRow.__dataclass_fields__


@pytest.mark.parametrize("field", lf.FORBIDDEN_FIELDS)
def test_forbidden_fields_absent_from_parsed_output(field: str):
    """解析结果里也不许出现这些字段（夹具里确实有，所以不是空转）。"""
    raw = load_youzi_json("em_lhb_detailbuy_20260916.json")
    assert field in raw["result"]["data"][0], "夹具应包含该字段，否则此断言无意义"
    rows = lf.parse_seat_rows(raw, lf.DIRECTION_BUY)
    assert all(field not in str(row) for row in rows)


def test_seat_row_has_no_probability_attribute():
    raw = load_youzi_json("em_lhb_detailbuy_20260916.json")
    rows = lf.parse_seat_rows(raw, lf.DIRECTION_BUY)
    assert rows
    for name in ("rise_probability_3day", "total_buyer_salestimes_3day", "success_rate"):
        assert not hasattr(rows[0], name)


def test_explain_note_is_kept_as_text_only():
    """`EXPLAIN` 夹带的「成功率」只作为原文说明保留，不解析成数值。"""
    rows = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    notes = [row.explain_note for row in rows if "成功率" in row.explain_note]
    assert notes, "夹具里应有夹带成功率的 EXPLAIN 行"
    # 说明字段是字符串，且没有对应的数值字段
    assert all(isinstance(note, str) for note in notes)
    assert lf._SUCCESS_RATE_IN_TEXT.search(notes[0])


# —— 席位明细 ——


def test_parse_buy_and_sell_seats():
    buys = lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY)
    sells = lf.parse_seat_rows(
        load_youzi_json("em_lhb_detailsell_20260916.json"), lf.DIRECTION_SELL
    )
    assert len(buys) == 365
    assert len(sells) == 365
    assert {row.direction for row in buys} == {lf.DIRECTION_BUY}
    assert {row.direction for row in sells} == {lf.DIRECTION_SELL}


def test_sell_report_exists_and_matches_buy_shape():
    """方案里原本留着「接入前再钉名称」，这条断言把 SELL report 钉死。

    两侧字段集实测一致，因此解析器可以共用；若上游哪天改了结构，这里会先炸。
    """
    assert lf.REPORT_SEAT_SELL == "RPT_BILLBOARD_DAILYDETAILSSELL"
    buy_raw = load_youzi_json("em_lhb_detailbuy_20260916.json")["result"]["data"][0]
    sell_raw = load_youzi_json("em_lhb_detailsell_20260916.json")["result"]["data"][0]
    assert set(buy_raw) == set(sell_raw)
    buys = lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY)
    sells = lf.parse_seat_rows(
        load_youzi_json("em_lhb_detailsell_20260916.json"), lf.DIRECTION_SELL
    )
    assert type(buys[0]) is type(sells[0])


def test_identity_key_is_stable_and_frozen():
    """主键 = (交易日, 代码, 席位代码, 方向)，P0 A03 冻结值。"""
    buys = lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY)
    row = buys[0]
    assert row.identity_key == (DAY, row.security_code, row.operatedept_code, lf.DIRECTION_BUY)
    assert len(row.identity_key) == 4


def test_identity_key_distinguishes_direction():
    """同一席位买/卖两侧必须能区分，否则会互相覆盖。"""
    buys = lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY)
    sells = lf.parse_seat_rows(
        load_youzi_json("em_lhb_detailsell_20260916.json"), lf.DIRECTION_SELL
    )
    assert buys[0].identity_key != sells[0].identity_key


@pytest.mark.parametrize("direction", [lf.DIRECTION_BUY, lf.DIRECTION_SELL])
def test_seats_for_security_orders_by_direction_amount(direction):
    """真实夹具：按买榜 BUY / 卖榜 SELL 排序，而不是按轧差净额。"""
    from dataclasses import replace

    name = (
        "em_lhb_detailbuy_20260916.json"
        if direction == lf.DIRECTION_BUY
        else "em_lhb_detailsell_20260916.json"
    )
    rows = lf.parse_seat_rows(load_youzi_json(name), direction)
    picked = lf.seats_for_security(rows, "000592", direction)
    assert picked
    assert all(row.security_code == "000592" and row.direction == direction for row in picked)
    amounts = [getattr(row, direction) for row in picked if getattr(row, direction) is not None]
    assert amounts == sorted(amounts, reverse=True)

    # 合成反例独立于原始夹具：净额最大不是该侧金额最大，缺失排在真实零之后。
    base = picked[0]
    amounts_and_nets = [(10.0, -1000.0), (100.0, 1.0), (None, 9999.0), (0.0, -999.0)]
    synthetic = [
        replace(base, operatedept_code=str(i), net=net, **{direction: amount})
        for i, (amount, net) in enumerate(amounts_and_nets)
    ]
    result = lf.seats_for_security(synthetic, "000592", direction)
    assert [row.operatedept_code for row in result] == ["1", "0", "3", "2"]
    assert result[1].net == -1000.0  # 排序不得改写净额或翻转卖榜符号。


def test_seats_for_security_rejects_bad_direction():
    with pytest.raises(ValueError, match="direction"):
        lf.seats_for_security([], "000592", "both")


def test_seats_for_security_returns_empty_for_unlisted_code():
    """未上榜股票返回空列表，由调用方决定不生成 S6（而不是这里造个空行）。"""
    buys = lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY)
    assert lf.seats_for_security(buys, "999999", lf.DIRECTION_BUY) == []


def test_parse_seat_rows_rejects_bad_direction():
    with pytest.raises(ValueError, match="direction"):
        lf.parse_seat_rows(load_youzi_json("em_lhb_detailbuy_20260916.json"), "both")


# —— 失败响应必须可读地透传 ——


def test_like_query_rejection_is_surfaced_verbatim():
    """实测 `like` 被数据源拒绝；原因必须原样可见，不能被当成「无数据」。"""
    reason = lf.payload_error(load_youzi_json("em_like_unsupported.json"))
    assert "不支持like查询" in reason
    assert "9501" in reason


def test_empty_bucket_name_lookup_is_surfaced():
    reason = lf.payload_error(load_youzi_json("em_bucket_name_empty.json"))
    assert "返回数据为空" in reason
    assert "9201" in reason


def test_success_payload_has_no_error():
    assert lf.payload_error(load_youzi_json("em_lhb_details_20260916.json")) == ""


def test_failed_payload_yields_no_rows():
    """失败响应不得解析出任何行——否则「查不到」会变成「没上榜」。"""
    assert lf.parse_billboard_rows(load_youzi_json("em_like_unsupported.json")) == []
    assert lf.parse_seat_rows(load_youzi_json("em_bucket_name_empty.json"), lf.DIRECTION_BUY) == []


def test_operatedept_history_requires_code_not_name():
    """数据源不支持 like 查询，因此档案接口必须要求精确代码。"""
    with pytest.raises(ValueError, match="operatedept_code"):
        lf.fetch_operatedept_history("")


# —— 日期归一化 ——


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-16 00:00:00", "20260916"),
        ("2026-09-16", "20260916"),
        ("20260916", "20260916"),
        ("2026/09/16", "20260916"),
        ("", ""),
    ],
)
def test_normalise_trade_date(raw, expected):
    assert lf.normalise_trade_date(raw) == expected


def test_operatedept_fixture_parses_via_extract_rows():
    """席位档案夹具（紫阳东路）应能取出记录。"""
    payload = load_youzi_json("em_operatedept_trade_10026937.json")
    rows = lf._extract_result_rows(payload)
    assert rows
    assert all(str(row.get("OPERATEDEPT_CODE")) == "10026937" for row in rows)
    assert any(str(row.get("SECURITY_CODE")) == "000592" for row in rows)


def test_report_names_are_frozen_constants():
    assert lf.REPORT_DAILY_BILLBOARD == "RPT_DAILYBILLBOARD_DETAILS"
    assert lf.REPORT_SEAT_BUY == "RPT_BILLBOARD_DAILYDETAILSBUY"
    assert lf.REPORT_SEAT_SELL == "RPT_BILLBOARD_DAILYDETAILSSELL"
    assert lf.REPORT_OPERATEDEPT_TRADE == "RPT_OPERATEDEPT_TRADE_DETAILS"


def test_json_roundtrip_does_not_inject_forbidden_keys():
    rows = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    blob = json.dumps([row.__dict__ for row in rows], ensure_ascii=False)
    for field in lf.FORBIDDEN_FIELDS:
        assert field not in blob
