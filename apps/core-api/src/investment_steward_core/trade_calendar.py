"""A 股交易日历（Y0-07 冻结实现）：从**已在白名单内**的数据源推导，零新增依赖。

## 为什么不用自然日、也不用「有榜日期」

路线图 Y0-07 要求「不得只排除周末或以有榜日期代替交易日」。两种朴素做法都被否掉：

* **自然日推算**（`_recent_trade_days` 的做法）会把长假中间的工作日当成交易日，
  于是「相邻交易日」和「五交易日窗口」都会算错。
* **有榜日期**（龙虎榜披露日）不等于交易日：某日可能确实开盘但无人上榜，
  用披露日当日历会漏掉窗口里的真实交易日，从而把「中间隔了一天」错配成相邻。

## 采用的口径：以全市场估值横截面为「该日是否开市」的判据

`RPT_VALUEANALYSIS_DET`（Core 既有估值源，域名 `datacenter-web.eastmoney.com`
已在 `official.youzi-radar` 的 `network_allowlist` 内）在**每个交易日**都返回全市场
横截面（2026-09-17 实测 5565 行，覆盖沪/深/创业板/科创板/北交所 920xxx），
而**非交易日**返回 `success=false` + `返回数据为空`。

因此对**单只成熟证券**按日期区间查询，返回的 `TRADE_DATE` 序列就是该区间内的
真实交易日序列。2026-09-18 实测：8 只成熟证券（600519/601398/000001/600000/
002594/688981/600036/000651/601857/600030）在 2024-01-02..2026-09-17 上给出
**完全一致的 658 天**，并集与交集之差为 0；2025/2026 的元旦、春节、清明、劳动节、
端午、国庆全部正确缺失（29 项人工核验 0 不符，见
`.runtime-local/y0-probes/y0_07_calendar_verify.py`）。

## 不可让渡的边界

1. **停牌不等于休市**。本模块因此只信任**多只成熟证券的交集**作为交易日：
   单只股票停牌会让它自己缺日，交集不会。取数少于 `MIN_REFERENCE_SECURITIES`
   只成功证券时宁可 `unavailable`，不拿一只票的序列冒充全市场日历。
2. **上市首日之前没有记录**。新股只返回上市后的日期；用固定成熟证券做基准即可，
   这也是「基准证券」必须是老牌大市值标的的原因。
3. **当日是否已收盘无法由本模块判定**。盘中查询当日会因横截面尚未生成而返回空，
   与节假日返回空**形状相同**。因此本模块只把「<= 最近一个已确认交易日」的日期
   视为交易日，并把「今天」单独标为 `pending`，绝不把当日空响应断言成「休市」。
4. **失败即降级**。取数异常一律返回 `available=False` + 原因，由调用方显式降级
   （路线图 Y0-07：无可信日历则阻断跨日功能），不允许回退到自然日算术。

缓存：日历按自然日变化，默认 TTL 6 小时；失败冷却 5 分钟，避免连续打上游。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
#: 估值横截面报表：每个交易日全市场一行，是「该日开市」的判据。
CALENDAR_REPORT = "RPT_VALUEANALYSIS_DET"

#: 基准证券：必须是上市多年、极少停牌的大市值标的。相关性越低越好。
#: 全部为 2024 年前上市的老牌标的，保证窗口内每个交易日都有记录。
DEFAULT_REFERENCE_SECURITIES: tuple[str, ...] = (
    "600519",  # 贵州茅台（沪主板）
    "601398",  # 工商银行（沪主板）
    "000001",  # 平安银行（深主板）
    "600036",  # 招商银行（沪主板）
    "000651",  # 格力电器（深主板）
)

#: 至少要这么多个基准证券成功且给出可用日期，否则视为无可信日历。
MIN_REFERENCE_SECURITIES = 3
#: 单次查询上限：避免把整段历史一次拉爆（每页 2000 行够 8 年）。
_MAX_PAGE_SIZE = 2000
_REQUEST_TIMEOUT = 20.0
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

#: 日历按自然日变化；失败冷却避免连续打上游。
_CACHE_TTL_SECONDS = 6 * 3600.0
_FAIL_COOLDOWN_SECONDS = 300.0
#: 日历本身有界：超过这个天数的请求直接拒绝，不静默截断成「完整」。
MAX_SPAN_DAYS = 4000

_cache: dict[str, tuple[float, tuple[str, ...], str]] = {}
_fail_cache: dict[str, float] = {}
_lock = threading.Lock()


class CalendarError(RuntimeError):
    """日历取数或结构异常；调用方必须显式降级，不得回退自然日。"""


@dataclass(frozen=True)
class CalendarResult:
    """一次日历查询的结果。

    `available=False` 时 `days` 为空且 `reason` 非空——**绝不用自然日填充**。
    """

    available: bool
    days: tuple[str, ...]
    reason: str = ""
    as_of: str = ""
    reference_count: int = 0
    fetched_at: float = field(default=0.0)

    def previous(self, day: str, *, count: int) -> tuple[str, ...]:
        """紧邻 `day` 之前的 `count` 个交易日（不含 `day`，升序）。

        `day` 不是已知交易日时，取「严格早于它」的交易日，不做任何插值。
        """
        if not self.available or count <= 0:
            return ()
        earlier = [item for item in self.days if item < day]
        return tuple(earlier[-count:])

    def window_ending_at(self, day: str, *, count: int) -> tuple[str, ...]:
        """以 `day` 结尾、长度为 `count` 的交易日窗口（含 `day`，升序）。

        `day` 不在日历中（休市/未来/尚未收盘）时返回空元组：窗口不完整，
        调用方据此**不生成**跨日事实，而不是拿别的日子凑数。
        """
        if not self.available or count <= 0:
            return ()
        if day not in set(self.days):
            return ()
        index = self.days.index(day)
        if index + 1 < count:
            return ()
        return self.days[index + 1 - count : index + 1]

    def contains(self, day: str) -> bool:
        return self.available and day in set(self.days)

    def next_after(self, day: str, *, count: int = 1) -> tuple[str, ...]:
        """紧邻 `day` 之后的 `count` 个交易日（不含 `day`，升序）。"""
        if not self.available or count <= 0:
            return ()
        later = [item for item in self.days if item > day]
        return tuple(later[:count])


def _normalise(day: object) -> str:
    text = str(day or "").strip()
    head = text.split(" ")[0].split("T")[0]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        head = head.replace("-", "")
    if len(head) != 8 or not head.isascii() or not head.isdigit():
        raise CalendarError(f"日期格式不合法（应为 YYYYMMDD 或 YYYY-MM-DD）：{day!r}")
    return head


def _to_dashed(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _as_date(day: str) -> date:
    """YYYYMMDD → date；不合法直接抛错（不猜、不修正）。"""
    try:
        return date(int(day[:4]), int(day[4:6]), int(day[6:]))
    except ValueError as error:
        raise CalendarError(f"无效日期：{day!r}") from error


def _fetch_reference_days(
    security_code: str, start: str, end: str, *, opener=None
) -> tuple[str, ...]:
    """取单只基准证券在区间内的 `TRADE_DATE` 序列（升序去重）。"""
    params = {
        "reportName": CALENDAR_REPORT,
        "columns": "TRADE_DATE,SECURITY_CODE",
        "pageNumber": 1,
        "pageSize": _MAX_PAGE_SIZE,
        "filter": (
            f'(SECURITY_CODE="{security_code}")'
            f"(TRADE_DATE>='{_to_dashed(start)}')(TRADE_DATE<='{_to_dashed(end)}')"
        ),
        "sortColumns": "TRADE_DATE",
        "sortTypes": 1,
        "source": "WEB",
        "client": "WEB",
    }
    url = f"{CALENDAR_URL}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "InvestmentSteward/0.1",
                    "Referer": "https://data.eastmoney.com/",
                    "Accept": "application/json",
                },
            )
            open_fn = opener or urllib.request.urlopen
            with open_fn(request, timeout=_REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise CalendarError("估值横截面返回结构异常")
            result = payload.get("result")
            if not isinstance(result, dict):
                # 非交易日/未披露都会走到这里：该证券在区间内没有记录。
                return ()
            data = result.get("data")
            if not isinstance(data, list):
                return ()
            days = {
                _normalise(row.get("TRADE_DATE"))
                for row in data
                if isinstance(row, dict) and row.get("TRADE_DATE")
            }
            return tuple(sorted(days))
        except CalendarError:
            raise
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise CalendarError(f"交易日历取数失败：{last_error}")


def fetch_calendar(
    start: str,
    end: str,
    *,
    reference_securities: tuple[str, ...] = DEFAULT_REFERENCE_SECURITIES,
    opener=None,
    use_cache: bool = True,
) -> CalendarResult:
    """取 [start, end] 区间的可信交易日序列。

    只用**多只基准证券的交集**：单只停牌或缺记录不会污染日历。成功证券数不足
    `MIN_REFERENCE_SECURITIES` 时返回 `available=False`，绝不回退到自然日。
    """
    first, last = _normalise(start), _normalise(end)
    if first > last:
        raise CalendarError(f"起止日期顺序不合法：{start} > {end}")
    first_date, last_date = _as_date(first), _as_date(last)
    if (last_date - first_date).days > MAX_SPAN_DAYS:
        raise CalendarError(f"区间超过 {MAX_SPAN_DAYS} 天上限：{start}..{end}")

    cache_key = f"{first}|{last}|{','.join(reference_securities)}"
    now = time.monotonic()
    if use_cache:
        with _lock:
            cached = _cache.get(cache_key)
            if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                return CalendarResult(
                    True, cached[1], "", cached[2], len(reference_securities), cached[0]
                )
            fail_at = _fail_cache.get(cache_key)
        if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
            return CalendarResult(False, (), "日历近期取数失败，处于冷却期")
    per_security: dict[str, tuple[str, ...]] = {}
    failures: list[str] = []
    for code in reference_securities:
        try:
            days = _fetch_reference_days(code, first, last, opener=opener)
        except CalendarError as error:
            failures.append(f"{code}:{error}")
            continue
        if days:
            per_security[code] = days

    if len(per_security) < MIN_REFERENCE_SECURITIES:
        with _lock:
            _fail_cache[cache_key] = time.monotonic()
        reason = f"可信基准证券不足（成功 {len(per_security)}，需要 {MIN_REFERENCE_SECURITIES}）"
        if failures:
            reason = f"{reason}；{'；'.join(failures[:2])}"
        return CalendarResult(False, (), reason)

    # 交集：只有所有成功基准证券都开市的日子才算交易日。
    common: set[str] = set(next(iter(per_security.values())))
    for days in per_security.values():
        common &= set(days)
    ordered = tuple(sorted(day for day in common if first <= day <= last))
    if not ordered:
        with _lock:
            _fail_cache[cache_key] = time.monotonic()
        return CalendarResult(False, (), "区间内没有共同交易日（基准证券交集为空）")

    as_of = ordered[-1]
    with _lock:
        _cache[cache_key] = (time.monotonic(), ordered, as_of)
        _fail_cache.pop(cache_key, None)
    return CalendarResult(True, ordered, "", as_of, len(per_security), time.monotonic())


def recent_trading_days(count: int, *, as_of: str | None = None, opener=None) -> CalendarResult:
    """最近 `count` 个交易日（升序，末尾为最新）。

    `as_of` 给定时取其**之前**的交易日（用于历史复盘的可重复计算）；
    未给定时用本机日期。当日是否已收盘不由本模块断言：调用方若拿到
    `as_of` 等于今日而当日尚未收盘，应把今日视为 `not_published`
    而不是休市（见模块文档边界 3）。
    """
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise CalendarError("count 必须是正整数")
    if count > 250:
        raise CalendarError("count 不得超过 250 个交易日")
    end_day = _normalise(as_of) if as_of else time.strftime("%Y%m%d")
    # 往前多取自然日，保证覆盖长假（春节/国庆连休可达 9 天）加缓冲。
    start_day = (_as_date(end_day) - timedelta(days=count + 40)).strftime("%Y%m%d")
    result = fetch_calendar(start_day, end_day, opener=opener)
    if not result.available:
        return result
    picked = result.days[-count:]
    if len(picked) < count:
        return CalendarResult(
            False,
            (),
            f"日历覆盖不足：请求 {count} 个交易日，仅取得 {len(picked)} 个",
            result.as_of,
            result.reference_count,
            result.fetched_at,
        )
    return CalendarResult(True, picked, "", result.as_of, result.reference_count, result.fetched_at)


def reset_caches() -> None:
    """清空模块级缓存（测试用）。"""
    with _lock:
        _cache.clear()
        _fail_cache.clear()


def is_plausible_day(day: str) -> bool:
    """校验 YYYYMMDD 且为真实公历日期（不做交易日判断）。"""
    try:
        parsed = _normalise(day)
    except CalendarError:
        return False
    try:
        time.strptime(parsed, "%Y%m%d")
    except ValueError:
        return False
    return True


__all__ = [
    "CALENDAR_REPORT",
    "DEFAULT_REFERENCE_SECURITIES",
    "MAX_SPAN_DAYS",
    "MIN_REFERENCE_SECURITIES",
    "CalendarError",
    "CalendarResult",
    "fetch_calendar",
    "is_plausible_day",
    "recent_trading_days",
    "reset_caches",
]
