"""东财龙虎榜席位内核（P2，纯 stdlib，零新增依赖）。

写法对齐 `news_evidence.py`：三次重试 + 退避 + UA/Referer + 模块级 TTL 缓存 +
失败冷却；取数失败抛 `LhbError` 由调用方**显式降级**，不返回空壳、不生成证据。

## 数据源与口径（2026-09-16 实测，见 tests/fixtures/youzi/README.md）

| 用途 | report 名 | 实测规模 |
| --- | --- | --- |
| 当日上榜股票列表 | `RPT_DAILYBILLBOARD_DETAILS` | 71 行 |
| 单票买入席位 | `RPT_BILLBOARD_DAILYDETAILSBUY` | 365 行 / 118 家 |
| 单票卖出席位 | `RPT_BILLBOARD_DAILYDETAILSSELL` | 字段集与 BUY 完全一致 |
| 席位档案（某营业部历史） | `RPT_OPERATEDEPT_TRADE_DETAILS` | 支持按代码/精确名过滤 |

## 三条不可让步的规则

1. **禁止把数据商统计当我们算出来的东西。** `RISE_PROBABILITY_3DAY`、
   `TOTAL_BUYER_SALESTIMES_3DAY`，以及 `EXPLAIN` 文本里夹带的「成功率 28.87%」
   都属于东财自己的口径。它们**可以**留在原文用于说明，**绝不**解析成我方字段、
   **绝不**进排序键（方案铁律 6）。
2. **桶不是营业部。** `机构专用` / `沪股通专用` / `深股通专用` / `*总部` 一律不当游资。
3. **只展示路径事实。** 席位档案给「某日出现在哪只股票的哪一侧」，不给收益率排行。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date as _date

from investment_steward_core import feed_health

logger = logging.getLogger(__name__)

_EM_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"

#: report 名（P0 A03 冻结；方案 §七「剩余：钉卖出席位 report 名」已在此钉死）。
REPORT_DAILY_BILLBOARD = "RPT_DAILYBILLBOARD_DETAILS"
REPORT_SEAT_BUY = "RPT_BILLBOARD_DAILYDETAILSBUY"
REPORT_SEAT_SELL = "RPT_BILLBOARD_DAILYDETAILSSELL"
REPORT_OPERATEDEPT_TRADE = "RPT_OPERATEDEPT_TRADE_DETAILS"

#: 方向常量：与 seat_book 的主键 `(trading_day, security_code, operatedept_code, direction)` 配套。
DIRECTION_BUY = "buy"
DIRECTION_SELL = "sell"

#: 明确禁止落库/进排序的数据商字段（P0 A04）。测试会断言它们不出现在输出结构里。
FORBIDDEN_FIELDS: tuple[str, ...] = (
    "RISE_PROBABILITY_3DAY",
    "TOTAL_BUYER_SALESTIMES_3DAY",
)

#: `EXPLAIN` 文本里夹带的「成功率 28.87%」——同样不许当成我方数值。
_SUCCESS_RATE_IN_TEXT = re.compile(r"成功率\s*[\d.]+%")

_USER_AGENT = "InvestmentSteward/0.1"
_REFERER = "https://data.eastmoney.com/"
_REQUEST_TIMEOUT = 20
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

_CACHE_TTL_SECONDS = 300.0
_FAIL_COOLDOWN_SECONDS = 300.0
_cache: dict[str, list[dict[str, object]]] = {}
_cache_at: dict[str, float] = {}
_fail_cache: dict[str, float] = {}


class LhbError(RuntimeError):
    """龙虎榜取数或结构异常，应显式降级而非伪造证据。"""


#: 东财龙虎榜个股详情页的 URL 形态（**本产品构造**，不是上游返回的字段）。
#:
#: G03（桌面端升级路线图 2026-09-18）：上游 `RPT_DAILYBILLBOARD_DETAILS` 与
#: `RPT_BILLBOARD_DAILYDETAILSBUY/SELL` 的**任何一行都没有 URL 字段**（2026-09-19 按夹具
#: 全键核对：TRADE_ID/SECUCODE/SECURITY_INNER_CODE/BUY_SEAT_NEW… 无 URL 类键），
#: 所以「回到原始披露」的回链只能由**交易日 + 证券代码**确定性拼出。形态已实测（2026-09-19）：
#: `https://data.eastmoney.com/stock/lhb,2026-09-16,000001.html` → HTTP 200 且页面含该代码；
#: 另一种写法 `.../lhb/2026-09-16/000001.html` → 404，**不要**改成那种。
PROVIDER_PAGE_URL = "https://data.eastmoney.com/stock/lhb,{day},{code}.html"


def provider_page_url(trading_day: str, security_code: str) -> str:
    """由「交易日 + 证券代码」拼出可点的东财原始披露页。

    任一参数缺失/不合形（交易日不是**真实存在的** 8 位日期、代码不是 6 位数字）就返回
    **空串**：不留半截 URL、不用当天日期兜底、不用不存在的日期凑——猜出来的回链比没有回链更糟。
    """
    day = normalise_trade_date(trading_day)
    code = str(security_code or "").strip()
    if not re.fullmatch(r"\d{8}", day) or not re.fullmatch(r"\d{6}", code):
        return ""
    try:
        date_cls = _date
        date_cls(int(day[:4]), int(day[4:6]), int(day[6:]))
    except ValueError:
        # `20261332` 这种「8 位数字但不是日期」不能拼进 URL：页面必然 404，等于给假回链。
        return ""
    return PROVIDER_PAGE_URL.format(day=f"{day[:4]}-{day[4:6]}-{day[6:]}", code=code)


# —— 数据类型 ——


@dataclass(frozen=True)
class BillboardRow:
    """当日上榜的一只股票（`RPT_DAILYBILLBOARD_DETAILS` 一行，只取可核对字段）。"""

    trading_day: str
    security_code: str
    security_name: str
    explanation: str
    change_rate: float | None
    close_price: float | None
    billboard_buy_amt: float | None
    billboard_sell_amt: float | None
    billboard_net_amt: float | None
    accum_amount: float | None
    trade_market: str
    #: 原文里夹带的统计（如「3家机构买入，成功率28.87%」）。**只作说明**，不进任何计算。
    explain_note: str
    #: G03：榜单行的扩展字段（原口径单位，缺值保持 None，**不填 0**）。
    #: `TURNOVERRATE` 是百分比（%）、`FREE_MARKET_CAP` 是自由流通市值（元）——与
    #: `youzi_normalize.normalize_billboard_row` 的 unit 声明一致（pct / yuan）。
    turnover_rate: float | None = None
    free_market_cap: float | None = None

    @property
    def source_url(self) -> str:
        """该行对应的东财原始披露页（构造式回链，缺参数时为空串）。"""
        return provider_page_url(self.trading_day, self.security_code)


@dataclass(frozen=True)
class SeatRow:
    """一条席位明细（买入或卖出席位，两侧字段集一致）。"""

    trading_day: str
    security_code: str
    operatedept_code: str
    operatedept_name: str
    direction: str
    explanation: str
    buy: float | None
    sell: float | None
    net: float | None
    change_rate: float | None
    close_price: float | None

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        """主键 `(交易日, 代码, 席位代码, 方向)`——P0 A03 冻结值。"""
        return (self.trading_day, self.security_code, self.operatedept_code, self.direction)

    @property
    def source_url(self) -> str:
        """该席位行所属的东财原始披露页（构造式回链，缺参数时为空串）。"""
        return provider_page_url(self.trading_day, self.security_code)


# —— 解析辅助 ——


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _float_or_none(row: dict[str, object], key: str) -> float | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalise_trade_date(raw: object) -> str:
    """东财返回 `2026-09-16 00:00:00`；归一化为 `20260916`（与 cffex_feed 一致）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    head = text.split(" ")[0].split("T")[0]
    if re.fullmatch(r"\d{8}", head):
        return head
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
        return head.replace("-", "")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", head):
        return head.replace("/", "")
    return head


