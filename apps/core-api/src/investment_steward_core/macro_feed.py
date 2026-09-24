"""宏观数据管道（M4 + M4.1）。

- 数据源只走公开接口：美国实拉 FRED（api.stlouisfed.org，key 存 Core 凭据库，D-11）；
  中/欧/日/印自 M4.1 起实拉世界银行公开 API（api.worldbank.org，无 key，年更口径）；
  其余未确认来源（PMI/社融/政策利率/金价/CFETS 等）一律 `pending`，绝不编造 series_id 或数值（ADR-0006）。
- 每条序列缓存于本机 `macro_cache`（series_key = "{region}:{indicator}"），payload 带
  as_of 与 dataset_version（= "fred:{series_id}:{obs日}" 或 "worldbank:{iso3}:{indicator}:{年份}"），任意历史可追溯。
- 美国 CPI 同比由 CPIAUCSL 指数 12 期差分换算，note 明示口径；世界银行 CPI 序列本身即同比，无需换算。
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from investment_steward_core import feed_health
from investment_steward_core.domain import MacroBackgroundRow, MacroIndicatorReading, MacroSnapshot

if TYPE_CHECKING:
    from investment_steward_core.credential_store import CredentialStore
    from investment_steward_core.storage import Database

FRED_API = "https://api.stlouisfed.org/fred/series/observations"
WORLD_BANK_API = "https://api.worldbank.org/v2/country"
_USER_AGENT = "investment-steward-core/0.1 (macro-radar; local-first)"

# 世界银行 ISO3 编码（M4.1）：eu 用 EMU（欧元区整体口径）。
WB_ISO3: dict[str, str] = {"cn": "CHN", "eu": "EMU", "jp": "JPN", "in": "IND"}

# 缓存 TTL（用户要求：落库缓存、不反复拉取）：
#   FRED 月度/日度序列 12h 过期重拉一次；世界银行年更序列 7 天。
#   过期重拉失败时回退旧缓存（as_of 如实标注陈旧，不丢弃真实数据）——见 _resolve_series。
FRED_TTL_HOURS = 12.0
WORLDBANK_TTL_HOURS = 168.0

REGIONS: dict[str, str] = {
    "us": "美国",
    "cn": "中国",
    "eu": "欧元区",
    "jp": "日本",
    "in": "印度",
}


def _pending(indicator: str, label: str, dim: str, note: str) -> MacroIndicatorReading:
    return MacroIndicatorReading(indicator=indicator, label=label, dim=dim, status="pending", note=note)


def _region_indicator_defs(region: str) -> list[dict[str, str]]:
    """各国首批指标清单；`fred` 键存在且非空才实拉，其余来源 pending（不编造 series_id）。

    C01（2026-09-15 路线图）：每个定义带 `frequency`（monthly/annual/daily）——
    观测期隔离：年更年度值只作年度背景，不得被月度发布事件当作「当月实际值」。
    """
    if region == "us":
        return [
            {"indicator": "nfp", "label": "非农就业", "dim": "employment", "fred": "PAYEMS",
             "unit": "千人", "frequency": "monthly",
             "note": "FRED PAYEMS，季调后非农薪人数；观测月为数据所属月份，次月初发布并含后续修正（如 8 月数据 9 月初发布，自动更新，无需人工提供）"},
            {"indicator": "unemployment", "label": "失业率", "dim": "employment", "fred": "UNRATE",
             "unit": "%", "frequency": "monthly",
             "note": "FRED UNRATE，16 岁以上失业率（月度）"},
            {"indicator": "cpi_yoy", "label": "CPI 同比（季调口径）", "dim": "inflation", "fred": "CPIAUCSL",
             "unit": "%YoY", "frequency": "monthly",
             "note": "由 FRED CPIAUCSL 城市消费者 CPI 指数（**季调**）12 期差分换算；与 BLS 公布的未季调 headline 存在季节性偏差，需要与官方口径对照时请看「CPI 同比（未季调口径）」"},
            {"indicator": "cpi_yoy_nsa", "label": "CPI 同比（未季调口径）", "dim": "inflation", "fred": "CPIAUCNS",
             "unit": "%YoY", "frequency": "monthly",
             "note": "由 FRED CPIAUCNS 城市消费者 CPI 指数（**未季调**）12 期差分换算；此为与 BLS 官方 headline CPI 同比对齐的口径（BLS 发布的是未季调 headline）"},
            {"indicator": "fedfunds", "label": "联邦基金利率", "dim": "monetary", "fred": "FEDFUNDS",
             "unit": "%", "frequency": "monthly",
             "note": "FRED FEDFUNDS，有效联邦基金利率（月均）"},
            {"indicator": "yield_10y2y", "label": "10y-2y 利差", "dim": "other", "fred": "T10Y2Y",
             "unit": "pp", "frequency": "daily",
             "note": "FRED T10Y2Y，期限利差（日度，取最近观测）"},
        ]
    if region == "cn":
        return [
            # C01：中国月度 CPI（IMF via FRED CHNCPIALLMINMEI，2026-09-15 实测可得，
            # 最新观测约滞后当期 1 年+；obs_date 如实标注数据所属月份，不以旧观测冒充当月值）。
            {"indicator": "cpi_yoy_imf", "label": "CPI 同比（IMF 月度口径）", "dim": "inflation", "fred": "CHNCPIALLMINMEI",
             "unit": "%YoY", "frequency": "monthly",
             "note": "由 FRED CHNCPIALLMINMEI（IMF 中国 CPI 指数，月度）12 期差分换算；发布滞后，obs_date 为数据所属月份；与国家统计局公布的同比口径可能存在差异，官方值以统计局为准"},
            {"indicator": "cpi_yoy", "label": "CPI 同比（年度，世界银行）", "dim": "inflation", "wb": "FP.CPI.TOTL.ZG",
             "unit": "%YoY", "frequency": "annual",
             "note": "世界银行 FP.CPI.TOTL.ZG（**年更年度值**，截至上年）；仅作年度背景，不可用于说明当月 CPI"},
            {"indicator": "unemployment_ilo", "label": "失业率（ILO 估算）", "dim": "employment", "wb": "SL.UEM.TOTL.ZS",
             "unit": "%", "frequency": "annual",
             "note": "世界银行 ILO 模型估算口径（年更），与国家统计局城镇调查口径不同，不可直接对比"},
            {"indicator": "manufacturing_pmi", "label": "制造业 PMI", "dim": "growth", "em": "RPT_ECONOMY_PMI|MAKE_INDEX",
             "unit": "%", "frequency": "monthly",
             "note": "东财数据中心 RPT_ECONOMY_PMI，制造业 PMI（荣枯线 50，月度）"},
            {"indicator": "non_manufacturing_pmi", "label": "非制造业 PMI", "dim": "growth", "em": "RPT_ECONOMY_PMI|NMAKE_INDEX",
             "unit": "%", "frequency": "monthly",
             "note": "东财数据中心 RPT_ECONOMY_PMI，非制造业 PMI（活动指数口径）"},
            {"indicator": "survey_unemployment", "label": "城镇调查失业率", "dim": "employment",
             "note": "待接数据源（国家统计局），后续适配器接入；东财数据中心 api/data/v1 盲探 4 组参数均报「url格式异常」，需浏览器 devtools 抓 data.eastmoney.com/cjsj/shrzgm.html 真实 XHR 后适配（2026-09-06 探测记录）"},
            {"indicator": "tsf", "label": "社融增量", "dim": "monetary",
             "note": "待接数据源（央行），后续适配器接入"},
        ]
    if region == "eu":
        return [
            {"indicator": "hicp_yoy", "label": "HICP 同比", "dim": "inflation", "wb": "FP.CPI.TOTL.ZG",
             "unit": "%YoY", "frequency": "annual",
             "note": "世界银行 FP.CPI.TOTL.ZG，EMU 基于 HICP（年更）；仅作年度背景，不可用于说明当月 HICP"},
            {"indicator": "unemployment", "label": "失业率", "dim": "employment", "wb": "SL.UEM.TOTL.ZS",
             "unit": "%", "frequency": "annual",
             "note": "世界银行 ILO 口径，EMU 整体（年更）"},
            {"indicator": "composite_pmi", "label": "综合 PMI", "dim": "growth",
             "note": "待接数据源（S&P Global），后续适配器接入"},
            {"indicator": "deposit_rate", "label": "存款利率", "dim": "monetary", "fred": "ECBDFR",
             "unit": "%", "frequency": "daily",
             "note": "FRED ECBDFR，欧央行存款便利利率（日度）"},
        ]
    if region == "jp":
        return [
            # 日本 FRED 月度序列 JPNCPIALLMINMEI 实测只有 6 期历史观测（不足 13 期差分），
            # 不可用（2026-09-15 探测记录）；月度值未接入前只提供年度背景。
            {"indicator": "cpi_yoy", "label": "CPI 同比（年度，世界银行）", "dim": "inflation", "wb": "FP.CPI.TOTL.ZG",
             "unit": "%YoY", "frequency": "annual",
             "note": "世界银行 FP.CPI.TOTL.ZG（**年更年度值**，截至上年）；仅作年度背景，不可用于说明当月 CPI"},
            {"indicator": "unemployment", "label": "失业率", "dim": "employment", "wb": "SL.UEM.TOTL.ZS",
             "unit": "%", "frequency": "annual",
             "note": "世界银行 ILO 口径（年更），与总务省月度口径存在差异"},
            {"indicator": "shunto", "label": "春斗工资涨幅", "dim": "employment",
             "note": "年度数据，待接数据源（连合），后续适配器接入"},
            {"indicator": "policy_rate", "label": "政策利率（代理）", "dim": "monetary", "fred": "IR3TIB01JPM156N",
             "unit": "%", "frequency": "monthly",
             "note": "FRED IR3TIB01JPM156N，日本 3 个月期银行间利率（月度，OECD 口径）作政策利率代理"},
        ]
    if region == "in":
        return [
            {"indicator": "gdp_yoy", "label": "GDP 同比", "dim": "growth", "wb": "NY.GDP.MKTP.KD.ZG",
             "unit": "%YoY", "frequency": "annual",
             "note": "世界银行 NY.GDP.MKTP.KD.ZG（年更，截至上年）"},
            # C01：印度月度 CPI（IMF via FRED INDCPIALLMINMEI，2026-09-15 实测可得，最新观测 2025-03）。
            {"indicator": "cpi_yoy_imf", "label": "CPI 同比（IMF 月度口径）", "dim": "inflation", "fred": "INDCPIALLMINMEI",
             "unit": "%YoY", "frequency": "monthly",
             "note": "由 FRED INDCPIALLMINMEI（IMF 印度 CPI 指数，月度）12 期差分换算；发布滞后，obs_date 为数据所属月份；与 MoSPI 公布口径可能存在差异"},
            {"indicator": "cpi_yoy", "label": "CPI 同比（年度，世界银行）", "dim": "inflation", "wb": "FP.CPI.TOTL.ZG",
             "unit": "%YoY", "frequency": "annual",
             "note": "世界银行 FP.CPI.TOTL.ZG（**年更年度值**，截至上年）；仅作年度背景，不可用于说明当月 CPI"},
            {"indicator": "unemployment", "label": "失业率（ILO 估算）", "dim": "employment", "wb": "SL.UEM.TOTL.ZS",
             "unit": "%", "frequency": "annual",
             "note": "世界银行 ILO 模型估算口径（年更），与 NSO 月度调查口径不同"},
            {"indicator": "manufacturing_pmi", "label": "制造业 PMI", "dim": "growth",
             "note": "待接数据源（S&P Global），后续适配器接入"},
        ]
    return []


def _background_defs() -> list[dict[str, str]]:
    """背景层定义（货币指数 / 油价 / 金价）：FRED 可稳定取得的实拉，其余 pending。"""
    return [
        {"key": "dxy_twi", "label": "广义贸易加权美元", "fred": "DTWEXBGS", "unit": "指数",
         "note": "FRED DTWEXBGS，美元对广泛贸易伙伴货币指数（日度）"},
        {"key": "wti", "label": "WTI 现货", "fred": "DCOILWTICO", "unit": "USD/bbl",
         "note": "EIA via FRED DCOILWTICO（日度）"},
        {"key": "brent", "label": "布伦特现货", "fred": "DCOILBRENTEU", "unit": "USD/bbl",
         "note": "EIA via FRED DCOILBRENTEU（日度）"},
        {"key": "gold", "label": "黄金现货（XAU）", "okx": "XAUT-USDT", "unit": "美元/盎司",
         "note": "OKX 公开行情 XAUT-USDT（1 token = 1 盎司伦敦金）；需本机可连 OKX（VPN），失败自动回退待接。LBMA 官方定盘授权申请中，获批后替换"},
        {"key": "cfets_rmb", "label": "人民币 CFETS 指数", "note": "待接数据源（中国外汇交易中心公开），M4.1 接入"},
        {"key": "eur_fx", "label": "欧元兑美元", "fred": "DEXUSEU", "unit": "USD/EUR",
         "note": "FRED DEXUSEU（日度）；BIS 有效汇率（贸易加权口径）待接"},
        {"key": "jpy_fx", "label": "日元兑美元", "fred": "DEXJPUS", "unit": "USD/JPY",
         "note": "FRED DEXJPUS（日度）；日元口径为 1 美元兑日元，方向与欧元相反"},
    ]


# 事件影响层（用户需求定稿：俄乌→粮食/化肥/能源，美伊→原油/航运）：
# 只呈现「对中国投资者可见的影响链」大宗商品，不呈现冲突方国内数据（乌克兰 GDP / 伊朗 CPI 已移除）。
# FRED IMF 全球商品价格序列（PURSI/PWHEAMT/PMAIZMT），其余 pending 不编造。
def _event_impact_defs() -> dict[str, dict[str, Any]]:
    return {
        "ukraine": {
            "title": "俄乌战争 · 粮食与化肥",
            "rows": [
                {"key": "wheat", "label": "小麦", "fred": "PWHEAMTUSDM", "unit": "美元/吨",
                 "note": "FRED PWHEAMTUSDM，黑海粮食通道是主要小麦出口线（IMF 月度）"},
                {"key": "corn", "label": "玉米", "fred": "PMAIZMTUSDM", "unit": "美元/吨",
                 "note": "FRED PMAIZMTUSDM，玉米全球价（IMF 月度）"},
                {"key": "soybeans", "label": "大豆", "fred": "PSOYBUSDM", "unit": "美元/吨",
                 "note": "FRED PSOYBUSDM，油籽/饲料链（IMF 月度）"},
                {"key": "eu_gas", "label": "欧洲天然气（TTF）", "fred": "PNGASEUUSDM", "unit": "美元/百万英热",
                 "note": "FRED PNGASEUUSDM，能源脱钩推升欧洲用能与氮肥成本"},
                {"key": "urea", "label": "尿素（氮肥）", "unit": "美元/吨",
                 "note": "待接数据源（世行 Pink Sheet，FRED 无对应序列），后续适配器接入"},
            ],
        },
        "iran": {
            "title": "美伊战争 · 原油与航运",
            "rows": [
                {"key": "brent", "label": "布伦特原油", "fred": "DCOILBRENTEU", "unit": "美元/桶",
                 "note": "EIA via FRED DCOILBRENTEU（日度）"},
                {"key": "wti", "label": "WTI 原油", "fred": "DCOILWTICO", "unit": "美元/桶",
                 "note": "EIA via FRED DCOILWTICO（日度）"},
                {"key": "dry_freight", "label": "干散货运费（BDI）", "unit": "点",
                 "note": "待接数据源（波罗的海交易所公开），后续适配器接入"},
            ],
        },
    }


MANUAL_MACRO_FILE = Path(__file__).parent / "data" / "manual_macro.json"


def get_manual_series(series_key: str) -> dict[str, Any] | None:
    """手动补录层（data/manual_macro.json）：接口未接通时的官方公开值,来源如实标注。

    与 macro_cache 同构;编辑 JSON 后重启 Core 生效。缺失/解析失败返回 None（不影响管道）。
    """
    try:
        data = json.loads(MANUAL_MACRO_FILE.read_text(encoding="utf-8"))
        payload = data.get(series_key)
        if payload and payload.get("observations"):
            return payload
    except (OSError, ValueError):
        pass
    return None


EM_MACRO_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def fetch_em_macro_series(series_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """东财数据中心宏观数据（公开接口，无需 key）。series_id = "{report}|{value_field}"。

    已验证报表：RPT_ECONOMY_PMI（制造业 MAKE_INDEX / 非制造业 NMAKE_INDEX，月度）。
    社融/失业率对应报表不存在（EM 社融页已废弃，HTML 内路径变量为空，2026-09-06 实测）。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    report, _, value_field = series_id.partition("|")
    params = {
        "reportName": report, "columns": "ALL", "pageNumber": "1", "pageSize": str(limit),
        "sortTypes": "-1", "sortColumns": "REPORT_DATE", "source": "WEB", "client": "WEB",
    }
    url = f"{EM_MACRO_API}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # F02：东财宏观取数埋点（此路径无重试，attempt_index 恒 1）。
        with feed_health.attempt(
            feed_health.SOURCE_MACRO_EASTMONEY, url, {"report": report, "value_field": value_field}
        ):
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"东财宏观拉取失败 {report}: {error}") from error
    if not payload.get("success"):
        raise RuntimeError(f"东财宏观 {report}: {payload.get('message')}")
    rows = payload.get("result", {}).get("data") or []
    observations = []
    for item in rows:
        raw = item.get(value_field)
        if raw is None:
            continue  # 缺失不编造
        observations.append({"obs_date": str(item.get("REPORT_DATE", ""))[:10], "value": round(float(raw), 2)})
    return observations


