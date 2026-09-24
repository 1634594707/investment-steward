"""M2-C06/C08 测试：估值取数降级、失败分类与证据口径失败场景（2026-09-15 路线图）。

锁定语义：
- C06 失败三分类：`request_failed`（主源请求失败/冷却）、`empty_data`（主源可达但空数据）、
  `insufficient_sample`（筛选后样本不足，非异常路径，由分位/聚合逻辑以 None 表达）；
- C06 带日期缓存降级：主源失败时回退陈旧缓存必须在**证据文本**注明抓取时点与数据截至，
  禁止冒充实时、禁止无说明拼接不同口径历史（历史/同业序列不做陈旧回退）；
- C06 横截面降级以**整份完整快照**为原子单元（不拼页）；
- C08 失败场景：年度/月度错配（C01）、主题不命中行业不取数、单一行业来源失败不拖垮
  另一来源、SPB 跨月标题解析（连字符）、SSE 未核验 TCE 字段不输出。

本文件全部用合成数据，绝不真实联网。
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from investment_steward_core import industry_evidence as ie
from investment_steward_core import macro_calendar as calendar_engine
from investment_steward_core import valuation_evidence as ve


@pytest.fixture(autouse=True)
def _reset_valuation_state():
    """每个用例前清空估值缓存/冷却/最后完整快照，避免用例间串扰。"""
    ve._cache.clear()
    ve._fail_cache.clear()
    ve._stale_cache.clear()
    ve._last_good_cross_section = None
    yield
    ve._cache.clear()
    ve._fail_cache.clear()
    ve._stale_cache.clear()
    ve._last_good_cross_section = None


# ---------------------------------------------------------------------------
# 合成取数分发器
# ---------------------------------------------------------------------------

_CURRENT_ROW = {
    "SECURITY_CODE": "600519",
    "SECURITY_NAME_ABBR": "贵州茅台",
    "BOARD_CODE": "016165",
    "BOARD_NAME": "白酒Ⅱ",
    "TRADE_DATE": "2026-09-11 00:00:00",
    "CLOSE_PRICE": 1285.13,
    "TOTAL_MARKET_CAP": 1.6065e12,
    "NOTLIMITED_MARKETCAP_A": 1.6065e12,
    "TOTAL_SHARES": 1.25e9,
    "PE_TTM": 19.73,
    "PE_LAR": 19.52,
    "PB_MRQ": 6.39,
    "PS_TTM": 9.27,
    "PCF_OCF_TTM": 13.49,
    "PEG_CAR": -4.76,
}


def _history_rows(count: int = 25) -> list[dict]:
    rows = []
    for i in range(count):
        rows.append({
            "TRADE_DATE": f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "PE_TTM": 10.0 + i,   # 全正数 → 分位可算
            "PB_MRQ": 2.0 + i * 0.1,
            "PS_TTM": 3.0,
            "CLOSE_PRICE": 100.0 + i,
        })
    return rows


def _dispatcher_for(success_columns: set[str]):
    """按 columns 分发的合成 `_get_json`：不在 success_columns 内的请求抛连接错误。"""

    def _fake_get_json(params: dict) -> dict:
        columns = str(params.get("columns") or "")
        if columns not in success_columns:
            # 真实 `_get_json` 契约：网络错误重试后统一包装为 ValuationError（request_failed）。
            raise ve.ValuationError(f"{ve.VALUATION_SOURCE_LABEL}接口请求失败: 合成断连")
        if "PE_LAR" in columns:  # 当前值页
            return {"result": {"data": [dict(_CURRENT_ROW)]}, "success": True}
        if columns == ve._HISTORY_COLUMNS:  # 历史页（25 行 < 500 → 单页即止）
            return {"result": {"data": _history_rows()}, "success": True}
        if columns == ve._PEER_COLUMNS:  # 同业页
            return {
                "result": {
                    "data": [
                        {"SECURITY_CODE": "000858", "SECURITY_NAME_ABBR": "五粮液",
                         "PE_TTM": 15.0, "PB_MRQ": 3.0, "PS_TTM": 5.0,
                         "TRADE_DATE": "2026-09-11"},
                        {"SECURITY_CODE": "600809", "SECURITY_NAME_ABBR": "山西汾酒",
                         "PE_TTM": 22.0, "PB_MRQ": 5.0, "PS_TTM": 7.0,
                         "TRADE_DATE": "2026-09-11"},
                    ]
                },
                "success": True,
            }
        raise AssertionError(f"未预期的 columns: {columns}")

    return _fake_get_json


# ---------------------------------------------------------------------------
# C06-1：失败三分类
# ---------------------------------------------------------------------------


def test_c06_request_failure_classified_as_request_failed(monkeypatch):
    """主源连接失败 → ValuationError.failure_kind == request_failed。"""
    monkeypatch.setattr(ve, "_get_json", _dispatcher_for(set()))
    with pytest.raises(ve.ValuationError) as exc_info:
        ve.fetch_valuation("600519")
    assert exc_info.value.failure_kind == "request_failed"


def test_c06_empty_payload_classified_as_empty_data(monkeypatch):
    """主源可达但返回空数据 → empty_data（与请求失败区分，处置不同）。"""
    monkeypatch.setattr(
        ve, "_get_json",
        lambda params: {"result": {"data": []}, "success": True},
    )
    with pytest.raises(ve.ValuationError) as exc_info:
        ve.fetch_valuation("600519")
    assert exc_info.value.failure_kind == "empty_data"


def test_c06_cross_section_empty_is_empty_data(monkeypatch):
    """横截面该交易日无数据 → empty_data（非请求失败）。"""
    monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
    monkeypatch.setattr(ve, "_query", lambda *a, **k: [])
    with pytest.raises(ve.ValuationError) as exc_info:
        ve.fetch_market_cross_section("2026-09-11")
    assert exc_info.value.failure_kind == "empty_data"


# ---------------------------------------------------------------------------
# C06-2：当前值「带日期缓存」降级（注明时点），历史/同业不降级
# ---------------------------------------------------------------------------


def test_c06_fetch_valuation_falls_back_to_dated_cache_with_note(monkeypatch):
    """第一次全成功留档 → 缓存过期 + 主源仅当前值页断连 → 当前值走带日期缓存。

    验收：快照成功返回且 degraded_note 非空；describe_valuation 文本含时效警告；
    历史/同业不做陈旧回退（各自以 note 如实降级为缺失）。
    """
    # 第 1 步：全部成功（填充 _cache 与 _stale_cache）。
    monkeypatch.setattr(
        ve, "_get_json", _dispatcher_for({ve._CURRENT_COLUMNS, ve._HISTORY_COLUMNS, ve._PEER_COLUMNS})
    )
    first = ve.fetch_valuation("600519")
    assert first.degraded_note == ""
    assert first.pe_ttm == 19.73

    # 第 2 步：90s 缓存过期（置负 TTL），主源对当前值页断连。
    monkeypatch.setattr(ve, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(ve, "_get_json", _dispatcher_for(set()))
    second = ve.fetch_valuation("600519")
    assert second.degraded_note, "当前值应回退带日期缓存并给出降级说明"
    assert "小时" in second.degraded_note and "非实时" in second.degraded_note
    # 历史序列不做陈旧回退（禁止无说明拼接不同口径历史）→ 分位缺失并如实说明。
    assert second.percentiles.get("pe_ttm") is None
    assert "历史序列取数失败" in (second.percentile_note or "")
    text = ve.describe_valuation(second)
    assert "数据时效警告" in text
    assert "截至" in text  # 数据截至时点仍可追溯（TRADE_DATE 在行内）


def test_c06_stale_cache_expired_beyond_7d_does_not_fallback(monkeypatch):
    """陈旧缓存超 7 天直接丢弃（宁缺勿错），照常抛 request_failed。"""
    monkeypatch.setattr(
        ve, "_get_json", _dispatcher_for({ve._CURRENT_COLUMNS})
    )
    # 成功一次留档。
    ve._query(ve._CURRENT_COLUMNS, ve._build_filter(symbol="600519"),
              page_size=1, sort_column="TRADE_DATE", desc=True)
    # 人为把留档时间拨回 8 天前。
    key = (ve._build_filter(symbol="600519"), ve._CURRENT_COLUMNS, 1)
    wall, rows = ve._stale_cache[key]
    ve._stale_cache[key] = (wall - 8 * 24 * 3600.0, rows)
    # 主源断连 + 实时缓存过期。
    monkeypatch.setattr(ve, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(ve, "_get_json", _dispatcher_for(set()))
    with pytest.raises(ve.ValuationError):
        ve.fetch_valuation("600519")


# ---------------------------------------------------------------------------
# C06-3：横截面整份降级（原子单元，不拼页）
# ---------------------------------------------------------------------------


def _cross_rows() -> list[ve.CrossSectionRow]:
    return [
        ve.CrossSectionRow(symbol="600519", name="贵州茅台", board_code="K01",
                           board_name="白酒Ⅱ", total_market_cap=1e12,
                           pe_ttm=19.7, pb_mrq=6.4, ps_ttm=9.3),
        ve.CrossSectionRow(symbol="000858", name="五粮液", board_code="K01",
                           board_name="白酒Ⅱ", total_market_cap=5e11,
                           pe_ttm=15.0, pb_mrq=3.0, ps_ttm=5.0),
        ve.CrossSectionRow(symbol="601318", name="中国平安", board_code="K02",
                           board_name="保险Ⅱ", total_market_cap=9e11,
                           pe_ttm=9.0, pb_mrq=1.2, ps_ttm=2.0),
    ]


def test_c06_cross_section_fallback_uses_dated_snapshot(monkeypatch):
    """主源失败但有完整快照留档 → 整份回退并注明抓取时点与数据截至。"""
    ve._last_good_cross_section = (time.time() - 3600.0, "2026-09-11", _cross_rows())
    monkeypatch.setattr(
        ve, "latest_valuation_trade_date",
        lambda: (_ for _ in ()).throw(ve.ValuationError("合成断连")),
    )
    trade_date, rows, note = ve.cross_section_with_fallback()
    assert trade_date == "2026-09-11"
    assert len(rows) == 3
    assert "降级" in note and "2026-09-11" in note and "小时" in note
    assert "不与实时数据混排" in note


def test_c06_cross_section_fallback_without_snapshot_raises(monkeypatch):
    """无留档（如进程首次启动即失败）→ 不编造，照常抛错进入证据不足状态。"""
    monkeypatch.setattr(
        ve, "latest_valuation_trade_date",
        lambda: (_ for _ in ()).throw(ve.ValuationError("合成断连")),
    )
    with pytest.raises(ve.ValuationError):
        ve.cross_section_with_fallback()


def test_c06_cross_section_success_records_last_good(monkeypatch):
    """完整横截面成功后必须留档（含 trade_date），作为后续降级的原子单元。"""
    monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
    monkeypatch.setattr(ve, "_query", lambda *a, **k: [
        {"SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台",
         "BOARD_CODE": "K01", "BOARD_NAME": "白酒Ⅱ",
         "TOTAL_MARKET_CAP": 1e12, "PE_TTM": 19.7, "PB_MRQ": 6.4, "PS_TTM": 9.3},
    ])
    rows = ve.fetch_market_cross_section("2026-09-11")
    assert len(rows) == 1
    assert ve._last_good_cross_section is not None
    wall, trade_date, recorded = ve._last_good_cross_section
    assert trade_date == "2026-09-11"
    assert len(recorded) == 1
    assert wall > 0


# ---------------------------------------------------------------------------
# C08-1：年度/月度错配（C01 频率隔离的失败场景验证）
# ---------------------------------------------------------------------------


def _elapsed_cn_cpi_events():
    """找一个 cn_cpi 已过发布窗口（elapsed）的日期，返回该日事件。"""
    for offset in range(1, 28):
        today = date(2026, 9, offset)
        events = [
            e for e in calendar_engine.calendar_snapshot(today, days=30).events
            if e.event_key == "cn_cpi" and e.phase == "elapsed"
        ]
        if events:
            return events
    raise AssertionError("30 天内未找到 elapsed 的 cn_cpi 事件（日历窗口定义可能变更）")


def test_c08_annual_reading_never_becomes_monthly_actual():
    """主口径键下挂年度读数（C01 防线）：actual_value 保持 None，年度值只进 annual_background。

    验收（C01/C08）：2025 年年度 CPI 不再被用于说明 2026 年当月 CPI——事件只消费
    `linked_series`，故该键下读数为年度频率时必须被隔离为年度背景。
    """
    readings = {
        "cn:cpi_yoy_imf": {  # cn_cpi 的 linked_series（月度事件的主口径键）
            "latest": 0.2, "obs_date": "2025-12-31", "unit": "%YoY",
            "source": "World Bank FP.CPI.TOTL.ZG", "status": "ok",
            "frequency": "annual",
        },
    }
    events = calendar_engine.attach_actual_values(_elapsed_cn_cpi_events(), readings)
    target = events[0]
    assert target.actual_value is None
    assert target.actual_frequency is None
    assert len(target.annual_background) == 1
    assert target.annual_background[0]["value"] == pytest.approx(0.2)


def test_c08_monthly_reading_writes_actual_and_annual_does_not_leak():
    """月度主口径读数写 actual_*（记频率）；未关联的年度读数绝不漏进实际值/并列口径。"""
    readings = {
        "cn:cpi_yoy_imf": {  # 月度主口径（FRED IMF 月度 CPI）
            "latest": 0.4, "obs_date": "2026-07-01", "unit": "%YoY",
            "source": "FRED CHNCPIALLMINMEI", "status": "ok",
            "frequency": "monthly",
        },
        "cn:cpi_yoy": {
            "latest": 0.2, "obs_date": "2025-12-31", "unit": "%YoY",
            "source": "World Bank FP.CPI.TOTL.ZG", "status": "ok",
            "frequency": "annual",
        },
    }
    events = calendar_engine.attach_actual_values(_elapsed_cn_cpi_events(), readings)
    target = events[0]
    assert target.actual_value == pytest.approx(0.4)
    assert target.actual_frequency == "monthly"
    # 年度值（0.2）不得出现在实际值或并列口径（口径错配零容忍）。
    assert target.actual_value != pytest.approx(0.2)
    assert all(
        item.get("value") != pytest.approx(0.2) for item in target.actual_variants
    )


# ---------------------------------------------------------------------------
# C08-2：行业证据失败场景（部分行业无数据 / 单一来源失败隔离）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_industry_state():
    ie._express_cache = None
    ie._express_fail_until = 0.0
    ie._shipping_cache = None
    ie._shipping_fail_until = 0.0
    yield
    ie._express_cache = None
    ie._express_fail_until = 0.0
    ie._shipping_cache = None
    ie._shipping_fail_until = 0.0


def test_c08_topic_without_industry_keywords_fetches_nothing(monkeypatch):
    """主题不命中快递/航运关键词 → 不取数、无条目也无失败（不空转打源站）。"""

    def _boom(*a, **k):
        raise AssertionError("主题未命中不应发起任何行业取数")

    monkeypatch.setattr(ie, "fetch_express_evidence", _boom)
    monkeypatch.setattr(ie, "fetch_shipping_indices", _boom)
    items, failures = ie.direction_industry_evidence("银行股被低估了吗")
    assert items == []
    assert failures == []


def test_c08_single_industry_source_failure_isolated(monkeypatch):
    """快递源失败、航运源正常 → 航运条目保留，快递失败进 failures（不静默、不互相拖垮）。"""
    monkeypatch.setattr(
        ie, "fetch_express_evidence", lambda *a, **k: {"ok": False, "detail": "合成失败"}
    )
    monkeypatch.setattr(
        ie, "fetch_shipping_indices",
        lambda *a, **k: {
            "ok": True,
            "publisher": "上海航运交易所官方接口（合成）",
            "frequency": "weekly",
            "retrieved_at": "2026-09-15T00:00:00+00:00",
            "indices": [{
                "name": "SCFI（合成）", "previous": 100.0, "previous_date": "2026-09-04",
                "current": 110.0, "current_date": "2026-09-11", "change": 10.0, "unit": "点",
            }],
            "partial_failures": [],
            "coverage": {
                "required": ["SCFI（集运）"], "available": ["SCFI（合成）"],
                "missing": [], "observation_period": "2026-09-04 ~ 2026-09-11",
                "missing_note": "",
            },
            "note": "合成数据",
        },
    )
    items, failures = ie.direction_industry_evidence("快递与海运")
    assert len(items) == 1
    assert "SCFI" in items[0]["content"]
    assert len(failures) == 1
    assert "快递" in failures[0] and "合成失败" in failures[0]


# ---------------------------------------------------------------------------
# C08-3：SPB 跨月标题解析回归（连字符曾致最新公告漏配）+ 覆盖率对齐
# ---------------------------------------------------------------------------


_SPB_LIST_HTML = """
<ul>
  <li><a href="/gjyzj/c100015/c100016/202608/abc.shtml" TARGET='_blank'>
    <img src="/x.png" /><p>国家邮政局公布2026年1-7月邮政行业运行情况</p>
    <span>2026-08-14</span></a></li>
  <li><a href="/gjyzj/c100015/c100016/202601/def.shtml" TARGET='_blank'>
    <p>国家邮政局公布2025年邮政行业运行情况</p><span>2026-01-22</span></a></li>