def _extract_result_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    """从东财响应里取出 data 列表；失败响应（code 9501/9201）一律当空。"""
    if payload.get("success") is not True:
        return []
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def payload_error(payload: dict[str, object]) -> str:
    """把失败响应转成可读原因（`不支持like查询` / `返回数据为空` 等）。

    这些 msg 是**数据源的真实约束**，要能原样透传到 UI 与日志，
    否则「查不到」会被误读成「今天没上榜」。
    """
    if payload.get("success") is True:
        return ""
    message = str(payload.get("message") or "").strip()
    code = payload.get("code")
    if message:
        return f"{message}（code={code}）"
    return f"东财返回失败响应（code={code}）"


# —— 解析（纯函数） ——


def parse_billboard_rows(payload: dict[str, object]) -> list[BillboardRow]:
    """解析当世上榜列表。**不含**任何数据商概率字段。"""
    return parse_billboard_items(_extract_result_rows(payload))


def parse_billboard_items(items: list[dict[str, object]]) -> list[BillboardRow]:
    """从已取出的行解析上榜股票（供取数路径直接复用，避免伪造 payload 外壳）。"""
    rows: list[BillboardRow] = []
    for item in items:
        code = _text(item, "SECURITY_CODE")
        if not code:
            continue
        rows.append(
            BillboardRow(
                trading_day=normalise_trade_date(item.get("TRADE_DATE")),
                security_code=code,
                security_name=_text(item, "SECURITY_NAME_ABBR"),
                explanation=_text(item, "EXPLANATION"),
                change_rate=_float_or_none(item, "CHANGE_RATE"),
                close_price=_float_or_none(item, "CLOSE_PRICE"),
                billboard_buy_amt=_float_or_none(item, "BILLBOARD_BUY_AMT"),
                billboard_sell_amt=_float_or_none(item, "BILLBOARD_SELL_AMT"),
                billboard_net_amt=_float_or_none(item, "BILLBOARD_NET_AMT"),
                accum_amount=_float_or_none(item, "ACCUM_AMOUNT"),
                trade_market=_text(item, "TRADE_MARKET"),
                explain_note=_text(item, "EXPLAIN"),
                # G03：扩展字段按原口径读入（缺失/非数字保持 None）。
                turnover_rate=_float_or_none(item, "TURNOVERRATE"),
                free_market_cap=_float_or_none(item, "FREE_MARKET_CAP"),
            )
        )
    return rows


