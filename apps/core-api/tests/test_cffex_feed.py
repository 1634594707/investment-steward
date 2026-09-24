"""中金所席位内核（P1）夹具测试——**全程不联网**。

夹具是 2026-09-16 真实响应原文（见 `tests/fixtures/youzi/README.md`），
其中的反例（期权、国债、HTML 错误页）与真实数据同样重要：
它们保证「脏数据被挡住」这件事有测试兜着，而不是靠评审时的印象。
"""

from __future__ import annotations

import pytest
from conftest import load_youzi_fixture
from investment_steward_core import cffex_feed as cf

TRADING_DAY = "20260916"


@pytest.fixture(autouse=True)
def _clear_caches():
    """模块级缓存跨用例共享，必须逐用例清空，否则出现顺序相关失败。"""
    cf.reset_caches()
    yield
    cf.reset_caches()


# —— 品种白名单：期权与国债必须被挡在门外 ——


def test_index_parse_keeps_only_index_futures():
    """`index.xml` 同时含期权与国债期货；白名单过滤后只应剩股指四品种。"""
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    products = {item.product_id for item in contracts}
    assert products == set(cf.CFFEX_INDEX_PRODUCTS)
    # 反例：期权 MO 与国债 T/TF/TL/TS 一个都不许漏进来
    for banned in ("MO", "HO", "IO", "T", "TF", "TL", "TS"):
        assert banned not in products


def test_index_parse_drops_option_rows_by_name():
    """夹具里确实存在期权行，否则上一个测试是空转。"""
    raw = load_youzi_fixture("cffex_index_20260916.xml")
    assert "<productid>MO</productid>" in raw
    assert "<productid>T</productid>" in raw


# —— 合约口径：近月 vs 持仓量最高，两者不同且都必须准确 ——


def test_near_contracts_follow_contract_month_not_open_interest():
    """近月按合约月份，不按持仓量。"""
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    day = cf.build_product_day(contracts, "IF")
    assert day is not None
    assert [item.instrument_id for item in day.near_contracts] == ["IF2609", "IF2610"]


def test_open_interest_leader_differs_from_near_contract():
    """2026-09-16 的 IF：近月是 IF2609，持仓量最高却是 IF2612。"""
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    day = cf.build_product_day(contracts, "IF")
    assert day is not None
    assert day.open_interest_leader is not None
    assert day.open_interest_leader.instrument_id == "IF2612"
    assert day.open_interest_leader.instrument_id != day.near_contracts[0].instrument_id


def test_if2609_matches_the_plan_document_numbers():
    """方案 §4.3 引用的「IF 近月：成交 60392 手，持仓 57081」必须能从夹具复现。

    这条是**跨文档一致性**断言：方案里的示例数字若与真实披露不符，测试就会失败。
    """
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    by_id = {item.instrument_id: item for item in contracts}
    if2609 = by_id["IF2609"]
    assert if2609.volume == 60392
    assert if2609.open_interest == 57081


def test_contract_month_key_orders_by_month():
    assert cf.contract_month_key("IF2609") < cf.contract_month_key("IF2610")
    assert cf.contract_month_key("IF2610") < cf.contract_month_key("IF2612")
    assert cf.contract_month_key("IF2612") < cf.contract_month_key("IF2703")
    # 解析不出来的排到最后，而不是猜一个月份
    bad = cf.contract_month_key("IF-UNKNOWN")
    assert bad[0] == 10**9


# —— 汇总与缺失语义 ——


def test_snapshot_summarises_all_four_products():
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    snapshot = cf.build_snapshot(contracts, trading_day=TRADING_DAY)
    assert snapshot.trading_day == TRADING_DAY
    assert [item.product_id for item in snapshot.products] == list(cf.CFFEX_INDEX_PRODUCTS)
    assert not snapshot.is_empty


def test_missing_product_is_reported_not_substituted():
    """只给 IF 的合约时，其余三品种如实缺项，不得用别的品种顶上。"""
    contracts = [
        item
        for item in cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
        if item.product_id == "IF"
    ]
    snapshot = cf.build_snapshot(contracts, trading_day=TRADING_DAY)
    assert [item.product_id for item in snapshot.products] == ["IF"]
    joined = "\n".join(snapshot.limitations)
    assert "IH" in joined and "IC" in joined and "IM" in joined
    assert "未用其他品种代替" in joined


def test_empty_contracts_yield_degradable_snapshot():
    snapshot = cf.build_snapshot([], trading_day=TRADING_DAY)
    assert snapshot.is_empty
    text = cf.describe_snapshot(snapshot)
    assert "未取得任何股指期货品种数据" in text
    # 空快照不得渲染出任何具体数字
    assert "0 手" not in text


