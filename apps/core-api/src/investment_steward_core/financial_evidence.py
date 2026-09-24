"""A 股财务报表证据源（东财 F10 免费接口直连，纯 stdlib）。

对齐 `D:/Administrator/Desktop/finance/packages/financial_data/akshare_provider.py` 的
东财约定（函数接口、symbol 前缀规则 SH/SZ/BJ、报表别名映射、报告期/公告期列名）——
不自行猜测任何标识符。默认经 `_EastmoneyDirectClient` 直连东财 F10 免费接口，不依赖
pandas/requests/akshare，也不需要安装 financials 额外依赖；取数失败或结构异常时显式降级
（available=False，不生成证据），并对齐 ADR-0004：`license_status=local-personal-use`、
`raw_locator` 保留原始来源与字段。

测试不依赖 pandas / 网络：fetch 接受框架对象，仅要求 `empty` 与 `to_dict(orient="records")`，
可按需注入假客户。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from investment_steward_core.news_evidence import parse_notice_timestamp

# 与 finance `_ENDPOINTS` 一致：报表类型 → akshare 端点函数名
_ENDPOINTS = {
    "income": "stock_profit_sheet_by_report_em",
    "balance": "stock_balance_sheet_by_report_em",
    "cash_flow": "stock_cash_flow_sheet_by_report_em",
}

# 与 finance `_ALIASES` 一致：规范行项目名 → 东财原始列名候选（按序取首个非空）
_ALIASES = {
    "income": (
        ("revenue", ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "营业总收入", "营业收入")),
        ("operating_profit", ("OPERATE_PROFIT", "营业利润")),
        ("net_profit", ("PARENT_NETPROFIT", "NETPROFIT", "归属于母公司股东的净利润", "净利润")),
        ("basic_eps", ("BASIC_EPS", "基本每股收益")),
    ),
    "balance": (
        ("cash", ("MONETARYFUNDS", "货币资金")),
        ("accounts_receivable", ("ACCOUNTS_RECE", "应收账款")),
        ("inventory", ("INVENTORY", "存货")),
        ("total_assets", ("TOTAL_ASSETS", "资产总计")),
        ("total_debt", ("TOTAL_LIABILITIES", "负债合计")),
        ("total_equity", ("TOTAL_EQUITY", "TOTAL_PARENT_EQUITY", "所有者权益合计")),
    ),
    "cash_flow": (
        ("operating_cash_flow", ("NETCASH_OPERATE", "经营活动产生的现金流量净额")),
        ("investing_cash_flow", ("NETCASH_INVEST", "投资活动产生的现金流量净额")),
        ("financing_cash_flow", ("NETCASH_FINANCE", "筹资活动产生的现金流量净额")),
        ("capital_expenditure", ("CONSTRUCT_LONG_ASSET", "购建固定资产、无形资产和其他长期资产支付的现金")),
    ),
}

_TRUE_STATEMENT_NAMES = {
    "income": "利润表",
    "balance": "资产负债表",
    "cash_flow": "现金流量表",
}

# R03（2026-09-18 排版与研报呈现一致性路线图）：S4 证据文本用「人话」中文行项目名。
# 旧实现把内部规范键（`basic_eps`/`total_debt`…）直接写进模型可见的 S4 文本
# （app.py:5878 形如 `basic_eps=0.26`），模型照抄成正文「basic_eps 0.26元/股」「total_debt 323.09亿元」。
# 规范键仍是账本 `item_snapshot`（app.py:5712）与 `raw_locator` 的回链主键，只改**呈现层**、不改取数。
# 未列出的键回退为原键（宁可露一个陌生键，也不臆造中文）。
ITEM_LABEL_CN: dict[str, str] = {
    "revenue": "营业总收入",
    "operating_profit": "营业利润",
    "net_profit": "归母净利润",
    "basic_eps": "基本每股收益",
    "cash": "货币资金",
    "accounts_receivable": "应收账款",
    "inventory": "存货",
    "total_assets": "总资产",
    "total_debt": "负债合计",
    "total_equity": "所有者权益合计",
    "operating_cash_flow": "经营活动现金流净额",
    "investing_cash_flow": "投资活动现金流净额",
    "financing_cash_flow": "筹资活动现金流净额",
    "capital_expenditure": "资本开支",
}

# R03：报表类型也换人话（`cash_flow` 带下划线，同样不该出现在正文里）。
STATEMENT_LABEL_CN: dict[str, str] = dict(_TRUE_STATEMENT_NAMES)


class FinancialsError(RuntimeError):
    """财报源取数或结构异常，应显式降级而非生成证据。"""


@dataclass(frozen=True)
class FinancialStatementRow:
    symbol: str
    statement_type: str
    period_end: str
    published_raw: str
    items: dict[str, str]
    identity: str


logger = logging.getLogger(__name__)

# 东财 F10「财务分析」报告期的直连接口（精确对齐 akshare stock_three_report_em 的
# `lrb/zcfzb/xjllb{DateAjaxNew,AjaxNew}` 约定，纯 stdlib，不引入 pandas/requests/akshare）：
# 先取报告期列表（{prefix}DateAjaxNew，reportDateType=0），再把报告期按每 5 个逗号拼接成
# dates 参数逐批拉明细（{prefix}AjaxNew，reportType=1）。companyType 来自 F10 页面隐藏域
# `hidctype`。
_F10_BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
_INDEX_URL = f"{_F10_BASE}Index"
_F10_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InvestmentSteward/0.1"
_F10_HEADERS = {"User-Agent": _F10_UA, "Accept-Encoding": "identity"}
_F10_TIMEOUT = 15
_F10_ATTEMPTS = 3
_F10_BACKOFF = 1.0
# 报表类型 → 东财 ajax 前缀（与 akshare `_ENDPOINTS` 端点一一对应）。
_STATEMENT_AJAX = {"income": "lrb", "balance": "zcfzb", "cash_flow": "xjllb"}
_HIDCTYPE_ATTR_FIRST = re.compile(r'id="hidctype"[^>]*?value="([^"]*)"')
_HIDCTYPE_ATTR_LAST = re.compile(r'value="([^"]*)"[^>]*?id="hidctype"')

_CACHE_TTL_SECONDS = 90.0
_data_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_fail_cache: dict[tuple[str, str], float] = {}


def _decode_body(raw: bytes) -> str:
    """东财对部分接口返回 gzip 流（regardless of Accept-Encoding），按需解压再按 utf-8 解码。"""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _http_get(url: str) -> str:
    """GET 并返回页面文本；网络异常重试后统一抛 FinancialsError。"""
    last_error: Exception | None = None
    for attempt in range(_F10_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=_F10_HEADERS)
            with urllib.request.urlopen(request, timeout=_F10_TIMEOUT) as response:
                return _decode_body(response.read())
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _F10_ATTEMPTS - 1:
                time.sleep(_F10_BACKOFF * (attempt + 1))
    raise FinancialsError(f"东方财富财务分析页请求失败: {last_error}")


def _http_get_json(url: str) -> dict[str, Any]:
    """GET 并解析 JSON；网络/结构异常重试后统一抛 FinancialsError（不中断调用方）。"""
    last_error: Exception | None = None
    for attempt in range(_F10_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=_F10_HEADERS)
            with urllib.request.urlopen(request, timeout=_F10_TIMEOUT) as response:
                payload = _decode_body(response.read())
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise FinancialsError("东方财富财务接口返回结构异常")
            return parsed
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _F10_ATTEMPTS - 1:
                time.sleep(_F10_BACKOFF * (attempt + 1))
    raise FinancialsError(f"东方财富财务请求失败: {last_error}")


def _company_type(symbol: str) -> str:
    """从 F10 分析页隐藏域 `hidctype` 取公司类型（决定报表字段集）。

    与 akshare `_stock_balance_sheet_by_report_ctype_em` 一致：页面 URL 的 code 用小写。
    """
    page_url = f"{_INDEX_URL}?{urllib.parse.urlencode({'type': 'web', 'code': symbol.lower()})}"
    page = _http_get(page_url)
    if not isinstance(page, str):
        raise FinancialsError("东方财富财务分析页不可解析")
    match = _HIDCTYPE_ATTR_FIRST.search(page) or _HIDCTYPE_ATTR_LAST.search(page)
    if match is None or not match.group(1):
        raise FinancialsError("无法从 F10 页面解析公司类型（hidctype）")
    return match.group(1)


def _report_dates(prefix: str, symbol: str, company_type: str) -> list[str]:
    """取报告期列表（{prefix}DateAjaxNew → data[].REPORT_DATE），按 YYYY-MM-DD 规整升序。"""
    url = (
        f"{_F10_BASE}{prefix}DateAjaxNew?"
        + urllib.parse.urlencode(
            {"companyType": company_type, "reportDateType": "0", "code": symbol}
        )
    )
    data = _http_get_json(url).get("data")
    if not isinstance(data, list) or not data:
        raise FinancialsError("东方财富未返回报告期列表")
    dates: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = item.get("REPORT_DATE")
        parsed = _iso_date_for(raw)
        if parsed and parsed not in seen:  # 规整为 YYYY-MM-DD 后去重
            seen.add(parsed)
            dates.append(parsed)
    if not dates:
        raise FinancialsError("东方财富报告期列表为空")
    # 东财默认倒序（最新在前）；升序便于 akshare 同步片的语义对齐，但切片按最新取。
    return dates


def _report_chunk(prefix: str, symbol: str, company_type: str, dates_arg: str) -> list[dict[str, Any]]:
    """取一批报告期的明细（{prefix}AjaxNew → data[]，字段即报表原始英文列名）。"""
    url = (
        f"{_F10_BASE}{prefix}AjaxNew?"
        + urllib.parse.urlencode(
            {
                "companyType": company_type,
                "reportDateType": "0",
                "reportType": "1",
                "dates": dates_arg,
                "code": symbol,
            }
        )
    )
    payload = _http_get_json(url)
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


class _RecordFrame:
    """轻量 record 列表包装：仅需 `empty` 与 `to_dict(orient==\"records\")`，供归一化层复用。"""

    __slots__ = ("_records",)

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    @property
    def empty(self) -> bool:
        return not self._records

    def to_dict(self, orient: str = "records") -> list[dict[str, Any]]:
        if orient != "records":
            raise ValueError("仅支持 records")
        return self._records


class _EastmoneyDirectClient:
    """东财财报直连客户端：暴露与 akshare 相同的 `stock_*_sheet_by_report_em(symbol)` 接口，
    返回 `_RecordFrame`（records 列表），使归一化层（`_fetch_statements_frame`）零改动复用。
    """

    def stock_balance_sheet_by_report_em(self, symbol: str) -> _RecordFrame:
        return _RecordFrame(self._load("balance", symbol))

    def stock_profit_sheet_by_report_em(self, symbol: str) -> _RecordFrame:
        return _RecordFrame(self._load("income", symbol))

    def stock_cash_flow_sheet_by_report_em(self, symbol: str) -> _RecordFrame:
        return _RecordFrame(self._load("cash_flow", symbol))

    def _load(self, statement_type: str, symbol: str) -> list[dict[str, Any]]:
        key = (statement_type, symbol)
        now = time.monotonic()
        cached = _data_cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        fail_at = _fail_cache.get(key)
        if fail_at and now - fail_at < _CACHE_TTL_SECONDS:
            raise FinancialsError(f"{symbol} 财报近期拉取失败，处于冷却期")
        prefix = _STATEMENT_AJAX[statement_type]
        # 客户端方法收到的 `symbol` 已是东财前缀码（如 SH600519，本模块 `_eastmoney_symbol`
        # 只负责把用户裸码转成该形式，勿在此重复归一化）；直接用于东财接口寻址与页面取公司类型。
        em_code = symbol
        try:
            company_type = _company_type(em_code)
            dates = _report_dates(prefix, em_code, company_type)
            limit = 5
            # 只需最近 limit 期（默认 5 → 1 个批次），不做 akshare 的全历史逐批拉取。
            recent = dates[:limit]
            records: list[dict[str, Any]] = []
            for start in range(0, len(recent), 5):
                batch = _report_chunk(prefix, em_code, company_type, ",".join(recent[start : start + 5]))
                if not batch:
                    break
                records.extend(batch)
            if not records:
                raise FinancialsError(f"东方财富未返回 {symbol} 的报表明细")
        except (FinancialsError, ValueError):
            _fail_cache[key] = time.monotonic()
            raise
        _data_cache[key] = (time.monotonic(), records)
        return records


def _direct_client() -> _EastmoneyDirectClient:
    """免费直连（东财）取数客户端；替换原 akshare 可选依赖路径（无需 financials extra）。"""
    return _EastmoneyDirectClient()


def _eastmoney_symbol(symbol: str) -> str:
    """与 finance `_eastmoney_symbol` 一致的 A 股代码前缀规则：5/6/9→SH，4/8→BJ，其余→SZ。"""
    code = symbol.strip().upper()
    if not code.isdigit() or len(code) != 6:
        raise ValueError("A 股财务数据要求 6 位证券代码")
    prefix = "SH" if code.startswith(("5", "6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
    return f"{prefix}{code}"


def _is_na(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _first_value(row: dict[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    for name in names:
        value = row.get(name)
        if not _is_na(value) and value != "":
            return name, value
    return None


def _decimal(value: Any) -> Decimal | None:
    if _is_na(value):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _iso_date_for(value: Any) -> str:
    """把报告期/公告期尽量转 ISO 日期字符串；解析不了保留原文（不猜测）。"""
    if _is_na(value):
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if len(text) >= 10 and text[:10].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    parsed = parse_notice_timestamp(text)
    return parsed.date().isoformat() if parsed else text


def _fetch_statements_frame(client: Any, statement_type: str, symbol: str, limit: int) -> list[dict[str, Any]]:
    endpoint_name = _ENDPOINTS[statement_type]
    endpoint = getattr(client, endpoint_name, None)
    if endpoint is None:
        raise FinancialsError(f"数据源缺少端点 {endpoint_name}，无法取 {_TRUE_STATEMENT_NAMES[statement_type]}")
    frame = endpoint(symbol=_eastmoney_symbol(symbol))
    if frame is None or frame.empty:
        raise FinancialsError(f"数据源未返回 {symbol} 的 {_TRUE_STATEMENT_NAMES[statement_type]}")
    return frame.to_dict(orient="records")[:limit]


def _extract_items(row: dict[str, Any], statement_type: str) -> dict[str, str]:
    items: dict[str, str] = {}
    for canonical, aliases in _ALIASES[statement_type]:
        matched = _first_value(row, aliases)
        if matched is None:
            continue
        value = _decimal(matched[1])
        if value is None:
            continue
        items[canonical] = str(value)
    return items


def _statement_identity(symbol: str, statement_type: str, period: str, published: str, items: dict[str, str]) -> str:
    payload = json.dumps({"instrument": symbol, "type": statement_type, "period": period, "published": published, "items": items}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_financials(symbol: str, limit_per_type: int = 5, client: Any | None = None) -> list[FinancialStatementRow]:
    """按三种报表类型拉取 A 股财报，返回可定位的归一化行记录。

    默认走 `_direct_client()`（东财 F10 免费接口直连，纯 stdlib，无外部依赖）；
    `client` 可注入假客户（含 `empty` 与 `to_dict(orient="records")`），用于无网络的
    一致契约测试。
    """
    active_client = client if client is not None else _direct_client()
    rows: list[FinancialStatementRow] = []
    for statement_type in _ENDPOINTS:
        records = _fetch_statements_frame(active_client, statement_type, symbol, limit_per_type)
        for raw in records[:limit_per_type]:
            period_value = _first_value(raw, ("REPORT_DATE", "REPORTDATE", "报告日", "报告期"))
            published_value = _first_value(raw, ("NOTICE_DATE", "UPDATE_DATE", "公告日期", "最新公告日期"))
            if period_value is None or published_value is None:
                continue
            items = _extract_items(raw, statement_type)
            if not items:
                continue
            period = _iso_date_for(period_value[1])
            published = _iso_date_for(published_value[1])
            rows.append(
                FinancialStatementRow(
                    symbol=symbol,
                    statement_type=statement_type,
                    period_end=period,
                    published_raw=published,
                    items=items,
                    identity=_statement_identity(symbol, statement_type, period, published, items),
                )
            )
    return rows


def raw_locator_for_financials(symbol: str, row: FinancialStatementRow) -> str:
    return json.dumps(
        {
            "api": "https://data.eastmoney.com/bbsj/",
            "symbol": symbol,
            "statement_type": row.statement_type,
            "period_end": row.period_end,
            "published": row.published_raw,
            "items": row.items,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# v29（三次升级方案 §3.2/§十）：**单季拆分**——A 股利润表/现金流量表按累计数披露，
# 直接把累计值当单季是本方案点名的错误口径。单季 = 本期累计 − **紧邻上一报告期**累计
# （Q1 即单季原值）；报告期序列出现断档（缺披露）时**停止差分**并如实标注，
# 绝不跨缺口相减（那会得到多季合计冒充单季）。资产负债表是时点数，不做差分。
# ---------------------------------------------------------------------------
QUARTERLY_SERIES_VERSION = "v1"
# 累计口径报表（需要差分）；资产负债表不在其中。
_CUMULATIVE_STATEMENTS = {"income", "cash_flow"}


def _quarter_label(period_end: str) -> str:
    """YYYY-MM-DD → (年份, 季度序号 1-4)；解析不出返回 (0, 0)，调用方按无效处理。"""
    text = (period_end or "")[:10]
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return 0, 0
    try:
        year = int(text[0:4])
        month = int(text[5:7])
    except ValueError:
        return 0, 0
    if month < 1 or month > 12 or month % 3 != 0:
        return 0, 0
    return year, month // 3


def derive_quarterly_series(
    rows: list[Any],
    *,
    statement_type: str,
    item_key: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """从归一化财报行拆**单季**序列（升序；缺失报告期保留断点，不补零、不跨缺口差分）。

    返回逐季：`period_end`（报告期）、`label`（如 2026Q2）、`value`（单季值）、
    `cumulative`（来源披露的累计原值）、`is_derived`（True=相邻报告期差分所得；
    Q1/断档后首期为原值）、`yoy`（单季同比：与去年同季单季比，缺去年同季为 None——
    不用累计同比冒充单季同比）。
    """
    # 1) 过滤 + 按报告期升序去重（同报告期取公告日最新的一份，保留可追溯性）。
    by_period: dict[str, FinancialStatementRow] = {}
    for row in rows:
        # 鸭子类型而非 isinstance：真实来源是 FinancialStatementRow，测试/插件可用属性齐全的替身。
        # （statement_type / period_end / published_raw / items 四个属性齐全即可参与。）
        if not all(hasattr(row, attr) for attr in ("statement_type", "period_end", "published_raw", "items")):
            continue
        if row.statement_type != statement_type:
            continue
        if item_key not in row.items:
            continue
        period = (row.period_end or "")[:10]
        if _quarter_label(period) == (0, 0):
            continue
        existing = by_period.get(period)
        if existing is None or (row.published_raw or "") >= (existing.published_raw or ""):
            by_period[period] = row
    ordered = [by_period[period] for period in sorted(by_period)]
    if not ordered:
        return []

    cumulative: dict[str, Decimal | None] = {}
    for row in ordered:
        value = _decimal(row.items[item_key])
        cumulative[(row.period_end or "")[:10]] = value

    out: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        period = (row.period_end or "")[:10]
        year, quarter = _quarter_label(period)
        current = cumulative[period]
        if current is None:
            out.append({
                "period_end": period, "label": f"{year}Q{quarter}", "value": None,
                "cumulative": None, "is_derived": False, "yoy": None,
                "note": "该期累计值缺失或不可解析，单季不可得（不补零）。",
            })
            continue
        is_q1 = quarter == 1
        previous: Decimal | None = None
        previous_ready = False
        if index > 0:
            prev_period = (ordered[index - 1].period_end or "")[:10]
            prev_year, prev_quarter = _quarter_label(prev_period)
            # 紧邻报告期 = 同一年内季序恰好 +1（跨年或跳季视为断档，不跨缺口差分）。
            if prev_year == year and prev_quarter == quarter - 1:
                previous = cumulative[prev_period]
                previous_ready = previous is not None
        if statement_type in _CUMULATIVE_STATEMENTS and not is_q1:
            if previous_ready:
                quarter_value: Decimal | None = current - previous
                is_derived = True
                note = ""
            else:
                # ★ 方案 §十「不能把累计值当单季」：紧邻上期缺失时宁可给 None，
                #   也绝不把本期累计值冒充单季（缺失保留断点）。
                quarter_value = None
                is_derived = False
                note = "紧邻上一报告期缺失，累计值无法拆为单季（保留断点，不补零）。"
        else:
            # Q1 即单季；非累计报表（资产负债表为时点数）：披露原值即单季值。
            quarter_value = current
            is_derived = False
            note = ""
        single = float(quarter_value) if quarter_value is not None else None
        yoy: float | None = None
        if single is not None:
            prior = next(
                (item["value"] for item in out if item["label"] == f"{year - 1}Q{quarter}" and item["value"] is not None),
                None,
            )
            if prior not in (None, 0):
                yoy = round((single - prior) / abs(prior), 4)
        # v30（申通方案 §四.1）：派生指标公式与输入期间留痕——差分值可复算，原值可回链报告期。
        # is_derived=True 时 prev_period 必已定义（差分只在「紧邻报告期存在」分支成立）。
        quarter_label = f"{year}Q{quarter}"
        if is_derived:
            formula = f"{quarter_label} 单季 = {period} 累计 − {prev_period} 累计（相邻报告期差分）"
            formula_inputs: list[str] = [prev_period, period]
        else:
            formula = f"{quarter_label} 单季 = {period} 披露原值（Q1/时点数不差分）"
            formula_inputs = [period]
        out.append({
            "period_end": period,
            "label": quarter_label,
            "value": single,
            "cumulative": float(current),
            "is_derived": is_derived,
            "yoy": yoy,
            "note": note,
            "formula": formula,
            "inputs": formula_inputs,
        })
    return out[-limit:] if limit and len(out) > limit else out


def describe_quarterly(series: list[dict[str, Any]], item_key: str) -> str:
    """单季序列 → 可进 S4 的一行文本（模型据此解释「下降来自收入、毛利率、费用还是非经常项目」）。"""
    if not series:
        return ""
    parts = []
    for item in series:
        value = item["value"]
        text = "不可拆" if value is None else f"{value:,.0f}"
        yoy = item.get("yoy")
        yoy_text = "" if yoy is None else f"(同比 {yoy * 100:+.1f}%)"
        mark = "" if item.get("is_derived") else "〔原值〕"
        parts.append(f"{item['label']} {text}{yoy_text}{mark}")
    joined = "；".join(parts)
    if item_key == "revenue":
        label = "营业收入"
    elif item_key == "net_profit":
        label = "归母净利润"
    elif item_key == "operating_cash_flow":
        label = "经营活动现金流净额"
    else:
        label = item_key
    return (
        f"- 单季{label}（累计报表相邻报告期差分，Q1/标注〔原值〕者为披露原值；"
        f"缺失报告期保留断点不补零）：{joined}"
    )