def parse_seat_rows(payload: dict[str, object], direction: str) -> list[SeatRow]:
    """解析席位明细（入参为原始响应）。"""
    return parse_seat_items(_extract_result_rows(payload), direction)


def parse_seat_items(items: list[dict[str, object]], direction: str) -> list[SeatRow]:
    """从已取出的行解析席位明细。`direction` 必须是 `buy` / `sell`。"""
    if direction not in (DIRECTION_BUY, DIRECTION_SELL):
        raise ValueError(f"direction 必须是 {DIRECTION_BUY} 或 {DIRECTION_SELL}：{direction!r}")
    rows: list[SeatRow] = []
    for item in items:
        code = _text(item, "OPERATEDEPT_CODE")
        if not code:
            continue
        rows.append(
            SeatRow(
                trading_day=normalise_trade_date(item.get("TRADE_DATE")),
                security_code=_text(item, "SECURITY_CODE"),
                operatedept_code=code,
                operatedept_name=_text(item, "OPERATEDEPT_NAME"),
                direction=direction,
                explanation=_text(item, "EXPLANATION"),
                buy=_float_or_none(item, "BUY"),
                sell=_float_or_none(item, "SELL"),
                net=_float_or_none(item, "NET"),
                change_rate=_float_or_none(item, "CHANGE_RATE"),
                close_price=_float_or_none(item, "CLOSE_PRICE"),
            )
        )
    return rows