def get_release_board(db: Database, credential: CredentialStore) -> dict[str, Any]:
    """经济数据发布看板（用户需求：CPI/非农/议息——前值、预期、实际）。

    前值/实际：FRED 序列直接计算（同一 _resolve_series 管道，落库缓存）；
    市场预期（consensus）：FRED 无此数据，第三方源（investing.com / ForexFactory）未接入——
    expectation 恒为 None 并全局注明，不编造；密歇根通胀预期（MICH）作为免费的官方预期代理单独成行。
    FOMC 会期需人工维护官方日程（写错即编造），本版只给实际利率不给会期。
    """
    defs: list[dict[str, Any]] = [
        {"key": "us_cpi", "label": "美国 CPI（同比）", "fred": "CPIAUCSL", "transform": "cpi_yoy", "unit": "%",
         "release_note": "BLS 每月中旬发布上月 CPI（日程以 BLS 官网为准）"},
        {"key": "us_nfp", "label": "美国非农（月增）", "fred": "PAYEMS", "transform": "diff", "unit": "千人",
         "release_note": "BLS 每月第一个周五发布上月就业报告；月增 = 当月 − 上月水平值"},
        {"key": "us_ffr", "label": "联邦基金利率（月均）", "fred": "FEDFUNDS", "unit": "%",
         "release_note": "美联储 H.15；议息决议直接影响该利率，会期日程以美联储官网为准（本版不预填）"},
        {"key": "us_infl_exp", "label": "密歇根通胀预期（一年期）", "fred": "MICH", "unit": "%",
         "release_note": "密歇根大学月度调查（FRED MICH）：免费的「市场通胀预期」官方代理"},
    ]
    rows: list[dict[str, Any]] = []
    has_key = bool(credential.get("macro_data_key"))
    for definition in defs:
        payload = _resolve_series(
            db, credential, f"release:{definition['key']}", definition["fred"],
            transform=definition.get("transform", "none"), ttl_hours=FRED_TTL_HOURS,
        )
        if payload:
            obs = payload["observations"]
            previous = obs[1] if len(obs) > 1 else None
            rows.append({
                "key": definition["key"], "label": definition["label"], "status": "ok",
                "actual": obs[0]["value"], "previous": previous["value"] if previous else None,
                "obs_date": obs[0]["obs_date"], "unit": definition["unit"],
                "as_of": payload["as_of"], "dataset_version": payload["dataset_version"],
                "expectation": None, "release_note": definition["release_note"],
            })
        else:
            reason = "凭据库无 FRED key" if not has_key else "拉取失败，稍后重试"
            rows.append({
                "key": definition["key"], "label": definition["label"], "status": "pending",
                "unit": definition["unit"], "expectation": None,
                "release_note": f"{definition['release_note']}；{reason}",
            })
    return {
        "rows": rows,
        "expectation_note": "市场预期（consensus）来自第三方预期源（investing.com / ForexFactory 等），尚未接入；预期列暂缺是明示而非遗漏，不编造。",
    }