</ul>
"""

_SPB_ARTICLE_HTML = """
<html><body>
<p>7月份，快递业务量完成170.8亿件，同比增长4.1%</p>
<p>7月份，快递业务收入完成1303.7亿元，同比增长8.1%</p>
<p>快递业务量累计完成1174.7亿件，同比增长4.8%</p>
<p>快递业务收入累计完成9017.7亿元，同比增长7.4%</p>
<p>快递业务收入品牌集中度指数CR8为87.0</p>
<p>日期：2026-08-14</p>
</body></html>
"""


def test_c08_spb_selects_latest_cross_month_report_and_coverage_aligned(monkeypatch):
    """跨月标题（含连字符）必须可解析并按发布日期选中最新公告；required 与 metrics 全名对齐。"""

    def _fake_http_get_text(url: str) -> str:
        if "common_list" in url:
            return _SPB_LIST_HTML
        if url.endswith("202608/abc.shtml"):
            return _SPB_ARTICLE_HTML
        raise AssertionError(f"不应请求其它页面: {url}")

    monkeypatch.setattr(ie, "_http_get_text", _fake_http_get_text)
    payload = ie.fetch_express_evidence(force_refresh=True)
    assert payload["ok"] is True
    assert payload["period_label"] == "2026年1-7月"   # 非 2025 年报（连字符回归）
    assert payload["release_date"] == "2026-08-14"
    names = {item["name"] for item in payload["metrics"]}
    assert {
        "快递业务量", "快递业务收入", "单票收入（推算）",
        "快递业务收入品牌集中度 CR8",
    } <= names
    # 单票收入可追溯：1303.7 / 170.8 = 7.633（分子分母同源同月）。
    unit_price = next(m for m in payload["metrics"] if m["name"] == "单票收入（推算）")
    assert unit_price["value"] == pytest.approx(7.633, abs=1e-3)
    assert "1303.7" in unit_price["derivation"] and "170.8" in unit_price["derivation"]
    # C07 覆盖率：全名对齐后不再误报缺失。
    assert payload["coverage"]["missing"] == []
    assert payload["coverage"]["observation_period"] == "2026年1-7月"


# ---------------------------------------------------------------------------
# C08-4：SSE 未核验 TCE 字段结构性不输出（不猜口径）
# ---------------------------------------------------------------------------


def test_c08_sse_never_outputs_unverified_tce_fields(monkeypatch):
    """源数据混入 *_TCE 字段也不得输出（输出键集合结构性限定为核验过的子序列）。"""
    fake = {
        "scfi": {"curIndex": {"t": 3662.18}, "lastIndex": {"t": 3590.05},
                 "curDate": "2026-09-11", "lastDate": "2026-09-04"},
        "ctfi": {"curIndex": {"t": 13691.4, "ct1_WS": 1001.56, "VLCC_TCE": 99999.0},
                 "lastIndex": {"t": 11403.99, "ct1_WS": 818.8, "VLCC_TCE": 88888.0},
                 "curDate": "2026-09-14", "lastDate": "2026-09-11"},
    }
    monkeypatch.setattr(ie, "_fetch_sse_index", lambda t: fake[t])
    payload = ie.fetch_shipping_indices(force_refresh=True)
    assert payload["ok"] is True
    names = [item["name"] for item in payload["indices"]]
    assert all("TCE" not in name for name in names), names
    ctfi_ws = next(i for i in payload["indices"] if "WS" in i["unit"])
    assert ctfi_ws["current"] == pytest.approx(1001.56)
    assert ctfi_ws["previous"] == pytest.approx(818.8)
