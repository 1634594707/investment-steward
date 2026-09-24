"""中国市场行情真实数据源（纯 stdlib，零新增依赖）。

复用 QuantHub（D:/Administrator/Desktop/finance）中已验证的东方财富 push2 接口约定，
但不引入 pandas / requests / akshare，保证 Core 依赖面不扩张。

网络不可达、响应非法或数据不满足 OHLC 边界校验时统一抛 FeedError，由调用方降级到
确定性演示数据，确保离线可用。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from investment_steward_core import feed_health

logger = logging.getLogger(__name__)

_PUSH2_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_USER_AGENT = "InvestmentSteward/0.1"
_REQUEST_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

_CACHE_TTL_SECONDS = 90.0
_cache: dict[tuple[str, int, str], tuple[float, list[dict[str, object]], str]] = {}
# 失败冷却：供应商对频繁请求会临时断连。近期失败直接短路抛 FeedError，由调用方快速降级，
# 避免每次渲染都重试打供应商，也避免雪上加霜的限流。
_FAIL_COOLDOWN_SECONDS = 90.0
_fail_cache: dict[tuple[str, int, str], float] = {}

# 腾讯免费行情（gu.qq.com 系）：本环境东财 push2his 被断连，腾讯可达且日 K 含当日实时蜡烛。
_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_PROVIDER_NAME = {
    "tencent": "中国市场行情（腾讯行情）",
    "eastmoney": "中国市场行情（东方财富）",
}
_SOURCE_ORDER = ("tencent", "eastmoney")

# K 线周期：腾讯 param 第二段直接用 day/week/month；东财 klt 码 101/102/103。
_VALID_PERIODS = ("day", "week", "month")
_EM_KLT = {"day": "101", "week": "102", "month": "103"}


class FeedError(RuntimeError):
    """行情源取数或校验失败，应降级而不应中断调用方。"""


def _symbol_to_secid(symbol: str) -> str:
    """A股代码转东财 secid（沪市 1. 前缀，深市 0. 前缀）。规则对齐 finance/eastmoney_source.py。"""
    if symbol.startswith(("5", "6")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def _symbol_to_tx(symbol: str) -> str:
    """A股代码转腾讯行情代码（sh/sz/bj 前缀）。5/6/1/9 前缀沪市，0/2/3 深市，4 北交所。"""
    head = symbol[:1]
    if head in ("0", "2", "3"):
        return f"sz{symbol}"
    if head == "4":
        return f"bj{symbol}"
    return f"sh{symbol}"


def _http_get(
    url: str,
    ua: str = _USER_AGENT,
    referer: str | None = None,
    source: str = feed_health.SOURCE_CN_MARKET_EASTMONEY,
) -> dict[str, object]:
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            headers = {"User-Agent": ua}
            if referer:
                headers["Referer"] = referer
            request = urllib.request.Request(url, headers=headers)
            # F02：逐次尝试埋点（含重试的第几次），失败与「响应不可用」都记，
            # 使成功率与延迟可复算。埋点未安装 recorder 时是空操作。
            with feed_health.attempt(source, url, attempt_index=attempt + 1):
                with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                    payload = response.read().decode("utf-8")
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise FeedError("行情源返回结构异常")
                return parsed
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise FeedError(f"行情请求失败: {last_error}")


def _parse_kline_rows(data: dict[str, object]) -> list[dict[str, object]]:
    """按 eastmoney fields2 顺序解析：date,open,close,high,low,volume,amount,...,turnover。"""
    node = data.get("data")
    klines = node.get("klines") if isinstance(node, dict) else None
    if not isinstance(klines, list):
        return []

    rows: list[dict[str, object]] = []
    for line in klines:
        if not isinstance(line, str):
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=UTC)
            open_price = float(parts[1])
            close_price = float(parts[2])
            high = float(parts[3])
            low = float(parts[4])
            volume = float(parts[5])
        except (ValueError, IndexError):
            continue
        if low > min(open_price, close_price) or high < max(open_price, close_price):
            continue
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )

    by_ts: dict[str, dict[str, object]] = {}
    for row in rows:
        by_ts[str(row["timestamp"])] = row
    ordered = sorted(by_ts.values(), key=lambda row: str(row["timestamp"]))
    return ordered


def _fetch_em_kline(symbol: str, limit: int, period: str = "day") -> list[dict[str, object]]:
    """拉取 A 股 / ETF K 线（东方财富前复权，klt 周期码），返回升序的 OHLCV 行列表。不触碰冷却缓存。"""
    secid = _symbol_to_secid(symbol)
    end = datetime.now(UTC)
    # 周线/月线一根覆盖更长时间，起止窗口放大避免周/月 K 数量不足。
    span_days = 540 if period == "day" else 5400
    beg = end - timedelta(days=span_days)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": _EM_KLT[period],
        "fqt": "1",
        "lmt": str(limit),
        "end": end.strftime("%Y%m%d"),
        "beg": beg.strftime("%Y%m%d"),
    }
    url = f"{_PUSH2_URL}?{urllib.parse.urlencode(params)}"
    data = _http_get(url, source=feed_health.SOURCE_CN_MARKET_EASTMONEY)
    rows = _parse_kline_rows(data)
    if not rows:
        raise FeedError(f"东方财富未返回 {symbol} 的行情")
    return rows[-limit:]


def _fetch_tx_kline(symbol: str, limit: int, period: str = "day") -> list[dict[str, object]]:
    """拉取 A 股 / ETF K 线（腾讯前复权，param 周期段 day/week/month），返回升序的 OHLCV 行列表。

    腾讯返回 [date, open, close, high, low, volume]；日 K 在收盘后即含当日实时蜡烛，
    响应默认包含当日，符合「当晚应见到今天 K 线」的时效预期。
    """
    tx_symbol = _symbol_to_tx(symbol)
    params = {"param": f"{tx_symbol},{period},,,{limit},qfq"}
    url = f"{_TX_KLINE_URL}?{urllib.parse.urlencode(params)}"
    data = _http_get(url, ua=_CHROME_UA, source=feed_health.SOURCE_CN_MARKET_TENCENT)

    node = data.get("data")
    item = node.get(tx_symbol) if isinstance(node, dict) else None
    rows_raw = None
    if isinstance(item, dict):
        # 复权序列键为 qfqday/qfqweek/qfqmonth；个别品种只给未复权键 day/week/month。
        rows_raw = item.get(f"qfq{period}") or item.get(period)
    if not isinstance(rows_raw, list) or not rows_raw:
        raise FeedError(f"腾讯未返回 {symbol} 的行情")

    rows: list[dict[str, object]] = []
    for line in rows_raw:
        if not isinstance(line, list) or len(line) < 5:
            continue
        try:
            ts = datetime.strptime(str(line[0]), "%Y-%m-%d").replace(tzinfo=UTC)
            open_price = float(line[1])
            close_price = float(line[2])
            high = float(line[3])
            low = float(line[4])
            volume = float(line[5]) if len(line) > 5 else 0.0
        except (ValueError, IndexError, TypeError):
            continue
        if low > min(open_price, close_price) or high < max(open_price, close_price):
            continue
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )

    by_ts: dict[str, dict[str, object]] = {}
    for row in rows:
        by_ts[str(row["timestamp"])] = row
    ordered = sorted(by_ts.values(), key=lambda row: str(row["timestamp"]))
    if not ordered:
        raise FeedError(f"腾讯未返回 {symbol} 的有效行情")
    return ordered[-limit:]


def fetch_cn_kline(symbol: str, limit: int = 120, period: str = "day") -> tuple[list[dict[str, object]], str]:
    """按序探测多供应商拉取 K 线（day/week/month），返回 (升序 OHLCV 行列表, 供应商标识)。

    腾讯优先（本环境可达且当日蜡烛即时），东财为回退；两个源都失败时进入冷却并抛
    FeedError，由调用方降级。模块级 TTL 缓存避免每次渲染重复请求。
    """
    if period not in _VALID_PERIODS:
        raise FeedError(f"不支持的 K 线周期: {period}（可选 {_VALID_PERIODS}）")
    cache_key = (symbol, limit, period)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1], cached[2]
    fail_at = _fail_cache.get(cache_key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise FeedError(f"{symbol} 行情近期拉取失败，处于冷却期")

    last_error: FeedError | None = None
    for source in _SOURCE_ORDER:
        try:
            rows = _fetch_tx_kline(symbol, limit, period) if source == "tencent" else _fetch_em_kline(symbol, limit, period)
            _cache[cache_key] = (time.monotonic(), rows, source)
            return rows, source
        except FeedError as error:
            last_error = error

    _fail_cache[cache_key] = time.monotonic()
    raise last_error or FeedError(f"行情数据源均不可用: {symbol}")


# —— 全市场榜单快照（新浪行情中心）：市场批量扫描的粗筛层 ——

# 东财 push2 clist 有 TLS 指纹反爬（curl 可过、urllib 一律断连，2026-09-07 实测），
# 故榜单层用新浪 Market_Center 接口（urllib 直连可达，GBK 编码，标准 JSON 数组）。
_SINA_CLIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page=1&num={top}&sort={sort}&asc={asc}&node=hs_a&symbol=&_s_r_a=auto"
)
_SINA_REFERER = "https://finance.sina.com.cn/"
# board 名 → (新浪排序字段, 是否升序)。
_MARKET_BOARDS: dict[str, tuple[str, int]] = {
    "turnover": ("amount", 0),
    "gainers": ("changepercent", 0),
    "losers": ("changepercent", 1),
    "turnover_rate": ("turnoverratio", 0),
}

_MARKET_CACHE_TTL_SECONDS = 60.0
_market_cache: dict[tuple[str, int], tuple[float, list[dict[str, object]]]] = {}

# —— 全市场分页遍历（mode=all）：新浪 hs_a 节点逐页拉取，覆盖全部沪深 A 股 ——
_SINA_CLIST_PAGE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num={num}&sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=auto"
)
# 全市场遍历较贵（数十页请求），成功结果缓存 5 分钟，避免连点重放。
_FULL_MARKET_CACHE_TTL_SECONDS = 300.0
_full_market_cache: tuple[float, list[dict[str, object]], bool] | None = None


def _parse_sina_rows(items: object) -> list[dict[str, object]]:
    """新浪榜单/遍历行 → 统一字段；停牌（price<=0）与北交所段（K 线源覆盖不全）如实跳过。"""
    rows: list[dict[str, object]] = []
    if not isinstance(items, list):
        return rows
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if not symbol or not name:
            continue
        if symbol.startswith(("4", "8", "9")):
            continue

        def _num(value: object) -> float | None:
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        price = _num(item.get("trade"))
        if price is None or price <= 0:  # 停牌等无报价行
            continue
        rows.append({
            "symbol": symbol,
            "name": name,
            "price": price,
            "change_pct": _num(item.get("changepercent")),
            "volume": _num(item.get("volume")),
            "turnover": _num(item.get("amount")),
            "turnover_rate": _num(item.get("turnoverratio")),
        })
    return rows


def fetch_cn_market_full_a(max_pages: int = 40, page_size: int = 80, page_sleep: float = 0.4) -> tuple[list[dict[str, object]], bool]:
    """分页遍历沪深 A 股（新浪 hs_a 节点，按成交额降序），供全市场趋势预筛。

    返回（行列表, 是否完整遍历）。按成交额降序意味着「页数预算用尽」时先覆盖
    最活跃的股票——部分遍历仍是有用信号，complete=False 时如实告知调用方。
    行字段与 fetch_cn_market_board 一致。结果缓存 5 分钟。
    """
    global _full_market_cache
    now = time.monotonic()
    if _full_market_cache and now - _full_market_cache[0] < _FULL_MARKET_CACHE_TTL_SECONDS:
        return _full_market_cache[1], _full_market_cache[2]

    rows: list[dict[str, object]] = []
    complete = False
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(page_sleep)  # 公开接口限速礼貌间隔
        url = _SINA_CLIST_PAGE_URL.format(page=page, num=int(page_size))
        try:
            # F02：逐页埋点。每页是不同的 URL（页号在 params 指纹里），故 attempt_index 恒为 1
            # ——该字段的语义是「同一请求的第几次重试」，不拿页号冒充。
            with feed_health.attempt(
                feed_health.SOURCE_CN_MARKET_SINA,
                url,
                {"page": page, "num": int(page_size)},
            ):
                raw = urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": _CHROME_UA, "Referer": _SINA_REFERER}),
                    timeout=_REQUEST_TIMEOUT,
                ).read().decode("gbk", errors="replace")
        except (OSError, ValueError) as error:
            if page == 1:
                raise FeedError(f"全市场遍历首页拉取失败：{error}") from error
            complete = False  # 中途页失败：保留已取得行，如实标注未完整
            break
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as error:
            if page == 1:
                raise FeedError(f"全市场遍历返回非 JSON：{error}") from error
            complete = False
            break
        if not isinstance(items, list) or not items:
            complete = True  # 空页 = 遍历自然结束
            break
        rows.extend(_parse_sina_rows(items))
        if len(items) < page_size:
            complete = True
            break
    else:
        complete = False  # 页数预算用尽
    _full_market_cache = (now, rows, complete)
    return rows, complete


def score_market_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """快照趋势预筛分（0-100，纯快照口径，非 K 线引擎；供粗筛排序，不构成结论）。

    组成（可在结果里逐股解释）：涨幅 40（当日 0~10% 线性）+ 换手 30（3%~15% 最优区间，
    两侧线性衰减）+ 成交额分位 30（在全市场样本中的百分位）。负涨幅行涨幅项记 0。
    """
    amounts = sorted(float(row["turnover"]) for row in rows if row.get("turnover") is not None)
    if not amounts:
        return [dict(row, trend_score=0.0, trend_parts={"cp": 0.0, "tr": 0.0, "amt": 0.0}) for row in rows]

    def _percentile(value: float | None) -> float:
        if value is None:
            return 0.0
        below = sum(1 for item in amounts if item <= value)
        return below / len(amounts)

    scored: list[dict[str, object]] = []
    for row in rows:
        cp = row.get("change_pct")
        cp_value = float(cp) if isinstance(cp, (int, float)) else 0.0
        cp_points = max(0.0, min(cp_value, 10.0)) / 10.0 * 40.0

        tr = row.get("turnover_rate")
        tr_value = float(tr) if isinstance(tr, (int, float)) else 0.0
        if tr_value <= 0:
            tr_points = 0.0
        elif tr_value < 3.0:
            tr_points = tr_value / 3.0 * 30.0
        elif tr_value <= 15.0:
            tr_points = 30.0
        elif tr_value < 30.0:
            tr_points = 30.0 * (1.0 - (tr_value - 15.0) / 15.0)
        else:
            tr_points = 0.0

        amt_points = _percentile(row.get("turnover") if isinstance(row.get("turnover"), (int, float)) else None) * 30.0
        scored.append(dict(
            row,
            trend_score=round(cp_points + tr_points + amt_points, 1),
            trend_parts={"cp": round(cp_points, 1), "tr": round(tr_points, 1), "amt": round(amt_points, 1)},
        ))
    scored.sort(key=lambda row: float(row["trend_score"]), reverse=True)
    return scored


def fetch_cn_market_board(board: str, top: int = 30) -> list[dict[str, object]]:
    """拉取全市场榜单快照（单页请求，沪深 A 股），供市场批量扫描粗筛。

    返回字段：symbol/name/price/change_pct/volume/turnover/turnover_rate。
    停牌（price<=0）或字段缺失的行如实跳过。60 秒模块级缓存避免连点重放。
    """
    sort_rule = _MARKET_BOARDS.get(board)
    if sort_rule is None:
        raise FeedError(f"不支持的市场榜单: {board}（可选 {sorted(_MARKET_BOARDS)}）")
    sort_field, ascending = sort_rule
    cache_key = (board, top)
    now = time.monotonic()
    cached = _market_cache.get(cache_key)
    if cached and now - cached[0] < _MARKET_CACHE_TTL_SECONDS:
        return cached[1]

    url = _SINA_CLIST_URL.format(top=int(top), sort=sort_field, asc=ascending)
    with feed_health.attempt(
        feed_health.SOURCE_CN_MARKET_SINA, url, {"board": board, "top": int(top)}
    ):
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": _CHROME_UA, "Referer": _SINA_REFERER}),
            timeout=_REQUEST_TIMEOUT,
        ).read().decode("gbk", errors="replace")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FeedError(f"市场榜单返回非 JSON: {board}") from error
    if not isinstance(items, list):
        raise FeedError(f"市场榜单返回结构异常: {board}")
    rows: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if not symbol or not name:
            continue
        if symbol.startswith(("4", "8", "9")):  # 北交所段：K 线源覆盖不全，按「沪深 A 股」口径排除
            continue

        def _num(value: object) -> float | None:
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        price = _num(item.get("trade"))
        if price is None or price <= 0:  # 停牌等无报价行
            continue
        rows.append({
            "symbol": symbol,
            "name": name,
            "price": price,
            "change_pct": _num(item.get("changepercent")),
            "volume": _num(item.get("volume")),
            "turnover": _num(item.get("amount")),
            "turnover_rate": _num(item.get("turnoverratio")),
        })
    _market_cache[cache_key] = (time.monotonic(), rows)
    return rows

# —— 行业板块（新浪行情中心）：按板块划分扫描的板块维度（2026-09-11 用户需求） ——
#
# 用户要「按板块做划分，从板块股里挑」。此前 scan-market 的「板块」只是市场分层
# （主板/创业板/科创板，按代码前缀），不是行业语义。新浪行业板块清单接口
# newSinaHy.php 返回 `var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,..."}`
# （GBK，值 = 逗号分隔字符串，约 100 个板块）；成分股复用 Market_Center.getHQNodeData
# 的 node 参数（把 hs_a 换成板块代码），与榜单层同一实现口径。
_SINA_SECTOR_LIST_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
_SINA_SECTOR_NODE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page=1&num={top}&sort=amount&asc=0&node={node}&symbol=&_s_r_a=auto"
)
# 板块清单变化很慢（行业分类调整以季度计），缓存 5 分钟；成分股按 60 秒与榜单层同节奏。
_SECTOR_LIST_CACHE_TTL_SECONDS = 300.0
_SECTOR_MEMBERS_CACHE_TTL_SECONDS = 60.0
_sector_list_cache: tuple[float, list[dict[str, object]]] | None = None
_sector_members_cache: dict[tuple[str, int], tuple[float, list[dict[str, object]]]] = {}


def _parse_sina_sector_list(raw: str) -> list[dict[str, object]]:
    """`var S_Finance_bankuai_sinaindustry = {...}` → 板块行列表（字段缺失如实跳过）。"""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise FeedError("行业板块清单返回结构异常（未找到 JSON 主体）")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as error:
        raise FeedError("行业板块清单返回非 JSON") from error
    if not isinstance(payload, dict):
        raise FeedError("行业板块清单返回结构异常（非对象）")
    rows: list[dict[str, object]] = []
    for value in payload.values():
        parts = str(value).split(",")
        if len(parts) < 13:
            continue

        def _num(index: int) -> float | None:
            try:
                return float(parts[index])
            except (TypeError, ValueError):
                return None

        code = parts[0].strip()
        name = parts[1].strip()
        if not code or not name:
            continue
        rows.append({
            "code": code,
            "name": name,
            "member_count": int(_num(2) or 0),
            "avg_price": _num(3),
            "change_pct": _num(5),
            "amount": _num(7),
            "leader_symbol": parts[8].strip(),
            "leader_name": parts[12].strip(),
            "leader_change_pct": _num(9),
        })
    rows.sort(key=lambda row: float(row.get("change_pct") or 0.0), reverse=True)
    return rows


def fetch_cn_sector_list() -> list[dict[str, object]]:
    """新浪行业板块清单（约 100 个，按当日涨跌幅降序）。

    返回字段：code / name / member_count / avg_price / change_pct / amount /
    leader_symbol / leader_name / leader_change_pct。缓存 5 分钟。
    """
    global _sector_list_cache
    now = time.monotonic()
    if _sector_list_cache and now - _sector_list_cache[0] < _SECTOR_LIST_CACHE_TTL_SECONDS:
        return _sector_list_cache[1]
    with feed_health.attempt(feed_health.SOURCE_CN_MARKET_SINA, _SINA_SECTOR_LIST_URL):
        raw = urllib.request.urlopen(
            urllib.request.Request(
                _SINA_SECTOR_LIST_URL, headers={"User-Agent": _CHROME_UA, "Referer": _SINA_REFERER}
            ),
            timeout=_REQUEST_TIMEOUT,
        ).read().decode("gbk", errors="replace")
    rows = _parse_sina_sector_list(raw)
    if not rows:
        raise FeedError("行业板块清单为空")
    _sector_list_cache = (now, rows)
    return rows


def fetch_cn_sector_members(sector_code: str, top: int = 60) -> list[dict[str, object]]:
    """某行业板块的成分股快照（按成交额降序，字段与 fetch_cn_market_board 一致）。缓存 60 秒。"""
    node = (sector_code or "").strip()
    if not node:
        raise FeedError("板块代码为空")
    cache_key = (node, int(top))
    now = time.monotonic()
    cached = _sector_members_cache.get(cache_key)
    if cached and now - cached[0] < _SECTOR_MEMBERS_CACHE_TTL_SECONDS:
        return cached[1]
    url = _SINA_SECTOR_NODE_URL.format(top=int(top), node=urllib.parse.quote(node))
    with feed_health.attempt(
        feed_health.SOURCE_CN_MARKET_SINA, url, {"node": node, "top": int(top)}
    ):
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": _CHROME_UA, "Referer": _SINA_REFERER}),
            timeout=_REQUEST_TIMEOUT,
        ).read().decode("gbk", errors="replace")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as error:
        raise FeedError(f"行业板块成分股返回非 JSON: {node}") from error
    rows = _parse_sina_rows(items)
    if not rows:
        raise FeedError(f"行业板块成分股为空: {node}")
    _sector_members_cache[cache_key] = (now, rows)
    return rows