def get_event_impacts(db: Database, credential: CredentialStore) -> list[dict[str, Any]]:
    """事件影响快照：与国别快照同一套 `_resolve_series` 管道（macro_cache 落库 + TTL + 失败回退）。

    返回 [{event_id, title, rows: [{key,label,status,latest?,obs_date?,unit?,as_of?,dataset_version?,note}]}]；
    pending 行不输出数值（不编造，ADR-0006）。
    """
    events: list[dict[str, Any]] = []
    has_key = bool(credential.get("macro_data_key"))
    for event_id, spec in _event_impact_defs().items():
        rows: list[dict[str, Any]] = []
        for definition in spec["rows"]:
            if not definition.get("fred"):
                rows.append({
                    "key": definition["key"], "label": definition["label"],
                    "status": "pending", "unit": definition.get("unit", ""), "note": definition["note"],
                })
                continue
            payload = _resolve_series(db, credential, f"event:{definition['key']}", definition["fred"], ttl_hours=FRED_TTL_HOURS)
            if payload:
                latest = payload["observations"][0]
                rows.append({
                    "key": definition["key"], "label": definition["label"], "status": "ok",
                    "latest": latest["value"], "obs_date": latest["obs_date"], "unit": definition["unit"],
                    "as_of": payload["as_of"], "dataset_version": payload["dataset_version"], "note": definition["note"],
                })
            else:
                reason = "凭据库无 FRED key（设置 → 数据源与密钥录入）" if not has_key else "拉取失败，稍后重试"
                rows.append({
                    "key": definition["key"], "label": definition["label"], "status": "pending",
                    "unit": definition.get("unit", ""), "note": f"{definition['note']}；{reason}",
                })
        events.append({"event_id": event_id, "title": spec["title"], "rows": [r for r in rows if r["status"] == "ok"]})
    return events


