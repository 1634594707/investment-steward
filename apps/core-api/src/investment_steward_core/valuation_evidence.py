"""A 股估值证据源（东财 datacenter 估值分析直连，纯 stdlib）。

对齐 `financial_evidence.py` 的约定（免费直连、90s 缓存、失败冷却、显式降级、raw_locator），
为个股研报补齐**估值维度**（此前证据包只有 K 线/新闻/公告/三大报表，无任何估值指标）。
覆盖三层估值证据：

1. 当前估值快照：PE(TTM) / PE(静态) / PB(MRQ) / PS(TTM) / PEG / PCF、总市值 / 流通市值、行业二级；
2. 历史估值分位：日频 PE/PB/PS 序列 → 区间百分位（判断「当前处于历史什么位置」）；
3. 同业相对估值：同行业二级（BOARD_CODE）当日全部个股估值 → 中位数与目标位次。

背景（2026-09-10 实测，勿凭记忆改写，改动前先复跑探测）：
- 东财 push2 quote（`push2.eastmoney.com/api/qt/stock/get`）对 urllib **断连反爬**
  （RemoteDisconnected），**不可用**——与 2026-09-07 对 push2 clist 的结论一致；
- 腾讯 `qt.gtimg.cn/q=<code>` 可达（含 PE-TTM / PB / 总市值），但**无历史序列、无行业分类**，
  故仅作旁证，不作本模块主源；
- 本模块主源 `datacenter-web RPT_VALUEANALYSIS_DET` urllib 可达，单页上限 500，
  历史回溯至 2018-01-02（约 2100 个交易日）；
- 该源**无股息率字段**：显式请求 `DIVIDEND_YIELD` 时服务端返回 `code 9501「字段不存在」`。
  因此本模块**不提供股息率**，如实不编造；需要时另接分红接口，不得在此臆造。

口径铁律：分位只对**正数**序列计算——亏损期 PE 为负或缺失，分位无经济含义，如实置 None 并在
`percentile_note` 说明，绝不把负值塞进分位序列冒充「低估」。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from investment_steward_core import feed_health

VALUATION_SOURCE_LABEL = "东方财富·估值分析"

_VALUATION_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REPORT_NAME = "RPT_VALUEANALYSIS_DET"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InvestmentSteward/0.1"
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Encoding": "identity",
    "Referer": "https://data.eastmoney.com/",
}
_TIMEOUT = 20
_ATTEMPTS = 3
_BACKOFF = 1.0
_PAGE_SIZE = 500  # 服务端实测单页上限（2026-09-10）
_MAX_PAGES = 12
_CACHE_TTL_SECONDS = 90.0
_TRADING_DAYS_PER_YEAR = 250
_MIN_PERCENTILE_SAMPLES = 20  # 样本不足 20 个交易日不算分位（避免「一根K线分位」的伪结论）

_CURRENT_COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_CODE,BOARD_NAME,TRADE_DATE,CLOSE_PRICE,"
    "TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A,TOTAL_SHARES,FREE_SHARES_A,"
    "PE_TTM,PE_LAR,PB_MRQ,PS_TTM,PCF_OCF_TTM,PEG_CAR"
)
_HISTORY_COLUMNS = "TRADE_DATE,PE_TTM,PB_MRQ,PS_TTM,CLOSE_PRICE"
_PEER_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,PE_TTM,PB_MRQ,PS_TTM,TRADE_DATE"
# B01（v35）：全市场横截面列 —— 板块聚合所需的最小字段集（不取名称/股本科减少负载）。
_CROSS_SECTION_COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_CODE,BOARD_NAME,TRADE_DATE,"
    "TOTAL_MARKET_CAP,PE_TTM,PB_MRQ,PS_TTM"
)
# B01：全市场当日横截面约 11 页（A 股约 5400 只 / 单页 500，2026-09-14 口径）。
_CROSS_SECTION_MAX_PAGES = 14
_CROSS_SECTION_SORT = "SECURITY_CODE"

_PERCENTILE_METRICS = ("pe_ttm", "pb_mrq", "ps_ttm")

_cache: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = {}
_fail_cache: dict[tuple[str, str, int], float] = {}
# C06：带墙钟时间的陈旧缓存（与 90s `_cache` 独立）——主源失败时按「注明时点的有限比较」
# 口径降级复用，绝不冒充实时数据。横陈旧数据超过 `_STALE_TTL_SECONDS` 直接丢弃（宁缺勿错）。
_STALE_TTL_SECONDS = 7 * 24 * 3600.0
_stale_cache: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = {}
# C06：最后一份**完整**横截面（原子单元：全部页成功才算）——单页回退会造成跨页拼接，
# 因此横截面的降级以整份快照为粒度，不做部分页拼凑。
_last_good_cross_section: tuple[float, str, list[CrossSectionRow]] | None = None


class ValuationError(RuntimeError):
    """估值源取数或结构异常，应显式降级而非生成证据。

    C06：`failure_kind` 结构化区分失败类别，供调用方归类缺口（不再靠文本猜）：
    - `request_failed`：主源请求失败 / 冷却期 / 服务端返回失败（源不可用类）；
    - `empty_data`：主源可达但**返回空数据**（如停牌股、无该交易日横截面）。
    「筛选后样本不足」不是异常路径，由分位/聚合逻辑以 None + note 表达，不走本异常。
    """

    def __init__(self, message: str, *, failure_kind: str = "request_failed"):
        super().__init__(message)
        self.failure_kind = failure_kind


@dataclass(frozen=True)
class PeerValuation:
    """同业单只个股的估值快照（用于横向比较，不含历史与分位）。"""

    symbol: str
    name: str
    pe_ttm: float | None
    pb_mrq: float | None
    ps_ttm: float | None


@dataclass(frozen=True)
class ValuationSnapshot:
    """一次估值取数的完整结果：当前值 + 历史分位 + 同业相对。"""

    symbol: str
    name: str
    board_code: str
    board_name: str
    trade_date: str
    close_price: float | None
    pe_ttm: float | None
    pe_static: float | None
    pb_mrq: float | None
    ps_ttm: float | None
    peg: float | None
    pcf_ocf_ttm: float | None
    total_market_cap: float | None
    float_market_cap: float | None
    total_shares: float | None
    percentiles: dict[str, float | None] = field(default_factory=dict)
    percentile_window_bars: int = 0
    percentile_window_from: str = ""
    percentile_note: str = ""
    # B02（v36）：价格趋势 —— 与分位**共用同一份历史序列**计算，零额外网络请求。
    # 历史取数失败时分位与趋势同为缺失，口径一致（不出现「有分位无趋势」的矛盾态）。
    trend: PriceTrend | None = None
    peers: tuple[PeerValuation, ...] = ()
    peer_count: int = 0
    peer_median: dict[str, float | None] = field(default_factory=dict)
    peer_median_basis: dict[str, int] = field(default_factory=dict)
    peer_rank: dict[str, int] = field(default_factory=dict)
    peer_rank_basis: dict[str, int] = field(default_factory=dict)
    peer_loss_count: int = 0
    identity: str = ""
    # C06：非空表示当前值来自「带日期缓存」降级（注明时点的有限比较），调用方须如实渲染。
    degraded_note: str = ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num(value: Any) -> float | None:
    """数值归一：非数值 / NaN / 空串一律 None（不猜测、不填 0）。"""
    if value is None or value == "":
        return None
    if _is_number(value):
        result = float(value)
    else:
        try:
            result = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    if result != result:  # NaN
        return None
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_symbol(symbol: str) -> str:
    code = str(symbol).strip().upper()
    if not code.isdigit() or len(code) != 6:
        raise ValueError("A 股估值数据要求 6 位证券代码")
    return code


def _build_filter(
    symbol: str | None = None, board_code: str | None = None, trade_date: str | None = None
) -> str:
    """东财 datacenter filter 语法：`(字段="值")` 串联，日期用单引号。"""
    parts: list[str] = []
    if symbol:
        parts.append(f'(SECURITY_CODE="{symbol}")')
    if board_code:
        parts.append(f'(BOARD_CODE="{board_code}")')
    if trade_date:
        parts.append(f"(TRADE_DATE='{trade_date}')")
    return "".join(parts)


def _get_json(params: dict[str, Any]) -> dict[str, Any]:
    """GET 并解析 JSON；网络/结构异常重试后统一抛 ValuationError（不中断调用方）。"""
    url = _VALUATION_URL + "?" + urllib.parse.urlencode(params)
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=_HEADERS)
            # F02：逐次尝试埋点（含重试的第几次）。params 里含 reportName/filter/page，
            # 故同一逻辑查询的不同分页各有指纹，可分页看延迟与失败。
            with feed_health.attempt(
                feed_health.SOURCE_VALUATION_EASTMONEY, _VALUATION_URL, params, attempt_index=attempt + 1
            ):
                with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                if not isinstance(payload, dict):
                    raise ValuationError("估值接口返回结构异常")
                return payload
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _ATTEMPTS - 1:
                time.sleep(_BACKOFF * (attempt + 1))
    raise ValuationError(f"{VALUATION_SOURCE_LABEL}接口请求失败: {last_error}")


def _query(
    columns: str,
    flt: str,
    *,
    page_size: int,
    page_number: int = 1,
    sort_column: str,
    desc: bool = True,
) -> list[dict[str, Any]]:
    """带 90s 缓存与失败冷却的一页查询。"""
    key = (flt, columns, page_number)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    fail_at = _fail_cache.get(key)
    if fail_at and now - fail_at < _CACHE_TTL_SECONDS:
        raise ValuationError(f"{flt} 近期拉取失败，处于冷却期")

    params = {
        "reportName": _REPORT_NAME,
        "columns": columns,
        "filter": flt,
        "pageNumber": page_number,
        "pageSize": page_size,
        "sortColumns": sort_column,
        "sortTypes": -1 if desc else 1,
    }
    try:
        payload = _get_json(params)
    except ValuationError:
        _fail_cache[key] = time.monotonic()
        raise
    result = payload.get("result")
    rows: list[dict[str, Any]] = []
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            rows = [item for item in data if isinstance(item, dict)]
    if not rows and payload.get("success") is False:
        message = _text(payload.get("message")) or "未知错误"
        _fail_cache[key] = time.monotonic()
        raise ValuationError(f"{VALUATION_SOURCE_LABEL}返回失败: {message}")
    _cache[key] = (time.monotonic(), rows)
    _stale_cache[key] = (time.time(), rows)  # C06：成功即留档，供主源失败时注明时点降级
    return rows


def _query_with_stale(
    columns: str,
    flt: str,
    *,
    page_size: int,
    page_number: int = 1,
    sort_column: str,
    desc: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """C06：实时路径失败 → 回退**带日期的陈旧缓存**（注明抓取时点，禁止冒充实时）。

    返回 `(rows, degraded_note)`；`degraded_note` 为空串表示实时数据，非空时调用方
    必须把该说明带入证据文本。陈旧缓存超 `_STALE_TTL_SECONDS`（7 天）或不存在的
    键不回退（宁缺勿错），照常抛 `ValuationError`。
    """
    key = (flt, columns, page_number)
    try:
        return (
            _query(
                columns, flt, page_size=page_size, page_number=page_number,
                sort_column=sort_column, desc=desc,
            ),
            "",
        )
    except ValuationError:
        entry = _stale_cache.get(key)
        if not entry:
            raise
        age_seconds = time.time() - entry[0]
        if age_seconds > _STALE_TTL_SECONDS or not entry[1]:
            raise
        age_hours = age_seconds / 3600.0
        note = (
            f"主源请求失败，降级使用带日期缓存：抓取于约 {age_hours:.1f} 小时前，"
            "数据截至以行内 TRADE_DATE 为准；非实时口径，仅作注明时点的有限比较，"
            "不与实时数据混排"
        )
        return list(entry[1]), note


# —— B04（v35）断连防护：冷却期判定与预算截断说明 ——
def symbol_in_cooldown(symbol: str) -> bool:
    """该股任一查询键是否处于失败冷却期（冷却期内不再发起请求，降级为缺口）。

    复用 `_fail_cache`（与 `_query` 同源），不另建冷却状态；任何一页处于冷却即视为该股
    不可取数——避免「部分页成功」拼出残缺分位。
    """
    try:
        code = _normalize_symbol(symbol)
    except ValueError:
        return False
    now = time.monotonic()
    prefix_keys = (
        _build_filter(symbol=code),
    )
    for key, fail_at in _fail_cache.items():
        if key[0] in prefix_keys and now - fail_at < _CACHE_TTL_SECONDS:
            return True
    return False


def reset_cooldowns() -> None:
    """清空成功缓存、失败冷却与内存留档（供测试与「用户手动重试」路径调用）。

    C06 起 `_last_good_cross_section` 也是内存态「成功缓存」，必须一并清空：
    否则上一轮测试/上一次成功取数留下的整份横截面会在主源失败时被整份降级复用，
    造成跨用例状态泄漏（同一进程内顺序相关）。
    """
    global _last_good_cross_section
    _cache.clear()
    _fail_cache.clear()
    _last_good_cross_section = None



def fetch_valuation_history(symbol: str, years: int = 5) -> list[dict[str, Any]]:
    """按交易日倒序取估值历史序列（默认近 5 年，约 1250 个交易日 → 3 页）。"""
    code = _normalize_symbol(symbol)
    want = max(1, int(years * _TRADING_DAYS_PER_YEAR))
    flt = _build_filter(symbol=code)
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < want and page <= _MAX_PAGES:
        rows = _query(
            _HISTORY_COLUMNS, flt, page_size=_PAGE_SIZE, page_number=page,
            sort_column="TRADE_DATE", desc=True,
        )
        if not rows:
            break
        out.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        page += 1
    return out[:want]


def fetch_peer_valuations(
    board_code: str, trade_date: str, *, exclude_symbol: str | None = None, limit: int = 30
) -> list[PeerValuation]:
    """取同行业二级当日全部个股估值（按 PE(TTM) 升序），用于横向比较。"""
    if not board_code or not trade_date:
        return []
    flt = _build_filter(board_code=board_code, trade_date=trade_date)
    rows = _query(_PEER_COLUMNS, flt, page_size=100, sort_column="PE_TTM", desc=False)
    out: list[PeerValuation] = []
    for row in rows:
        code = _text(row.get("SECURITY_CODE"))
        if exclude_symbol and code == exclude_symbol:
            continue
        out.append(
            PeerValuation(
                symbol=code,
                name=_text(row.get("SECURITY_NAME_ABBR")),
                pe_ttm=_num(row.get("PE_TTM")),
                pb_mrq=_num(row.get("PB_MRQ")),
                ps_ttm=_num(row.get("PS_TTM")),
            )
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# B01（v35）：全市场当日估值横截面 → 按东财行业板块聚合
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossSectionRow:
    """横截面单只个股的估值行（仅板块聚合所需字段）。"""

    symbol: str
    name: str
    board_code: str
    board_name: str
    total_market_cap: float | None
    pe_ttm: float | None
    pb_mrq: float | None
    ps_ttm: float | None


@dataclass(frozen=True)
class SectorValuationAggregate:
    """单个东财行业板块的估值聚合结果。

    口径：中位数只用**正数**样本（`*_positive_count` 为可比样本数）；亏损股（PE ≤ 0）
    单独计数并从 PE 可比样本中排除；成分股数为该板块当日横截面出现的全部个股。
    """

    board_code: str
    board_name: str
    member_count: int
    total_market_cap: float | None
    pe_ttm_median: float | None
    pe_ttm_positive_count: int
    pb_mrq_median: float | None
    pb_mrq_positive_count: int
    ps_ttm_median: float | None
    ps_ttm_positive_count: int
    loss_count: int
    trade_date: str

    @property
    def comparable_count(self) -> int:
        """可比样本数（以 PB 正数样本为准；PB 跨行业可比性优于 PE）。"""
        return self.pb_mrq_positive_count


def latest_valuation_trade_date() -> str:
    """B01：探测估值源最新交易日（YYYY-MM-DD；无数据抛 ValuationError）。

    取全市场按 TRADE_DATE 倒序的第一行日期——横截面的 `TRADE_DATE` 过滤器必须与源
    实际最新交易日一致，否则会取到空集或历史残留。
    """
    rows = _query(
        "SECURITY_CODE,TRADE_DATE", "", page_size=1, sort_column="TRADE_DATE", desc=True
    )
    if not rows:
        raise ValuationError(
            f"{VALUATION_SOURCE_LABEL}未返回任何交易日期", failure_kind="empty_data"
        )
    date = _text(rows[0].get("TRADE_DATE"))[:10]
    if not date:
        raise ValuationError(
            f"{VALUATION_SOURCE_LABEL}最新交易日字段为空", failure_kind="empty_data"
        )
    return date


def fetch_market_cross_section(trade_date: str) -> list[CrossSectionRow]:
    """B01：取全市场当日估值横截面（分页，`page_size=500`，封顶 `_CROSS_SECTION_MAX_PAGES`）。

    分页依赖 `_query` 的 90 秒缓存与失败冷却；中途某页失败 → 抛 `ValuationError`
    由调用方如实降级（不做「部分页拼凑」——残缺横截面会把板块中位数算错）。
    """
    if not trade_date:
        raise ValuationError("横截面取数缺少交易日")
    flt = _build_filter(trade_date=trade_date)
    out: list[CrossSectionRow] = []
    page = 1
    while page <= _CROSS_SECTION_MAX_PAGES:
        rows = _query(
            _CROSS_SECTION_COLUMNS,
            flt,
            page_size=_PAGE_SIZE,
            page_number=page,
            sort_column=_CROSS_SECTION_SORT,
            desc=False,
        )
        if not rows:
            break
        for row in rows:
            out.append(
                CrossSectionRow(
                    symbol=_text(row.get("SECURITY_CODE")),
                    name=_text(row.get("SECURITY_NAME_ABBR")),
                    board_code=_text(row.get("BOARD_CODE")),
                    board_name=_text(row.get("BOARD_NAME")),
                    total_market_cap=_num(row.get("TOTAL_MARKET_CAP")),
                    pe_ttm=_num(row.get("PE_TTM")),
                    pb_mrq=_num(row.get("PB_MRQ")),
                    ps_ttm=_num(row.get("PS_TTM")),
                )
            )
        if len(rows) < _PAGE_SIZE:
            break
        page += 1
    if not out:
        raise ValuationError(
            f"{VALUATION_SOURCE_LABEL}未返回 {trade_date} 的横截面数据",
            failure_kind="empty_data",
        )
    # C06：完整横截面成功即留档（原子单元），供主源失败时整份降级（不拼页）。
    global _last_good_cross_section
    _last_good_cross_section = (time.time(), trade_date, out)
    return out


def cross_section_with_fallback() -> tuple[str, list[CrossSectionRow], str]:
    """C06：横截面取数 + 主源不可用时的整份降级（返回 `(trade_date, rows, degraded_note)`）。

    实时路径 = `latest_valuation_trade_date()` + `fetch_market_cross_section()`（沿用
    模块级函数名，测试打桩照常生效）。任一失败时，若存在**完整且未超 7 天**的最后
    一份横截面快照，则整份返回并附降级说明（注明抓取时点与数据截至日期）；否则照常
    抛 `ValuationError`——调用方进入「证据不足」状态，不做部分页拼凑。
    """
    global _last_good_cross_section
    try:
        trade_date = latest_valuation_trade_date()
        rows = fetch_market_cross_section(trade_date)
        return trade_date, rows, ""
    except ValuationError:
        entry = _last_good_cross_section
        if entry is None:
            raise
        wall, trade_date, rows = entry
        age_seconds = time.time() - wall
        if age_seconds > _STALE_TTL_SECONDS or not rows:
            raise
        age_hours = age_seconds / 3600.0
        note = (
            f"估值主源当前不可用，降级使用缓存横截面：抓取于约 {age_hours:.1f} 小时前，"
            f"数据截至 {trade_date}；为注明时点的有限比较，非实时口径，"
            "不与实时数据混排，不据此下「当前低估」结论"
        )
        return trade_date, list(rows), note


def aggregate_sector_valuations(
    rows: list[CrossSectionRow], *, trade_date: str = ""
) -> list[SectorValuationAggregate]:
    """B01：按 `BOARD_CODE` 聚合横截面 → 每板块估值中位数与样本构成（纯函数，无 IO）。

    中位数只用正数样本（亏损期指标无经济含义）；`loss_count` 只统计 **PE ≤ 0** 的
    亏损股（PB/PS 为负是净资产/营收为负，与亏损不是同一概念，不并入此计数）。
    """
    buckets: dict[str, list[CrossSectionRow]] = {}
    for row in rows:
        code = row.board_code
        if not code:
            continue  # 无板块归属的行不参与聚合（不建「未分类」伪板块）
        buckets.setdefault(code, []).append(row)

    out: list[SectorValuationAggregate] = []
    for code, members in buckets.items():
        pe_values = [item.pe_ttm for item in members]
        pb_values = [item.pb_mrq for item in members]
        ps_values = [item.ps_ttm for item in members]
        pe_pos = [value for value in pe_values if value is not None and value > 0]
        pb_pos = [value for value in pb_values if value is not None and value > 0]
        ps_pos = [value for value in ps_values if value is not None and value > 0]
        caps = [item.total_market_cap for item in members if item.total_market_cap is not None]
        board_name = next((item.board_name for item in members if item.board_name), "")
        out.append(
            SectorValuationAggregate(
                board_code=code,
                board_name=board_name,
                member_count=len(members),
                total_market_cap=round(sum(caps), 2) if caps else None,
                pe_ttm_median=_median(pe_pos),
                pe_ttm_positive_count=len(pe_pos),
                pb_mrq_median=_median(pb_pos),
                pb_mrq_positive_count=len(pb_pos),
                ps_ttm_median=_median(ps_pos),
                ps_ttm_positive_count=len(ps_pos),
                loss_count=sum(1 for value in pe_values if value is not None and value <= 0),
                trade_date=trade_date,
            )
        )
    return out


def sector_valuation_snapshot(
    *,
    board_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, list[SectorValuationAggregate]]:
    """B01：横截面取数 + 聚合的一站式入口（返回 (trade_date, 聚合列表)）。

    `board_names` 非空时（A03 复合主题的产业限定）只保留名称命中的板块——过滤按名
    宽松匹配，宁少不错配。
    """
    trade_date = latest_valuation_trade_date()
    rows = fetch_market_cross_section(trade_date)
    aggregates = aggregate_sector_valuations(rows, trade_date=trade_date)
    if board_names:
        wanted = [str(name).strip() for name in board_names if str(name).strip()]
        if wanted:
            aggregates = [
                item for item in aggregates if _board_name_in(item.board_name, wanted)
            ]
    return trade_date, aggregates


def _board_name_in(candidate: str, wanted: list[str]) -> bool:
    """板块名宽松匹配（去「行业」后缀后双向包含）；与 direction_research 同口径，
    此处独立实现以避免 valuation_evidence 反向依赖方向研判模块（依赖方向单向）。"""
    name = _text(candidate).removesuffix("行业")
    if not name:
        return False
    for raw in wanted:
        target = _text(raw).removesuffix("行业")
        if target and (name in target or target in name):
            return True
    return False


# ---------------------------------------------------------------------------
# B02（v35）/ A03（v36）：板块估值判定榜单 —— 排序口径声明 + top N
# ---------------------------------------------------------------------------
# A03（v36）主键切换：v35 用**绝对 PB 中位数**排序，回答的是「谁 PB 绝对值最低」；
# 银行等金融业结构性低 PB 在这种口径下永远靠前，与「相对自身历史是否便宜」无关
# （银行Ⅱ 绝对 PB 0.57 最低，但代表股 PB 分位中位数 96.6% 已处自身历史高位）。
# v36 改为**代表股 PB 分位中位数升序**（自身便宜度），分位不可得的板块不上榜。
# 次键沿用 PE 中位数、总市值，保证同分位时排序稳定且可复现。
SECTOR_RANKING_CALIBER = (
    "排序口径：主键**代表股 PB 分位中位数升序**（相对自身历史的便宜度；分位越低越便宜）；"
    "次键 PE(TTM) 中位数升序；同键板块总市值降序。"
    "分位只用正数样本且历史样本不少于 20 个交易日；样本数随条目披露。"
    "本榜单给出「板块估值判定（相对自身历史的便宜度排序）+ 四维分类」——"
    "便宜（低分位）不等于低估，还需盈利广度与价格位置未塌陷；"
    "分类是研究判断的显式化，不构成买卖指令。"
)
SECTOR_RANKING_TOP_N = 5
# 进入榜单的最低可比样本数：板块成分中 PB 为正的个股少于该数则不参与排序
# （样本太小的板块中位数不具代表性，宁可不上榜也不给伪信号）。
SECTOR_RANKING_MIN_SAMPLES = 5
# A03：主键为分位时，分位可得股数的最低要求（分位样本太少不具代表性）。
SECTOR_RANKING_MIN_PERCENTILE_STOCKS = 1


@dataclass(frozen=True)
class SectorRankingEntry:
    """榜单条目：板块聚合 + 排名与排序所用键值的显式记录。

    A03：`sort_percentile` 为 v36 主键（代表股 PB 分位中位数）；None 表示该板块走的是
    v35 绝对 PB 兼容路径或分位不可得。`label` / `rationale` 由调用方注入分类引擎结果
    （valuation_evidence 不依赖 direction_research，避免反向依赖）。
    """

    rank: int
    aggregate: SectorValuationAggregate
    sort_pb: float | None
    sort_pe: float | None
    sort_cap: float | None
    sort_percentile: float | None = None
    label: str = ""
    rationale: str = ""


def rank_sector_valuations(
    aggregates: list[SectorValuationAggregate],
    *,
    top_n: int = SECTOR_RANKING_TOP_N,
    min_samples: int = SECTOR_RANKING_MIN_SAMPLES,
    percentiles: dict[str, float | None] | None = None,
) -> list[SectorRankingEntry]:
    """B02（v35）/ A03（v36）：按声明口径排序产出板块估值判定榜单（纯函数，无 IO）。

    排序主键（A03）：
    - `percentiles` 提供时（board_code → 代表股 PB 分位中位数）：**主键分位升序**
      （自身便宜度），分位不可得（None）或缺该板块条目的板块**不上榜**；
    - `percentiles` 为 None 时（兼容路径，如复合主题仅需板块清单）：沿用 v35 的
      **绝对 PB 中位数升序**，行为与 v35 完全一致。

    参与排序的前置条件：PB 中位数可得且 PB 可比样本数 ≥ `min_samples`。样本不足或
    PB 中位数缺失的板块**不上榜**（由调用方在缺口声明中说明覆盖范围），不排在末尾
    冒充满足条件的板块。
    """
    eligible = [
        item
        for item in aggregates
        if item.pb_mrq_median is not None and item.pb_mrq_positive_count >= min_samples
    ]

    if percentiles is not None:
        # A03：主键 = 代表股 PB 分位中位数（自身便宜度）。分位不可得 → 不上榜。
        eligible = [
            item
            for item in eligible
            if percentiles.get(item.board_code) is not None
        ]

        def _key_pct(item: SectorValuationAggregate) -> tuple[object, ...]:
            pe_missing = item.pe_ttm_median is None
            cap_missing = item.total_market_cap is None
            pct = percentiles.get(item.board_code)
            return (
                pct if pct is not None else 100.0,
                pe_missing,
                item.pe_ttm_median if item.pe_ttm_median is not None else 0.0,
                cap_missing,
                -(item.total_market_cap if item.total_market_cap is not None else 0.0),
            )

        ordered = sorted(eligible, key=_key_pct)
        return [
            SectorRankingEntry(
                rank=index + 1,
                aggregate=item,
                sort_pb=item.pb_mrq_median,
                sort_pe=item.pe_ttm_median,
                sort_cap=item.total_market_cap,
                sort_percentile=percentiles.get(item.board_code),
            )
            for index, item in enumerate(ordered[: max(0, int(top_n))])
        ]

    # 排序键（v35 兼容）：PB 中位数升序 → PE 中位数升序（缺 PE 的排后）→ 总市值降序。
    # 用 (has_value, value) 元组把 None 稳定地排到末尾，避免 None 参与数值比较。
    def _key(item: SectorValuationAggregate) -> tuple[object, ...]:
        pb_missing = item.pb_mrq_median is None
        pe_missing = item.pe_ttm_median is None
        cap_missing = item.total_market_cap is None
        return (
            pb_missing,
            item.pb_mrq_median if item.pb_mrq_median is not None else 0.0,
            pe_missing,
            item.pe_ttm_median if item.pe_ttm_median is not None else 0.0,
            cap_missing,
            -(item.total_market_cap if item.total_market_cap is not None else 0.0),
        )

    ordered = sorted(eligible, key=_key)
    return [
        SectorRankingEntry(
            rank=index + 1,
            aggregate=item,
            sort_pb=item.pb_mrq_median,
            sort_pe=item.pe_ttm_median,
            sort_cap=item.total_market_cap,
        )
        for index, item in enumerate(ordered[: max(0, int(top_n))])
    ]


def describe_sector_ranking_entry(entry: SectorRankingEntry, *, total_boards: int = 0) -> str:
    """B02（v35）/ A03（v36）：榜单条目 → 证据文本（纯函数）。

    A03 四项措辞修正：
    1. 「低估度排名」→「板块估值判定」（排序口径为自身便宜度，不是低估结论）；
    2. 删除「不下低估结论」的立场规避（v36 由分类引擎给出显式分类）；
    3. **先给分类标签**（`entry.label` / `entry.rationale`），再给四维数值；
    4. 排序主键改为代表股 PB 分位中位数（提供时展示）。
    """
    agg = entry.aggregate
    coverage = f"全市场 {total_boards} 个东财行业板块中" if total_boards else ""
    parts: list[str] = []
    # A03：分类标签置前——「先给分类标签，再给四维数值」。
    # 分类引擎的 rationale 本身以「标签：」开头（如「深跌未反转：房屋建设Ⅱ …」），
    # 与方括号标签重复；此处剥离 rationale 的首部标签，避免「【X】X：…」的叠词渲染。
    if entry.label:
        rationale = entry.rationale
        prefix = f"{entry.label}："
        if rationale.startswith(prefix):
            rationale = rationale[len(prefix):]
        parts.append(f"【{entry.label}】{rationale}" if rationale else f"【{entry.label}】")
    parts.append(f"{coverage}板块估值判定第 {entry.rank} 名：板块「{agg.board_name or agg.board_code}」")
    if entry.sort_percentile is not None:
        parts.append(f"代表股 PB 分位中位数 {entry.sort_percentile:.1f}%（相对自身历史）")
    parts.extend([
        f"PB(MRQ) 中位数 {_fmt(agg.pb_mrq_median)}（{agg.pb_mrq_positive_count} 只可比）",
        f"PE(TTM) 中位数 {_fmt(agg.pe_ttm_median)}（{agg.pe_ttm_positive_count} 只可比）",
        f"PS(TTM) 中位数 {_fmt(agg.ps_ttm_median)}",
        f"成分股 {agg.member_count} 只",
        f"板块总市值 {_yi(agg.total_market_cap)}",
    ])
    if agg.loss_count:
        parts.append(f"亏损股 {agg.loss_count} 只（PE ≤ 0，已排除在中位数与排名之外）")
    else:
        parts.append("无亏损股（PE 均为正）")
    if agg.trade_date:
        parts.append(f"截至 {agg.trade_date}")
    parts.append(f"（{SECTOR_RANKING_CALIBER}）")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# A04（v36）：质量初筛闸门 —— 亏损面过高的板块不入深取名单
# ---------------------------------------------------------------------------
# 背景（路线图 §1 缺陷 #2）：低分位信号若不经基本面质量闸门，深跌板块会以「便宜」
# 身份大量挤进候选池（本版报告地产链占候选 6/8）。第一级初筛在**横截面聚合**上完成，
# 零额外取数：亏损面 ≥ 40% 的板块不进深取名单，改列「对照清单」并披露原因，
# 保证报告仍能讨论这类板块（不隐藏、只降级）。
VAL_PRESCREEN_LOSS_RATIO_MAX = 0.40
VAL_PRESCREEN_CONTRAST_TOP_N = 3


def board_loss_ratio(aggregate: SectorValuationAggregate) -> float | None:
    """A04：板块亏损面 = 亏损股数 / 成分股数（纯函数；分母为 0 时 None）。

    分母用 `member_count`（板块当日横截面出现的全部成分股）而非 PB 可比样本数：
    亏损面的经济含义是「这个行业有多少公司不赚钱」，与估值可比性无关。
    """
    if aggregate.member_count <= 0:
        return None
    return aggregate.loss_count / aggregate.member_count


@dataclass(frozen=True)
class PrescreenResult:
    """A04：初筛结果 —— 深取名单 + 对照清单（结构性区分，不靠文本约定）。"""

    deep_fetch: tuple[SectorValuationAggregate, ...]
    contrast: tuple[SectorValuationAggregate, ...]
    excluded_note: str = ""


def prescreen_sector_valuations(
    aggregates: list[SectorValuationAggregate],
    *,
    loss_ratio_max: float = VAL_PRESCREEN_LOSS_RATIO_MAX,
    contrast_top_n: int = VAL_PRESCREEN_CONTRAST_TOP_N,
) -> PrescreenResult:
    """A04：质量初筛闸门（纯函数，无 IO，零额外取数）。

    - 亏损面 **≥ `loss_ratio_max`**（默认 40%）的板块 → 排除出深取名单；
    - 被排除板块按 **PB 中位数升序取前 `contrast_top_n`（默认 3）名** → 对照清单
      （保证报告对地产链这类板块仍有讨论材料）；
    - 亏损面不可得（无成分股）的板块**保守留在深取名单**：缺数据不等于质量差，
      由后续分类引擎按「待定」处理，不在初筛阶段替它做判断。
    """
    deep: list[SectorValuationAggregate] = []
    excluded: list[SectorValuationAggregate] = []
    for item in aggregates:
        ratio = board_loss_ratio(item)
        if ratio is not None and ratio >= loss_ratio_max:
            excluded.append(item)
        else:
            deep.append(item)

    # 对照清单按 PB 中位数升序（最「便宜」者优先），缺 PB 的排末尾，取前 N。
    contrast_ordered = sorted(
        excluded,
        key=lambda item: (
            item.pb_mrq_median is None,
            item.pb_mrq_median if item.pb_mrq_median is not None else 0.0,
        ),
    )
    contrast = tuple(contrast_ordered[: max(0, int(contrast_top_n))])
    note = ""
    if contrast:
        names = "、".join(item.board_name or item.board_code for item in contrast)
        note = (
            f"亏损面 ≥ {loss_ratio_max * 100:.0f}% 的板块未进入深取名单"
            f"（按 PB 中位数升序取前 {contrast_top_n} 名列为对照）：{names}"
        )
    return PrescreenResult(deep_fetch=tuple(deep), contrast=contrast, excluded_note=note)


def describe_prescreen_contrast_entry(aggregate: SectorValuationAggregate) -> str:
    """A04：对照清单条目 → 证据文本（披露亏损面数值与排除原因；纯函数）。"""
    ratio = board_loss_ratio(aggregate)
    ratio_text = "不可得" if ratio is None else f"{ratio * 100:.1f}%"
    parts = [
        f"【对照清单·未入深取】板块「{aggregate.board_name or aggregate.board_code}」",
        f"亏损面 {ratio_text}（{aggregate.loss_count}/{aggregate.member_count} 只成分股亏损）"
        f" ≥ {VAL_PRESCREEN_LOSS_RATIO_MAX * 100:.0f}%，基本面质量未过初筛闸门",
        f"PB(MRQ) 中位数 {_fmt(aggregate.pb_mrq_median)}（{aggregate.pb_mrq_positive_count} 只可比）",
        f"PE(TTM) 中位数 {_fmt(aggregate.pe_ttm_median)}（{aggregate.pe_ttm_positive_count} 只可比）",
        f"板块总市值 {_yi(aggregate.total_market_cap)}",
    ]
    if aggregate.trade_date:
        parts.append(f"截至 {aggregate.trade_date}")
    parts.append("（列入对照仅为披露低分位与高亏损面并存的事实，不作为低估判定依据）")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# B01（v36）：价格位置与趋势 —— 复用既有历史取数，零额外网络请求
# ---------------------------------------------------------------------------
# 背景（路线图 §1.1 缺陷 #3）：「跌得久」与「低估」不可区分，是因为没有价格位置维度。
# `fetch_valuation_history` 本就为分位取 5 年历史（含 CLOSE_PRICE），原料在手；
# 本组只在**同一份历史序列**上计算三个趋势指标，不新增任何网络请求（B02 验收）。
#
# 样本规则：停牌缺口**不插值**，按实际行数计——插值会伪造「连续交易」的假象，
# 使 250 日涨幅在长期停牌股上失真。样本不足时对应字段为 None 并披露实际行数。
TREND_WINDOW_250D = 250
TREND_WINDOW_MA200 = 200
TREND_WINDOW_52W = 250  # 52 周 ≈ 250 个交易日（与 250 日涨幅同窗口）
TREND_MIN_BARS_250D = TREND_WINDOW_250D + 1
TREND_MIN_BARS_MA200 = TREND_WINDOW_MA200


@dataclass(frozen=True)
class PriceTrend:
    """B01：单只个股的价格位置与趋势（历史序列上计算，零额外请求）。

    字段缺失（样本不足）时为 None —— 不填 0、不做插值，由调用方如实披露样本数。
    """

    # 250 日涨幅（%）：最新收盘 / 250 个交易日前收盘 − 1。需 ≥ 251 根 K 线。
    ret_250d: float | None = None
    # 距 52 周最高收盘的回撤（%，≤ 0）：最新收盘 / 窗口内最高收盘 − 1。
    drawdown_52w: float | None = None
    # 是否站上 200 日均线（MA200）：需 ≥ 200 根 K 线才可算。
    above_ma200: bool | None = None
    # 相对 200 日均线的偏离（%）：最新收盘 / MA200 − 1。
    ma200_gap_pct: float | None = None
    # 参与计算的实际行数（披露用；停牌不插值，此处即真实交易根数）。
    bar_count: int = 0
    # 缺失原因（样本不足时如实说明），空串表示全部指标可得。
    note: str = ""


def compute_price_trend(history_rows: list[dict[str, Any]]) -> PriceTrend:
    """B01：历史行情序列 → 价格趋势指标（纯函数，无 IO）。

    输入为 `fetch_valuation_history` 的行（含 `TRADE_DATE` / `CLOSE_PRICE`），
    顺序不限（内部按日期升序排序，容忍源端倒序返回）。

    - `ret_250d`：需 ≥ 251 根 K 线（首尾两个点才构成 250 日区间），否则 None；
    - `drawdown_52w`：窗口取最近 250 根（不足则用全部可得），最高收盘为基准；
    - `above_ma200` / `ma200_gap_pct`：需 ≥ 200 根，MA200 取窗口内收盘均值；
    - 停牌缺口**不插值**，按实际行数计；
    - 无有效收盘价的行（空/非数值）剔除后再计数。
    """
    # 1) 清洗：只保留日期与收盘价都有效的行，并按日期升序（不依赖调用方顺序）。
    cleaned: list[tuple[str, float]] = []
    for row in history_rows or []:
        date = _text(row.get("TRADE_DATE"))[:10]
        close = _num(row.get("CLOSE_PRICE"))
        if date and close is not None and close > 0:
            cleaned.append((date, close))
    cleaned.sort(key=lambda item: item[0])
    bars = len(cleaned)

    if bars == 0:
        return PriceTrend(bar_count=0, note="无可用收盘价记录，价格趋势不可得")

    closes = [close for _, close in cleaned]
    latest = closes[-1]
    notes: list[str] = []

    # 2) 250 日涨幅：首尾两点，需 ≥ 251 根 K 线。
    ret_250d: float | None = None
    if bars >= TREND_MIN_BARS_250D:
        base = closes[-(TREND_WINDOW_250D + 1)]
        if base > 0:
            ret_250d = round((latest / base - 1) * 100, 2)
    else:
        notes.append(f"250 日涨幅需 ≥ {TREND_MIN_BARS_250D} 根 K 线，实际 {bars} 根")

    # 3) 距 52 周最高收盘的回撤：窗口取最近 250 根（不足则全部可得）。
    window_52w = closes[-TREND_WINDOW_52W:] if bars >= TREND_WINDOW_52W else closes
    drawdown_52w: float | None = None
    if window_52w:
        peak = max(window_52w)
        if peak > 0:
            drawdown_52w = round((latest / peak - 1) * 100, 2)

    # 4) 200 日均线位置：需 ≥ 200 根。
    above_ma200: bool | None = None
    ma200_gap_pct: float | None = None
    if bars >= TREND_MIN_BARS_MA200:
        ma200 = sum(closes[-TREND_WINDOW_MA200:]) / TREND_WINDOW_MA200
        if ma200 > 0:
            above_ma200 = latest >= ma200
            ma200_gap_pct = round((latest / ma200 - 1) * 100, 2)
    else:
        notes.append(f"200 日均线需 ≥ {TREND_MIN_BARS_MA200} 根 K 线，实际 {bars} 根")

    return PriceTrend(
        ret_250d=ret_250d,
        drawdown_52w=drawdown_52w,
        above_ma200=above_ma200,
        ma200_gap_pct=ma200_gap_pct,
        bar_count=bars,
        note="；".join(notes),
    )


@dataclass(frozen=True)
class BoardTrendAggregate:
    """A01/B03（v36）：板块级价格趋势聚合（第四维；三指标均为代表股中位数）。

    定义在本模块（趋势数据源侧）：`direction_research` 作为消费方反向导入，
    依赖方向为 direction → valuation，与 `VALUATION_SOURCE_LABEL` 的既有约定一致
    （避免 valuation_evidence 依赖 direction_research 造成循环）。
    """

    ret_250d_median: float | None
    drawdown_52w_median: float | None
    # 站上 200 日均线的代表股数 / 趋势可计算股数（n/N 均须披露；N=0 时 ratio 为 None）。
    above_ma200_count: int
    above_ma200_total: int
    # 趋势全部不可得的代表股（样本不足等），计入披露不进中位数。
    trend_unavailable_symbols: tuple[str, ...] = ()

    @property
    def above_ma200_ratio(self) -> float | None:
        """站上 200 日线比例；无可用样本时 None（不假设 0）。"""
        if self.above_ma200_total <= 0:
            return None
        return self.above_ma200_count / self.above_ma200_total


def describe_price_trend(trend: PriceTrend | None) -> str:
    """B01：趋势 → 文本（缺失如实标注样本数；纯函数）。"""
    if trend is None:
        return "价格趋势不可得（无历史序列）"
    if trend.bar_count == 0:
        return "价格趋势不可得（无可用收盘价记录）"
    parts: list[str] = []
    if trend.ret_250d is not None:
        parts.append(f"250 日涨幅 {trend.ret_250d:+.2f}%")
    if trend.drawdown_52w is not None:
        parts.append(f"距 52 周高点回撤 {trend.drawdown_52w:.2f}%")
    if trend.ma200_gap_pct is not None:
        position = "站上" if trend.above_ma200 else "跌破"
        parts.append(f"{position} 200 日均线（偏离 {trend.ma200_gap_pct:+.2f}%）")
    text = "；".join(parts) if parts else "趋势指标不可得"
    text += f"（{trend.bar_count} 根 K 线）"
    if trend.note:
        text += f"；{trend.note}"
    return text


def percentile_rank(values: list[float | None], current: float | None) -> float | None:
    """严格小于 current 的**正数**样本占比（0-100），保留 1 位小数。

    序列或当前值非正、样本不足 `_MIN_PERCENTILE_SAMPLES` → None（亏损期分位无经济含义）。
    """
    if current is None or current <= 0:
        return None
    sample = [value for value in values if value is not None and value > 0]
    if len(sample) < _MIN_PERCENTILE_SAMPLES:
        return None
    below = sum(1 for value in sample if value < current)
    return round(below / len(sample) * 100, 1)


def _median(values: list[float | None]) -> float | None:
    sample = sorted(value for value in values if value is not None)
    if not sample:
        return None
    count = len(sample)
    middle = count // 2
    result = sample[middle] if count % 2 else (sample[middle - 1] + sample[middle]) / 2
    return round(result, 2)


def _rank_of(values: list[float | None], current: float | None) -> tuple[int, int] | None:
    """current 在**正数**样本中的升序位次与可比样本数 (rank, of)；不可比时 None。

    只用正数样本：亏损股（PE ≤ 0）不可比，纳入会把中位数拉偏、位次失真。
    """
    if current is None or current <= 0:
        return None
    sample = sorted(value for value in values if value is not None and value > 0)
    if not sample:
        return None
    return sum(1 for value in sample if value < current) + 1, len(sample)


def _identity(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_valuation(
    symbol: str, *, history_years: int = 5, peer_limit: int = 20
) -> ValuationSnapshot:
    """取单只 A 股的估值全貌（当前 + 历史分位 + 同业相对）。

    当前值取不到 → 抛 `ValuationError`（调用方如实降级）；
    历史序列或同业取数失败 → 不抛出，降级为分位/同业缺失并在 note 中说明（部分可用优于全丢）。
    """
    code = _normalize_symbol(symbol)

    # C06：当前值页面允许「带日期缓存」降级（注明时点）；历史/同业仍走严格实时路径
    # ——历史序列若降级会与当前值口径错位（禁止无说明拼接不同口径历史），宁缺勿错。
    latest_rows, degraded_note = _query_with_stale(
        _CURRENT_COLUMNS, _build_filter(symbol=code),
        page_size=1, sort_column="TRADE_DATE", desc=True,
    )
    if not latest_rows:
        raise ValuationError(
            f"{VALUATION_SOURCE_LABEL}未返回 {code} 的估值数据", failure_kind="empty_data"
        )
    latest = latest_rows[0]

    trade_date = _text(latest.get("TRADE_DATE"))[:10]
    board_code = _text(latest.get("BOARD_CODE"))
    board_name = _text(latest.get("BOARD_NAME"))
    close_price = _num(latest.get("CLOSE_PRICE"))
    pe_ttm = _num(latest.get("PE_TTM"))
    pe_static = _num(latest.get("PE_LAR"))
    pb_mrq = _num(latest.get("PB_MRQ"))
    ps_ttm = _num(latest.get("PS_TTM"))

    percentiles: dict[str, float | None] = {}
    window_bars = 0
    window_from = ""
    # C06：降级说明排首位（describe_valuation 渲染于「分位口径说明」）。
    notes: list[str] = [degraded_note] if degraded_note else []
    # B02（v36）：趋势与分位共用同一份历史序列，历史失败时二者同时缺失（口径一致）。
    trend: PriceTrend | None = None
    try:
        history = fetch_valuation_history(code, years=history_years)
    except ValuationError as error:
        history = []
        notes.append(f"历史序列取数失败（{error}），无法给出估值分位。")
    if history:
        window_bars = len(history)
        window_from = _text(history[-1].get("TRADE_DATE"))[:10]
        percentiles["pe_ttm"] = percentile_rank([_num(r.get("PE_TTM")) for r in history], pe_ttm)
        percentiles["pb_mrq"] = percentile_rank([_num(r.get("PB_MRQ")) for r in history], pb_mrq)
        percentiles["ps_ttm"] = percentile_rank([_num(r.get("PS_TTM")) for r in history], ps_ttm)
        # B02：在**同一份 history 上**计算价格趋势——不新增任何网络请求（验收以此为准）。
        trend = compute_price_trend(history)
        if pe_ttm is not None and pe_ttm <= 0:
            notes.append("当前 PE(TTM) 为负（亏损），PE 分位无经济含义，已置空。")
        if window_bars < _MIN_PERCENTILE_SAMPLES:
            notes.append(f"历史样本仅 {window_bars} 个交易日，不足 {_MIN_PERCENTILE_SAMPLES}，分位置空。")

    peers: tuple[PeerValuation, ...] = ()
    peer_median: dict[str, float | None] = {}
    peer_median_basis: dict[str, int] = {}
    peer_rank: dict[str, int] = {}
    peer_rank_basis: dict[str, int] = {}
    peer_count = 0
    peer_loss_count = 0
    if board_code and trade_date:
        try:
            peer_list = fetch_peer_valuations(
                board_code, trade_date, exclude_symbol=code, limit=peer_limit
            )
        except ValuationError as error:
            peer_list = []
            notes.append(f"同业取数失败（{error}），无法给出横向比较。")
        if peer_list:
            peers = tuple(peer_list)
            peer_count = len(peer_list)
            peer_loss_count = sum(1 for p in peer_list if p.pe_ttm is not None and p.pe_ttm <= 0)
            for metric, current in (("pe_ttm", pe_ttm), ("pb_mrq", pb_mrq), ("ps_ttm", ps_ttm)):
                positives = [
                    value
                    for value in (getattr(p, metric) for p in peer_list)
                    if value is not None and value > 0
                ]
                if positives:
                    peer_median[metric] = _median(positives)
                    peer_median_basis[metric] = len(positives)
                ranked = _rank_of([getattr(p, metric) for p in peer_list], current)
                if ranked is not None:
                    peer_rank[metric], peer_rank_basis[metric] = ranked

    snapshot = {
        "symbol": code,
        "trade_date": trade_date,
        "pe_ttm": pe_ttm,
        "pe_static": pe_static,
        "pb_mrq": pb_mrq,
        "ps_ttm": ps_ttm,
        "peg": _num(latest.get("PEG_CAR")),
        "total_market_cap": _num(latest.get("TOTAL_MARKET_CAP")),
        "board_code": board_code,
    }
    return ValuationSnapshot(
        symbol=code,
        name=_text(latest.get("SECURITY_NAME_ABBR")),
        board_code=board_code,
        board_name=board_name,
        trade_date=trade_date,
        close_price=close_price,
        pe_ttm=pe_ttm,
        pe_static=pe_static,
        pb_mrq=pb_mrq,
        ps_ttm=ps_ttm,
        peg=_num(latest.get("PEG_CAR")),
        pcf_ocf_ttm=_num(latest.get("PCF_OCF_TTM")),
        total_market_cap=_num(latest.get("TOTAL_MARKET_CAP")),
        float_market_cap=_num(latest.get("NOTLIMITED_MARKETCAP_A")),
        total_shares=_num(latest.get("TOTAL_SHARES")),
        percentiles=percentiles,
        percentile_window_bars=window_bars,
        percentile_window_from=window_from,
        percentile_note=" ".join(notes),
        trend=trend,
        peers=peers,
        peer_count=peer_count,
        peer_median=peer_median,
        peer_median_basis=peer_median_basis,
        peer_rank=peer_rank,
        peer_rank_basis=peer_rank_basis,
        peer_loss_count=peer_loss_count,
        identity=_identity(snapshot),
        degraded_note=degraded_note,
    )


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "无数据" if value is None else f"{value:.{digits}f}{suffix}"


def _yi(value: float | None) -> str:
    """元 → 亿元（保留 2 位）；缺失如实标注。"""
    return "无数据" if value is None else f"{value / 1e8:.2f} 亿元"


def describe_valuation(snapshot: ValuationSnapshot) -> str:
    """把估值快照渲染为证据包 S5 的纯文本（口径逐项标注，缺失项如实写「无数据」）。"""
    lines: list[str] = [
        f"估值快照（{VALUATION_SOURCE_LABEL}，截至 {snapshot.trade_date or '未知'}，"
        f"行业：{snapshot.board_name or '未知'}）：",
        f"- 估值：PE(TTM) {_fmt(snapshot.pe_ttm)}；PE(静态/最近年报) {_fmt(snapshot.pe_static)}；"
        f"PB(MRQ) {_fmt(snapshot.pb_mrq)}；PS(TTM) {_fmt(snapshot.ps_ttm)}；"
        f"PEG {_fmt(snapshot.peg)}；PCF(经营现金流 TTM) {_fmt(snapshot.pcf_ocf_ttm)}",
    ]
    scale = (
        f"- 规模：总市值 {_yi(snapshot.total_market_cap)}；"
        f"流通市值 {_yi(snapshot.float_market_cap)}"
    )
    if snapshot.total_shares:
        scale += f"；总股本 {snapshot.total_shares / 1e8:.2f} 亿股"
    lines.append(scale)
    if snapshot.degraded_note:
        # C06：降级说明必须在证据正文可见（不藏在分位说明里）。
        lines.append(f"- 数据时效警告：{snapshot.degraded_note}")

    if snapshot.percentile_window_bars:
        window = f"近 {snapshot.percentile_window_bars} 个交易日（{snapshot.percentile_window_from} 起）"
        lines.append(
            f"- 历史分位（{window}）：PE(TTM) {_fmt(snapshot.percentiles.get('pe_ttm'), 1, '%')}；"
            f"PB {_fmt(snapshot.percentiles.get('pb_mrq'), 1, '%')}；"
            f"PS {_fmt(snapshot.percentiles.get('ps_ttm'), 1, '%')}"
            "（分位 = 历史正数样本中低于当前值的占比，越高越贵）"
        )
    if snapshot.percentile_note:
        lines.append(f"- 分位口径说明：{snapshot.percentile_note}")

    if snapshot.peer_count:
        med = snapshot.peer_median
        med_basis = snapshot.peer_median_basis
        rank = snapshot.peer_rank
        rank_basis = snapshot.peer_rank_basis

        def _median_text(key: str) -> str:
            basis = med_basis.get(key)
            return _fmt(med.get(key)) + (f"（{basis} 只可比）" if basis else "")

        rank_text = "；".join(
            f"{label} 第 {rank[key]}/{rank_basis.get(key, snapshot.peer_count)}"
            for key, label in (("pe_ttm", "PE(TTM)"), ("pb_mrq", "PB"), ("ps_ttm", "PS"))
            if key in rank
        )
        lines.append(
            f"- 同业（{snapshot.board_name}，{snapshot.peer_count} 只，当日）："
            f"中位数 PE(TTM) {_median_text('pe_ttm')} / PB {_median_text('pb_mrq')} / "
            f"PS {_median_text('ps_ttm')}"
            + (f"；本股升序位次：{rank_text}" if rank_text else "")
        )
        if snapshot.peer_loss_count:
            lines.append(
                f"- 同业亏损股：{snapshot.peer_loss_count} 只 PE 为负（亏损），"
                "已排除在中位数与位次之外，不参与估值比较。"
            )
        lowest = [
            f"{peer.name}(PE {peer.pe_ttm:.1f})"
            for peer in snapshot.peers
            if peer.pe_ttm is not None and peer.pe_ttm > 0
        ][:5]
        if lowest:
            lines.append("- 同业样本（PE 最低 5 只，均为盈利）：" + "、".join(lowest))

    lines.append("- 口径提示：本证据不含股息率（数据源无该字段），不得虚构。")
    return "\n".join(lines)


def raw_locator_for_valuation(snapshot: ValuationSnapshot) -> str:
    """对齐 ADR-0004：保留原始来源与关键字段，便于复核。"""
    return json.dumps(
        {
            "api": "https://data.eastmoney.com/gzfx/",
            "report": _REPORT_NAME,
            "symbol": snapshot.symbol,
            "trade_date": snapshot.trade_date,
            "pe_ttm": snapshot.pe_ttm,
            "pb_mrq": snapshot.pb_mrq,
            "ps_ttm": snapshot.ps_ttm,
            "board_code": snapshot.board_code,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# B03（v35）：榜单板块代表股分位锚点
# ---------------------------------------------------------------------------
# 设计边界（路线图 §2）：板块级「处于历史低位」由**市值前列代表股代理**，不是全成分
# 历史分位。代理口径必须在证据中如实声明，不得冒充全成分结论。
SECTOR_PROXY_DISCLAIMER = (
    "板块分位**由市值前列代表股代理**，非全成分历史分位——代表股数量与可得分位股数"
    "随附录披露，不得据此推断全板块成分股的分位分布。"
)
REPRESENTATIVE_MAX_PER_BOARD = 5
# B04（v35）取数预算：只给榜单前 N 个板块补代表股分位，且全批次总股数硬封顶。
# 依据：东财快速重复请求会临时断连，代表股逐股取数必须可预期地封顶。
SECTOR_PROXY_BOARD_LIMIT = 5
SECTOR_PROXY_STOCK_BUDGET = 25


@dataclass(frozen=True)
class RepresentativeStock:
    """代表股的估值分位锚点（分位取不到如实为空，不猜测）。

    B03（v36）：新增 `trend` —— 与分位同一次取数的价格趋势（`fetch_valuation` 内部
    在同一份历史序列上算好），用于板块第四维聚合；趋势缺失如实为 None。
    """

    symbol: str
    name: str
    total_market_cap: float | None
    pe_ttm: float | None
    pb_mrq: float | None
    pe_ttm_percentile: float | None
    pb_mrq_percentile: float | None
    percentile_window_bars: int
    trade_date: str
    percentile_note: str = ""
    trend: PriceTrend | None = None

    @property
    def comparable(self) -> bool:
        """PB 分位可得即视为可比（PB 分位为板块代理的主指标）。"""
        return self.pb_mrq_percentile is not None


@dataclass(frozen=True)
class BoardPercentileAppendix:
    """板块分位附录：代表股分位分布聚合（B03 v36 扩展趋势聚合）。"""

    board_code: str
    board_name: str
    representative_count: int
    comparable_count: int
    pb_percentile_median: float | None
    pe_percentile_median: float | None
    lowest_three: tuple[RepresentativeStock, ...]
    incomparable_symbols: tuple[str, ...]
    trade_date: str
    disclaimer: str = SECTOR_PROXY_DISCLAIMER
    # —— B03（v36）第四维：板块趋势聚合（代表股中位数；不足者不进中位数） ——
    ret_250d_median: float | None = None
    drawdown_52w_median: float | None = None
    above_ma200_count: int = 0
    above_ma200_total: int = 0
    trend_unavailable_symbols: tuple[str, ...] = ()

    @property
    def above_ma200_ratio(self) -> float | None:
        """站上 200 日线比例；无趋势样本时 None（不假设 0）。"""
        if self.above_ma200_total <= 0:
            return None
        return self.above_ma200_count / self.above_ma200_total

    def to_trend_aggregate(self) -> BoardTrendAggregate:
        """B03：转为 A01 的板块趋势聚合结构（direction_research 侧消费）。"""
        return BoardTrendAggregate(
            ret_250d_median=self.ret_250d_median,
            drawdown_52w_median=self.drawdown_52w_median,
            above_ma200_count=self.above_ma200_count,
            above_ma200_total=self.above_ma200_total,
            trend_unavailable_symbols=self.trend_unavailable_symbols,
        )


def select_representative_stocks(
    rows: list[CrossSectionRow],
    board_code: str,
    *,
    limit: int = REPRESENTATIVE_MAX_PER_BOARD,
) -> list[CrossSectionRow]:
    """B03：从横截面行中选该板块市值前列的代表股（纯函数，无额外网络请求）。

    市值缺失的个股排在末尾（不假设其规模），保证有市值数据的成分优先入选。
    """
    members = [item for item in rows if item.board_code == board_code and item.symbol]
    ordered = sorted(
        members,
        key=lambda item: (
            item.total_market_cap is None,
            -(item.total_market_cap or 0.0),
        ),
    )
    return ordered[: max(0, int(limit))]


def aggregate_board_percentiles(
    board_code: str,
    board_name: str,
    stocks: list[RepresentativeStock],
    *,
    trade_date: str = "",
) -> BoardPercentileAppendix:
    """B03：代表股分位 → 板块分位附录（纯函数）。

    「分位可得股数」「PB 分位中位数」「分位最低 3 只」三项为附录核心；分位置空的股
    进 `incomparable_symbols` 如实标注不可比，不拉进中位数。

    B03（v36）新增第四维趋势聚合：
    - `ret_250d_median` / `drawdown_52w_median`：代表股**趋势可得者**的中位数；
    - `above_ma200_count` / `above_ma200_total`：站上 MA200 的股数 / 趋势可计算股数（n/N 披露）；
    - 趋势不可得（`trend is None` 或三指标全 None）的股进 `trend_unavailable_symbols`，
      **不计入中位数与比例**的分子分母——缺数据不等于「没站上均线」。
    """
    comparable_stocks = [item for item in stocks if item.pb_mrq_percentile is not None]
    pb_values = [item.pb_mrq_percentile for item in comparable_stocks]
    pe_comparable = [item.pe_ttm_percentile for item in stocks if item.pe_ttm_percentile is not None]
    lowest = sorted(
        comparable_stocks,
        key=lambda item: (item.pb_mrq_percentile if item.pb_mrq_percentile is not None else 100.0),
    )[:3]

    # —— B03：趋势聚合（只统计趋势可得的代表股） ——
    trend_available = [item for item in stocks if _trend_usable(item.trend)]
    ret_values = [
        item.trend.ret_250d
        for item in trend_available
        if item.trend is not None and item.trend.ret_250d is not None
    ]
    dd_values = [
        item.trend.drawdown_52w
        for item in trend_available
        if item.trend is not None and item.trend.drawdown_52w is not None
    ]
    ma_flags = [
        item.trend.above_ma200
        for item in trend_available
        if item.trend is not None and item.trend.above_ma200 is not None
    ]
    trend_missing = tuple(item.symbol for item in stocks if not _trend_usable(item.trend))

    return BoardPercentileAppendix(
        board_code=board_code,
        board_name=board_name,
        representative_count=len(stocks),
        comparable_count=len(comparable_stocks),
        # 分位中位数直接对分位值取中位数（分位本身已归一，不需正数过滤）。
        pb_percentile_median=_median(pb_values),
        pe_percentile_median=_median(pe_comparable),
        lowest_three=tuple(lowest),
        incomparable_symbols=tuple(
            item.symbol for item in stocks if item.pb_mrq_percentile is None
        ),
        trade_date=trade_date or next((item.trade_date for item in stocks if item.trade_date), ""),
        ret_250d_median=_median(ret_values),
        drawdown_52w_median=_median(dd_values),
        above_ma200_count=sum(1 for flag in ma_flags if flag),
        above_ma200_total=len(ma_flags),
        trend_unavailable_symbols=trend_missing,
    )


def _trend_usable(trend: PriceTrend | None) -> bool:
    """B03：趋势是否可用（三项指标至少应有一项独立于 MA200 的可得值）。

    仅有 `bar_count` 而无任何指标（样本过少）视为不可用——它无法为第四维提供信息。
    """
    if trend is None or trend.bar_count == 0:
        return False
    return any(
        value is not None
        for value in (trend.ret_250d, trend.drawdown_52w, trend.ma200_gap_pct)
    )


def fetch_board_percentile_appendix(
    rows: list[CrossSectionRow],
    board_code: str,
    board_name: str,
    *,
    limit: int = REPRESENTATIVE_MAX_PER_BOARD,
    fetch: Any = None,
) -> tuple[BoardPercentileAppendix, list[str]]:
    """B03：代表股逐股取 5 年分位并聚合为板块附录（返回 (附录, 失败说明列表)）。

    - **逐股串行**（不并发）：东财快速重复请求会临时断连，串行 + `_query` 的失败冷却
      可避免重试轰炸（B04 预算与断连防护）；
    - 单股失败不中断整批：该股记为缺数（不计入代表股），失败原因汇总返回由调用方
      在缺口声明中说明；
    - `fetch` 可注入（测试用），默认 `fetch_valuation`。
    """
    picker = fetch or fetch_valuation
    representatives = select_representative_stocks(rows, board_code, limit=limit)
    stocks: list[RepresentativeStock] = []
    failures: list[str] = []
    for item in representatives:
        # B04：冷却期内不发起请求（不重试轰炸），该股直接降级为缺口。
        if fetch is None and symbol_in_cooldown(item.symbol):
            failures.append(
                f"{item.name or item.symbol}({item.symbol}) 估值源处于失败冷却期，"
                "本次跳过取数（避免重复请求加重断连）"
            )
            continue
        try:
            snapshot = picker(item.symbol)
        except ValuationError as exc:
            failures.append(f"{item.name or item.symbol}({item.symbol}) 估值分位取数失败：{exc}")
            continue
        stocks.append(
            RepresentativeStock(
                symbol=snapshot.symbol,
                name=snapshot.name or item.name,
                total_market_cap=snapshot.total_market_cap or item.total_market_cap,
                pe_ttm=snapshot.pe_ttm,
                pb_mrq=snapshot.pb_mrq,
                pe_ttm_percentile=snapshot.percentiles.get("pe_ttm"),
                pb_mrq_percentile=snapshot.percentiles.get("pb_mrq"),
                percentile_window_bars=snapshot.percentile_window_bars,
                trade_date=snapshot.trade_date,
                percentile_note=snapshot.percentile_note,
                # B03：趋势取自同一快照（与分位同一次取数，零额外请求）。
                trend=snapshot.trend,
            )
        )
    return aggregate_board_percentiles(board_code, board_name, stocks), failures


def describe_board_percentile_appendix(appendix: BoardPercentileAppendix) -> str:
    """B03：板块分位附录 → 证据文本（含代理口径声明；纯函数）。"""
    parts: list[str] = [
        f"板块「{appendix.board_name or appendix.board_code}」代表股分位附录："
        f"取市值前列 {appendix.representative_count} 只，其中 {appendix.comparable_count} 只分位可得"
    ]
    if appendix.pb_percentile_median is not None:
        parts.append(f"代表股 PB 分位中位数 {appendix.pb_percentile_median:.1f}%")
    else:
        parts.append("代表股 PB 分位中位数 无数据（可得样本为空）")
    if appendix.pe_percentile_median is not None:
        parts.append(f"PE 分位中位数 {appendix.pe_percentile_median:.1f}%")
    lowest = appendix.lowest_three
    if lowest:
        lowest_text = "、".join(
            f"{item.name}({item.symbol}) PB 分位 "
            f"{item.pb_mrq_percentile:.1f}%（截至 {item.trade_date or '未知'}）"
            for item in lowest
        )
        parts.append("PB 分位最低 3 只：" + lowest_text)
    if appendix.incomparable_symbols:
        parts.append(
            f"分位置空（历史样本不足 {_MIN_PERCENTILE_SAMPLES} 个交易日或指标非正，不可比）："
            + "、".join(appendix.incomparable_symbols)
        )
    # B03：第四维趋势聚合（缺失代表股如实披露，不进中位数与比例）。
    trend_parts: list[str] = []
    if appendix.ret_250d_median is not None:
        trend_parts.append(f"代表股 250 日涨幅中位数 {appendix.ret_250d_median:+.2f}%")
    if appendix.drawdown_52w_median is not None:
        trend_parts.append(f"距 52 周高点回撤中位数 {appendix.drawdown_52w_median:.2f}%")
    if appendix.above_ma200_ratio is not None:
        trend_parts.append(
            f"站上 200 日均线 {appendix.above_ma200_count}/{appendix.above_ma200_total} 只"
        )
    if trend_parts:
        parts.append("板块趋势：" + "；".join(trend_parts))
    else:
        parts.append("板块趋势：不可得（全部代表股历史样本不足）")
    if appendix.trend_unavailable_symbols:
        parts.append(
            "趋势不可得（样本不足，未计入中位数）：" + "、".join(appendix.trend_unavailable_symbols)
        )
    if appendix.trade_date:
        parts.append(f"截至 {appendix.trade_date}")
    parts.append(f"（{appendix.disclaimer}）")
    return "；".join(parts)