def seats_for_security(rows: list[SeatRow], security_code: str, direction: str) -> list[SeatRow]:
    """Filter one security/side, sorting by BUY or SELL, with missing amounts last."""
    if direction not in (DIRECTION_BUY, DIRECTION_SELL):
        raise ValueError(f"invalid direction: {direction!r}")
    target = security_code.strip()
    picked = [r for r in rows if r.security_code == target and r.direction == direction]

    def amount_key(row: SeatRow) -> tuple[bool, float]:
        # NET is BUY - SELL on both reports, not the ranking amount for either side.
        amount = row.buy if direction == DIRECTION_BUY else row.sell
        return (amount is None, -amount if amount is not None else 0.0)

    return sorted(picked, key=amount_key)


def _http_get_json(params: dict[str, object]) -> dict[str, object]:
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"{_EM_BASE}?{query}"
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": _USER_AGENT, "Referer": _REFERER}
            )
            # F02：逐次尝试埋点（含重试的第几次），失败与「响应不可用」都记。
            with feed_health.attempt(
                feed_health.SOURCE_LHB_EASTMONEY, url, params, attempt_index=attempt + 1
            ):
                with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                    payload = response.read().decode("utf-8")
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise LhbError("东财龙虎榜接口返回结构异常")
                return parsed
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise LhbError(f"东财龙虎榜请求失败：{last_error}")