def fetch_fred_series(series_id: str, api_key: str, limit: int = 14) -> list[dict[str, Any]]:
    """实拉 FRED 观测（最新在前）；网络/鉴权/解析失败抛 RuntimeError，由调用方降级。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = (
        f"{FRED_API}?{urllib.parse.urlencode({
            'series_id': series_id,
            'api_key': api_key,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': str(limit),
        })}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # F02：FRED 取数埋点（api_key 不入指纹——只记 series_id 与 limit）。
        with feed_health.attempt(
            feed_health.SOURCE_MACRO_FRED, FRED_API, {"series_id": series_id, "limit": int(limit)}
        ):
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"FRED 拉取失败 {series_id}: {error}") from error
    observations = []
    for item in payload.get("observations", []):
        raw = str(item.get("value", "")).strip()
        if raw in {"", ".", "NA"}:
            continue  # FRED 用 "." 标缺失，跳过不编造
        observations.append({"obs_date": str(item.get("date", "")), "value": float(raw)})
    return observations


def fetch_worldbank_series(iso3: str, indicator_id: str) -> list[dict[str, Any]]:
    """实拉世界银行观测（无 key 公开接口，最新年在前）；失败抛 RuntimeError 由调用方降级。

    响应结构（已实测确认）：[meta, rows[]]，rows 项含 date（年份字符串）/ value（数值或 null）。
    value 为 null 表示该年无数据，跳过不编造。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{WORLD_BANK_API}/{iso3}/indicator/{indicator_id}?{urllib.parse.urlencode({'format': 'json', 'per_page': '8'})}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # F02：世界银行取数埋点。
        with feed_health.attempt(
            feed_health.SOURCE_MACRO_WORLDBANK, WORLD_BANK_API, {"iso3": iso3, "indicator": indicator_id}
        ):
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(f"世界银行拉取失败 {iso3}/{indicator_id}: {error}") from error
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], list) else []
    observations: list[dict[str, Any]] = []
    for item in rows:
        value = item.get("value")
        if value is None:
            continue  # 该年无数据，跳过不编造
        observations.append({"obs_date": str(item.get("date", "")), "value": round(float(value), 2)})
    return observations