# —— 基差：只有拿到现货才算，否则如实说「未计算」 ——


def test_basis_not_computed_without_spot_data():
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    snapshot = cf.build_snapshot(contracts, trading_day=TRADING_DAY)
    assert snapshot.basis == {}
    assert "基差未计算" in snapshot.basis_note
    assert "基差未计算" in cf.describe_snapshot(snapshot)


def test_basis_computed_when_spot_available():
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    spot = {"000300": 4480.27, "000016": 2865.30, "000905": 7683.47, "000852": 7593.00}
    snapshot = cf.build_snapshot(contracts, spot_closes=spot, trading_day=TRADING_DAY)
    assert set(snapshot.basis) == set(cf.CFFEX_INDEX_PRODUCTS)
    # 基差 = 持仓量最高合约收盘价 - 现货；IF2612 收盘 4389.4
    assert snapshot.basis["IF"] == pytest.approx(4389.4 - 4480.27)


def test_basis_partial_when_one_spot_missing():
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    snapshot = cf.build_snapshot(
        contracts, spot_closes={"000300": 4480.27}, trading_day=TRADING_DAY
    )
    assert set(snapshot.basis) == {"IF"}
    assert "部分品种基差未计算" in snapshot.basis_note


# —— 证据文本 ——


def test_describe_snapshot_labels_both_contract_calibers():
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    text = cf.describe_snapshot(cf.build_snapshot(contracts, trading_day=TRADING_DAY))
    assert "近月" in text
    assert "次近月" in text
    assert "持仓量最高" in text
    # 代客口径与「非全市场」局限必须出现
    assert "代客" in text
    assert "非全市场" in text


def test_describe_snapshot_shows_none_fields_as_no_data():
    """缺字段写「无数据」，不能写成 0 手——0 和「没拿到」是两件事。

    构造一个持仓量缺失的合约：它必须渲染成「无数据」且**不**出现任何 `0` 手。
    """
    contract = cf.CffexContract(
        instrument_id="IF2609",
        product_id="IF",
        trading_day=TRADING_DAY,
        close=None,
        settlement=None,
        pre_settlement=None,
        volume=None,
        open_interest=None,
        pre_open_interest=None,
    )
    snapshot = cf.build_snapshot([contract], trading_day=TRADING_DAY)
    text = cf.describe_snapshot(snapshot)
    assert "无数据" in text
    assert "0 手" not in text
    # 持仓量变化无法计算时必须留空，而不是算成 0
    assert "(+0)" not in text and "(-0)" not in text
    assert contract.open_interest_change is None


# —— 会员排名解析 ——


def test_parse_ranking_reads_all_three_datatypes():
    rows = cf.parse_ranking(load_youzi_fixture("cffex_ccpm_IF_20260916.xml"), "IF")
    assert rows
    assert {row.datatype_id for row in rows} == set(cf.CFFEX_DATATYPE_LABELS)
    assert {row.product_id for row in rows} == {"IF"}


def test_datatype_labels_are_the_frozen_three():
    assert cf.CFFEX_DATATYPE_LABELS == {"0": "成交量", "1": "持买单量", "2": "持卖单量"}


def test_ranking_top_filters_by_caliber_and_respects_order():
    rows = cf.parse_ranking(load_youzi_fixture("cffex_ccpm_IF_20260916.xml"), "IF")
    top = cf.ranking_top(rows, cf.CFFEX_DATATYPE_LONG, top_n=5)
    assert len(top) == 5
    assert all(row.datatype_id == cf.CFFEX_DATATYPE_LONG for row in top)
    assert [row.rank for row in top] == sorted(row.rank for row in top)


def test_ranking_calibers_do_not_mix_numbers():
    """同一席位在成交量口径与持买单量口径下的数字不同，绝不能混用。"""
    rows = cf.parse_ranking(load_youzi_fixture("cffex_ccpm_IF_20260916.xml"), "IF")
    volume_top = {
        row.short_name: row.volume
        for row in cf.ranking_top(rows, cf.CFFEX_DATATYPE_VOLUME, 1)
    }
    long_top = {
        row.short_name: row.volume
        for row in cf.ranking_top(rows, cf.CFFEX_DATATYPE_LONG, 1)
    }
    assert volume_top  # 夹具非空
    shared = set(volume_top) & set(long_top)
    for name in shared:
        assert volume_top[name] != long_top[name]


def test_ranking_parses_second_product_without_crossing_over():
    """IH 排名不得混入 IF 的行。"""
    rows = cf.parse_ranking(load_youzi_fixture("cffex_ccpm_IH_20260916.xml"), "IH")
    assert rows
    assert {row.product_id for row in rows} == {"IH"}
    assert all(row.instrument_id.startswith("IH") for row in rows if row.instrument_id)