def _fetch_rows(
    report_name: str, day: str, *, page_size: int = 500, extra: dict[str, object] | None = None
) -> list[dict[str, object]]:
    cache_key = f"{report_name}|{day}|{page_size}|{json.dumps(extra or {}, sort_keys=True)}"
    now = time.monotonic()
    cached_at = _cache_at.get(cache_key)
    if cached_at is not None and now - cached_at < _CACHE_TTL_SECONDS:
        return _cache[cache_key]
    fail_at = _fail_cache.get(cache_key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise LhbError(f"东财龙虎榜近期失败，处于冷却期：{report_name}")

    filter_expr = f"(TRADE_DATE='{day[:4]}-{day[4:6]}-{day[6:]}')"
    params: dict[str, object] = {
        "reportName": report_name,
        "columns": "ALL",
        "pageNumber": 1,
        "pageSize": page_size,
        "filter": filter_expr,
        "source": "WEB",
        "client": "WEB",
    }
    if extra:
        params.update(extra)

    try:
        payload = _http_get_json(params)
        reason = payload_error(payload)
        if reason:
            raise LhbError(f"{report_name}：{reason}")
        rows = _extract_result_rows(payload)
    except LhbError:
        _fail_cache[cache_key] = time.monotonic()
        raise
    _cache[cache_key] = rows
    _cache_at[cache_key] = time.monotonic()
    return rows


def fetch_billboard(trading_day: str) -> list[BillboardRow]:
    """某交易日的上榜股票列表。无数据抛 `LhbError`（由调用方降级）。"""
    parsed = parse_billboard_items(_fetch_rows(REPORT_DAILY_BILLBOARD, trading_day))
    if not parsed:
        raise LhbError(f"东财 {trading_day} 无龙虎榜上榜数据（非交易日或尚未披露）")
    return parsed


# —— Y2-07：完整分页取数（共享预算 + 单次 transport）——
#
# `_fetch_rows` 只用单页 `pageSize=500`；当某报告的 `count` 超过单页容量时
# 会静默丢行（`em_operatedept_trade_10026937.json` 实测 `count=1116, pages=23`
# 即旁证）。跨日事实不允许建立在可能缺页的输入上，因此另给一条**完整分页**路径：
# 走 `youzi_paging.PagingKernel`（进程内并发 2、共享 40 次尝试/30 秒预算、同键合并、
# 有界缓存）+ `youzi_transport.eastmoney_once`（单次 HTTP，无内部重试）。
#
# 该路径**不替换** `_fetch_rows`：一期消费者语义不变，二期跨日事实才用它。

#: 分页报告的稳定排序键。必须显式给出，避免页间漂移导致重/漏行。
#: `SECURITY_CODE` 在 buy/sell 明细里仍可能同码多行（同日多原因），故再以
#: `OPERATEDEPT_CODE` 兜底 tie-breaker，保证页序确定。
_SORT_COLUMNS: dict[str, str] = {
    REPORT_DAILY_BILLBOARD: "SECURITY_CODE",
    REPORT_SEAT_BUY: "SECURITY_CODE,OPERATEDEPT_CODE",
    REPORT_SEAT_SELL: "SECURITY_CODE,OPERATEDEPT_CODE",
}

#: 与 `_SORT_COLUMNS` 逐项配平的排序方向。
#:
#: ★ 2026-09-18 实测（`RPT_BILLBOARD_DAILYDETAILSBUY`，`TRADE_DATE='2026-08-14'`）：
#: `sortColumns` / `sortTypes` 是**两个等长逗号串**，不是「单值方向 + 多列」：
#:
#: | sortColumns                        | sortTypes  | 上游返回                            |
#: | ---------------------------------- | ---------- | ----------------------------------- |
#: | `SECURITY_CODE,OPERATEDEPT_CODE`   | `1`        | code=9501 排序字段和顺序数量不一致  |
#: | `SECURITY_CODE,OPERATEDEPT_CODE`   | `[1,1]`    | code=9501 排序顺序字段不能为非数字  |
#: | `SECURITY_CODE,OPERATEDEPT_CODE`   | `"1,1"`    | **ok，count=369 / pages=74**        |
#:
#: 旧实现写死 `sortTypes: 1`，于是**多列排序的两个报告恒被上游拒绝**：`success=false`
#: → `PagingKernel` 判 `upstream_error` → `coverage=fetch_failed` → 跨日事实一条都生成
#: 不出来（症状为「直连有数据、经内核 0 行」）。方向统一取 1（升序），**只要与列数等长
#: 即可保证页序确定**；升/降本身不影响稳定性，故不引入按报告配置的复杂度。
_SORT_TYPES: dict[str, str] = {name: ",".join(["1"] * len(cols.split(","))) for name, cols in _SORT_COLUMNS.items()}

_paging_kernel: object | None = None
_paging_kernel_lock = threading.Lock()


def paging_policy() -> object:
    """二期分页策略（Y0-09 冻结口径：40 次尝试 / 30 秒 / 并发 2 / 缓存 60 秒）。

    缓存与冷却统一到 60s/5s，与旧读取路径的 300s 分离但显式可查——不再有两个
    互不知情的 TTL 同时生效。
    """
    from investment_steward_core import youzi_paging

    return youzi_paging.PagingPolicy(
        page_size=500,
        max_pages=40,
        max_rows=20_000,
        retries_per_page=2,
        request_timeout=10.0,
        cache_ttl=60.0,
        cooldown_ttl=5.0,
        cache_entries=32,
        max_inflight=32,
    )


def _kernel() -> object:
    """进程级单例内核：并发上限与缓存必须在**所有**调用方之间共享才有效。"""
    global _paging_kernel
    if _paging_kernel is None:
        with _paging_kernel_lock:
            if _paging_kernel is None:
                from investment_steward_core import youzi_paging, youzi_transport

                _paging_kernel = youzi_paging.PagingKernel(
                    youzi_transport.eastmoney_once, policy=paging_policy()
                )
    return _paging_kernel


def fetch_report_page_complete(
    report_name: str, day: str, *, budget: object | None = None
) -> tuple[list[dict[str, object]], str, str]:
    """按完整分页取某报告某交易日的全部行。

    返回 `(rows, coverage, reason)`：`coverage` 为 `complete` 时 `rows` 是该报告的
    完整行集；否则调用方**必须**降级（不得把部分行当完整输入生成跨日事实）。
    """
    from investment_steward_core import youzi_paging

    columns = _SORT_COLUMNS.get(report_name)
    if columns is None:
        raise LhbError(f"报告 {report_name} 未登记稳定排序键，拒绝分页取数")
    types = _SORT_TYPES[report_name]
    if len(columns.split(",")) != len(types.split(",")):
        # 自检：列数与方向数不等长时上游恒返 code=9501，宁可显式降级也不要静默 0 行。
        raise LhbError(f"报告 {report_name} 排序键与方向数量不一致，拒绝分页取数")
    active_budget = budget if budget is not None else youzi_paging.Budget()
    result = _kernel().fetch(
        {
            "reportName": report_name,
            "columns": "ALL",
            "source": "WEB",
            "client": "WEB",
        },
        sort_params={"sortColumns": columns, "sortTypes": types},
        filter_params={"filter": f"(TRADE_DATE='{day[:4]}-{day[4:6]}-{day[6:]}')"},
        budget=active_budget,
    )
    return list(result.rows), result.coverage, result.reason


def fetch_day_page_complete(
    day: str, *, budget: object | None = None
) -> dict[str, tuple[list[dict[str, object]], str, str]]:
    """某交易日三报告（榜单 + 买卖明细）的完整分页取数，**共用一个预算**。

    交易日窗口内多日多报告叠加时，预算必须由调用方逐日传递同一个实例，
    否则「40 次尝试」会按报告倍增。
    """
    return {
        report: fetch_report_page_complete(report, day, budget=budget)
        for report in (REPORT_DAILY_BILLBOARD, REPORT_SEAT_BUY, REPORT_SEAT_SELL)
    }


def fetch_seats(trading_day: str) -> list[SeatRow]:
    """某交易日的全部买卖席位明细（两侧合并）。"""
    buys = parse_seat_items(_fetch_rows(REPORT_SEAT_BUY, trading_day), DIRECTION_BUY)
    sells = parse_seat_items(_fetch_rows(REPORT_SEAT_SELL, trading_day), DIRECTION_SELL)
    if not buys and not sells:
        raise LhbError(f"东财 {trading_day} 无席位明细（非交易日或尚未披露）")
    return buys + sells


#: 席位档案的单页上限（数据源 pageSize；返回满页即可能被截断，端点须如实声明——U03）。
OPERATEDEPT_PAGE_SIZE = 50


def fetch_operatedept_history(
    operatedept_code: str, *, page_size: int = OPERATEDEPT_PAGE_SIZE
) -> list[dict[str, object]]:
    """席位档案：某营业部近 N 日的上榜记录。

    只支持**代码精确过滤**。实测 `like` 查询被数据源拒绝（`code=9501`），
    「全市场营业部搜索」在数据源层面不可行，不是本产品的取舍。
    返回裸 list（数据源无 total）——调用方须按 `len >= OPERATEDEPT_PAGE_SIZE` 自行声明可能截断。
    """
    code = (operatedept_code or "").strip()
    if not code:
        raise ValueError("operatedept_code 不可为空（like 查询不受支持，必须给精确代码）")
    payload = _http_get_json(
        {
            "reportName": REPORT_OPERATEDEPT_TRADE,
            "columns": "ALL",
            "pageNumber": 1,
            "pageSize": page_size,
            "filter": f'(OPERATEDEPT_CODE="{code}")',
            "sortColumns": "TRADE_DATE",
            "sortTypes": -1,
            "source": "WEB",
            "client": "WEB",
        }
    )
    reason = payload_error(payload)
    if reason:
        raise LhbError(f"{REPORT_OPERATEDEPT_TRADE}：{reason}")
    return _extract_result_rows(payload)


def reset_caches() -> None:
    """清空模块级缓存（测试用）。"""
    _cache.clear()
    _cache_at.clear()
    _fail_cache.clear()