OKX_API = "https://www.okx.com/api/v5/market/candles"
_OKX_FAIL_COOLDOWN_MINUTES = 10
_source_fail_until: dict[str, datetime] = {}


def fetch_okx_series(inst_id: str, limit: int = 15) -> list[dict[str, Any]]:
    """OKX 公开行情（无需密钥）：XAUT-USDT 每 token = 1 盎司伦敦金，作金价现货代理。

    本机需可直连 OKX（VPN/代理场景，urllib 自动遵循 HTTPS_PROXY 环境变量）；
    失败记入 10 分钟冷却，期间直接回退缓存/待接，不拖慢页面。ADR-0004 口径：公开接口、本机个人使用。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    fail_until = _source_fail_until.get(inst_id)
    if fail_until and datetime.now(UTC) < fail_until:
        raise RuntimeError(f"OKX {inst_id} 冷却中（上次失败）")
    url = f"{OKX_API}?{urllib.parse.urlencode({'instId': inst_id, 'bar': '1D', 'limit': str(limit)})}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # F02：OKX 取数埋点（冷却期直接抛出的调用**不产生**尝试记录——没有发请求就不算一次采集）。
        with feed_health.attempt(
            feed_health.SOURCE_MACRO_OKX, OKX_API, {"inst_id": inst_id, "bar": "1D", "limit": int(limit)}
        ):
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as error:
        _source_fail_until[inst_id] = datetime.now(UTC) + timedelta(minutes=_OKX_FAIL_COOLDOWN_MINUTES)
        raise RuntimeError(f"OKX 拉取失败 {inst_id}: {error}") from error
    rows = payload.get("data") or []
    observations = [
        {"obs_date": datetime.fromtimestamp(int(item[0]) / 1000, UTC).date().isoformat(), "value": float(item[4])}
        for item in rows
        if str(item[4]).strip() not in {"", "."}
    ]
    if not observations:
        _source_fail_until[inst_id] = datetime.now(UTC) + timedelta(minutes=_OKX_FAIL_COOLDOWN_MINUTES)
        raise RuntimeError(f"OKX {inst_id} 未返回观测")
    return observations


def _cpi_yoy(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CPI 指数 → 同比（12 期差分）；观测不足 13 期返回空（不输出半截结论）。"""
    ordered = sorted(observations, key=lambda row: row["obs_date"])
    out: list[dict[str, Any]] = []
    for index in range(12, len(ordered)):
        base = ordered[index - 12]["value"]
        current = ordered[index]["value"]
        if base == 0:
            continue
        out.append({"obs_date": ordered[index]["obs_date"], "value": round((current / base - 1) * 100, 1)})
    return list(reversed(out))