def test_ranking_rejects_unknown_product():
    with pytest.raises(ValueError, match="不支持的品种"):
        cf.fetch_ranking(TRADING_DAY, "MO")


# —— 路径与 URL 构造 ——


def test_normalise_trading_day_accepts_common_forms():
    assert cf.normalise_trading_day("20260916") == "20260916"
    assert cf.normalise_trading_day("2026-09-16") == "20260916"
    assert cf.normalise_trading_day("2026/09/16") == "20260916"


def test_normalise_trading_day_rejects_garbage():
    with pytest.raises(ValueError):
        cf.normalise_trading_day("2026-9")
    with pytest.raises(ValueError):
        cf.normalise_trading_day("not-a-day")


def test_url_templates_match_the_documented_paths():
    """URL 模板必须与路线图 §2.1 记录的实测地址一致。"""
    assert cf._INDEX_URL_TEMPLATE.format(yyyymm="202609", dd="16") == (
        "http://www.cffex.com.cn/sj/hqsj/rtj/202609/16/index.xml"
    )
    assert cf._CCPM_URL_TEMPLATE.format(yyyymm="202609", dd="16", product="IF") == (
        "http://www.cffex.com.cn/sj/ccpm/202609/16/IF.xml"
    )


def test_html_error_page_is_treated_as_failure(monkeypatch):
    """中金所缺文件时返回 HTML 错误页而非 404；必须当失败，不能当数据解析。"""
    html = "<!DOCTYPE html PUBLIC ...><html><head><title>网页错误</title></head></html>"

    def _fake_urlopen(request, timeout=None):
        class _Response:
            def read(self) -> bytes:
                return html.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()

    monkeypatch.setattr(cf.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(cf.CffexError, match="HTML 错误页"):
        cf._http_get_text("http://www.cffex.com.cn/sj/hqsj/rtj/202609/16/index.xml")


def test_decode_body_prefers_utf8_then_falls_back_to_gb18030():
    """`_decode_body`：正常 XML 走 UTF-8；gb2312 的错误页走兜底，不许抛异常。"""
    assert cf._decode_body("<x>(c) 沪</x>".encode("utf-8")) == "<x>(c) 沪</x>"
    assert "网页错误" in cf._decode_body("<title>网页错误</title>".encode("gb2312"))


def test_gb2312_html_error_page_is_treated_as_failure(monkeypatch):
    """回归：中金所错误页是 **gb2312**，不是 UTF-8（线上实测 2026-09-17）。

    此前 `_http_get_text` 硬编码 `payload.decode("utf-8")`，而错误页的
    `<meta ... charset=gb2312>` 让它必然解不出 UTF-8 → 先抛 UnicodeDecodeError，
    把本该清晰的「HTML 错误页」结论淹没成
    `'utf-8' codec can't decode byte 0xcd in position 252`。

    注意：**失效点只在解码**。实测真实错误页里 `<html` 落在第 123 个字符，
    本来就在 `text[:200]` 窗口内，所以"窗口太窄"不是原因——别误修。
    """
    html = (
        '\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        '<meta http-equiv="Content-Type" content="text/html; charset=gb2312" />\n'
        "<title>网页错误</title></head><body>您要查看的网页可能已被删除、名称已被更改，"
        "或者暂时不可用。</body></html>"
    )
    payload = html.encode("gb2312")
    # 前置断言：这串字节确实解不出 UTF-8，否则本用例会退化成上面那条 UTF-8 用例
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")
    assert payload.find(b"<html") > 0  # `<html>` 存在；实测落在第 123 字节（在 200 窗口内）

    def _fake_urlopen(request, timeout=None):
        class _Response:
            def read(self) -> bytes:
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()

    monkeypatch.setattr(cf.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(cf.CffexError, match="HTML 错误页"):
        cf._http_get_text("http://www.cffex.com.cn/sj/hqsj/rtj/202609/17/index.xml")


# —— 降级入口 ——


def test_try_fetch_snapshot_returns_reason_instead_of_raising(monkeypatch):
    def _boom(trading_day):
        raise cf.CffexError("模拟数据源不可用")

    monkeypatch.setattr(cf, "fetch_index_contracts", _boom)
    snapshot, reason = cf.try_fetch_snapshot(TRADING_DAY)
    assert snapshot is None
    assert "模拟数据源不可用" in reason


def test_try_fetch_snapshot_success_has_empty_reason(monkeypatch):
    contracts = cf.parse_index_contracts(load_youzi_fixture("cffex_index_20260916.xml"))
    monkeypatch.setattr(cf, "fetch_index_contracts", lambda trading_day: contracts)
    snapshot, reason = cf.try_fetch_snapshot(TRADING_DAY)
    assert snapshot is not None
    assert reason == ""