def _resolve_series(
    db: Database,
    credential: CredentialStore,
    series_key: str,
    series_id: str,
    *,
    transform: str = "none",
    source: str = "fred",
    ttl_hours: float | None = None,
) -> dict[str, Any] | None:
    """取一条序列：先缓存，缺失/过期时实拉（FRED 需 key；世界银行无 key）并落缓存。返回缓存 payload 或 None。

    source="worldbank" 时 series_id 编码为 "{iso3}|{indicator_id}"。
    ttl_hours 传入时缓存按 as_of 判过期（市场定价层日度序列用 24h）；
    过期后重拉失败时回退旧缓存（as_of 如实标注陈旧，不丢弃真实数据）。
    """
    # §9.1 缓存键含请求参数：transform 改变输出语义（diff/cpi_yoy），必须进键，
    # 否则同一序列的不同变换会互相覆盖污染（真 bug：569 行调用点 transform 可变）。
    cache_key = series_key if transform == "none" else f"{series_key}:t:{transform}"
    cached = db.get_macro_series(cache_key)
    expired = False
    if cached is not None and ttl_hours is not None:
        try:
            fetched_at = datetime.fromisoformat(str(cached.get("as_of", "")))
            expired = (datetime.now(UTC) - fetched_at).total_seconds() >= ttl_hours * 3600
        except ValueError:
            expired = True  # as_of 不可解析视为过期
    if cached is not None and not expired:
        return cached
    if cached is None:
        manual = get_manual_series(series_key)
        if manual:
            # §9.1 一致性：manual 注册表按原 series_key 存放水平值；调用方要求变换时，
            # 必须先应用同一变换再返回，否则 diff/cpi_yoy 层会拿到未变换数据。
            observations = list(manual.get("observations", []))
            if transform == "cpi_yoy":
                observations = _cpi_yoy(observations)
            elif transform == "diff":
                ordered = sorted(observations, key=lambda row: row["obs_date"], reverse=True)
                observations = [
                    {"obs_date": row["obs_date"], "value": round(row["value"] - nxt["value"], 1)}
                    for row, nxt in zip(ordered, ordered[1:])
                ]
            return {**manual, "observations": observations}
    if source == "worldbank":
        iso3, _, indicator_id = series_id.partition("|")
        try:
            observations = fetch_worldbank_series(iso3, indicator_id)
        except RuntimeError:
            return cached if expired else None  # 拉取失败：过期回退旧缓存，缺失不编造
        version = f"worldbank:{iso3}:{indicator_id}:{observations[0]['obs_date']}" if observations else ""
    elif source == "em_macro":
        try:
            observations = fetch_em_macro_series(series_id, limit=15)
        except RuntimeError:
            return cached if expired else None  # 拉取失败：过期回退旧缓存，缺失不编造
        version = f"em:{series_id}:{observations[0]['obs_date']}" if observations else ""
    elif source == "okx":
        try:
            observations = fetch_okx_series(series_id, limit=15)
        except RuntimeError:
            return cached if expired else None  # 拉取失败：过期回退旧缓存，缺失不编造
        version = f"okx:{series_id}:{observations[0]['obs_date']}" if observations else ""
    else:
        api_key = credential.get("macro_data_key")
        if not api_key:
            return None
        try:
            observations = fetch_fred_series(series_id, api_key, limit=15)
        except RuntimeError:
            return cached if expired else None  # 拉取失败：过期回退旧缓存，缺失不编造
        version = f"fred:{series_id}:{observations[0]['obs_date']}" if observations else ""
    if transform == "cpi_yoy":
        observations = _cpi_yoy(observations)
    elif transform == "diff":
        # 水平值 → 月度变化量（如 PAYEMS 就业人数的月增），变化量 = 当月 − 上月。
        ordered = sorted(observations, key=lambda row: row["obs_date"], reverse=True)
        out: list[dict[str, Any]] = []
        for index in range(len(ordered) - 1):
            out.append({"obs_date": ordered[index]["obs_date"], "value": round(ordered[index]["value"] - ordered[index + 1]["value"], 1)})
        observations = out
    if not observations:
        return cached if expired else None
    payload = {
        "observations": observations,
        "as_of": datetime.now(UTC).isoformat(),
        "dataset_version": version,
        "series_id": series_id,
    }
    db.upsert_macro_series(cache_key, payload)
    return payload


def _series_transform(region: str, indicator: str) -> str:
    """序列变换选择：CPI **指数**序列 → 12 期差分同比（月度 IMF 口径与美股口径同法）。"""
    return "cpi_yoy" if (region, indicator) in {
        ("us", "cpi_yoy"),
        ("us", "cpi_yoy_nsa"),
        ("cn", "cpi_yoy_imf"),
        ("in", "cpi_yoy_imf"),
    } else "none"


def get_region_snapshot(
    db: Database,
    credential: CredentialStore,
    region: str,
    *,
    weights: dict[str, float] | None = None,
    weight_version: str = "v1",
) -> MacroSnapshot | None:
    """组装国别快照（us/cn/eu/jp/in）或背景层快照（global）；未知 region 返回 None。

    weights/weight_version 来自宏观权重版本链（M5.5）：缺省 = 默认 v1。
    """
    if region == "global":
        rows: list[MacroBackgroundRow] = []
        pending_notes: list[str] = []
        for definition in _background_defs():
            if definition.get("fred"):
                payload = _resolve_series(db, credential, f"global:{definition['key']}", definition["fred"], ttl_hours=FRED_TTL_HOURS)
            elif definition.get("okx"):
                payload = _resolve_series(db, credential, f"global:{definition['key']}", definition["okx"], source="okx", ttl_hours=FRED_TTL_HOURS)
            else:
                payload = None
            if payload:
                latest = payload["observations"][0]
                rows.append(
                    MacroBackgroundRow(
                        key=definition["key"], label=definition["label"], status="ok",
                        latest=latest["value"], obs_date=latest["obs_date"],
                        as_of=payload["as_of"], source="FRED（公开接口）",
                        dataset_version=payload["dataset_version"], note=definition["note"],
                    )
                )
            else:
                note = definition["note"]
                pending_notes.append(definition["label"])
                rows.append(
                    MacroBackgroundRow(key=definition["key"], label=definition["label"], status="pending", note=note)
                )
        pending_notes_total = len(pending_notes)
        rows = [r for r in rows if r.status == "ok"]
        return MacroSnapshot(
            region="global", label="全球背景层（货币指数 · 油价 · 金价）",
            background=rows,
            degraded_reason=(
                f"{pending_notes_total} 项背景指标无公开数据源，已隐藏（接入后自动展示）"
                if pending_notes_total else None
            ),
        )

    if region not in REGIONS:
        return None
    readings: list[MacroIndicatorReading] = []
    payload_values: dict[str, dict[str, Any]] = {}
    for definition in _region_indicator_defs(region):
        series_id = definition.get("fred")
        wb_id = definition.get("wb")
        em_id = definition.get("em")
        if not series_id and not wb_id and not em_id:
            manual = get_manual_series(f"{region}:{definition['indicator']}")
            if manual:
                latest = manual["observations"][0]
                readings.append(MacroIndicatorReading(
                    indicator=definition["indicator"], label=definition["label"], dim=definition["dim"],
                    status="ok", latest=latest["value"], obs_date=latest.get("obs_date"),
                    unit=definition.get("unit", ""), as_of=manual.get("as_of"),
                    source="手动补录（官方公开值）", dataset_version=manual.get("dataset_version"),
                    note=f"{definition['note']}；{manual.get('note', '')}",
                ))
                continue
            readings.append(_pending(definition["indicator"], definition["label"], definition["dim"], definition["note"]))
            continue
        transform = _series_transform(region, definition["indicator"])
        if em_id:
            source_name = "东财数据中心（公开接口）"
            payload = _resolve_series(db, credential, f"{region}:{definition['indicator']}", em_id, source="em_macro", ttl_hours=FRED_TTL_HOURS)
        elif series_id:
            source_name = "FRED（公开接口）"
            payload = _resolve_series(db, credential, f"{region}:{definition['indicator']}", series_id, transform=transform, ttl_hours=FRED_TTL_HOURS)
        else:
            source_name = "世界银行（公开接口）"
            payload = _resolve_series(
                db, credential, f"{region}:{definition['indicator']}",
                f"{WB_ISO3[region]}|{wb_id}", source="worldbank", ttl_hours=WORLDBANK_TTL_HOURS,
            )
        if payload:
            payload_values[definition["indicator"]] = payload  # 键=裸指标名，供打分器查 observations
            latest = payload["observations"][0]
            previous = payload["observations"][1] if len(payload["observations"]) > 1 else None
            note = definition["note"]
            if previous is not None:
                note = f"{note}；上期 {previous['value']}{definition['unit']}（{previous['obs_date']}）"
            readings.append(
                MacroIndicatorReading(
                    indicator=definition["indicator"], label=definition["label"], dim=definition["dim"],
                    status="ok", latest=latest["value"], obs_date=latest["obs_date"],
                    unit=definition["unit"], as_of=payload["as_of"], source=source_name,
                    dataset_version=payload["dataset_version"], note=note,
                    frequency=definition.get("frequency"),
                )
            )
        else:
            pending_reason = (
                "凭据库无 FRED key（设置 → 数据源与密钥录入）"
                if series_id and not credential.get("macro_data_key")
                else "拉取失败，稍后重试"
            )
            readings.append(
                MacroIndicatorReading(
                    indicator=definition["indicator"], label=definition["label"], dim=definition["dim"],
                    status="pending",
                    note=f"{definition['note']}；{pending_reason}",
                )
            )
    # M5 定位引擎：确定性规则打分（可复算）；无任何 ok 指标时返回 None 不编造。
    from investment_steward_core import macro_scoring

    positioning = macro_scoring.compute_positioning(
        region, readings, payload_values, weights=weights, weight_version=weight_version
    )
    # 用户定稿（2026-09-06）：没有数据源的指标不展示，展示的都有真值。
    # pending 行不下发（打分已用全部 ok/payload 计算）；接入新源后自动出现。
    pending_count = sum(1 for r in readings if r.status != "ok")
    readings = [r for r in readings if r.status == "ok"]
    return MacroSnapshot(
        region=region, label=REGIONS[region], indicators=readings, positioning=positioning,
        degraded_reason=(
            None if pending_count == 0
            else f"{pending_count} 项指标无公开数据源或暂未取到，已隐藏（接入后自动展示）；不输出编造数值"
        ),
    )
