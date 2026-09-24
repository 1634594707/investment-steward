"""中国市场公告证据源（纯 stdlib，零新增依赖）。

复用 QuantHub（D:/Administrator/Desktop/finance，core/data_feed/eastmoney_source.py
的 get_announcements）中已验证的东方财富公告接口约定，但不引入 pandas / requests /
akshare，保证 Core 依赖面不扩张。

再分发边界（见 ADR-0004）：本模块只把公告作为「用户本地证据」取用，license_status 统一
记为 `local-personal-use`，不对外再分发、不宣称官方授权。取数失败时不生成任何证据
（ADR-0004 不变量 #4：数据缺失时明确「证据不足」，不编造），由调用方显式标注降级。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_USER_AGENT = "InvestmentSteward/0.1"
_REQUEST_TIMEOUT = 15
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

_CACHE_TTL_SECONDS = 90.0
_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
_FAIL_COOLDOWN_SECONDS = 90.0
_fail_cache: dict[str, float] = {}

_ARTICLE_URL_TEMPLATE = "https://finance.eastmoney.com/a/{art_code}.html"


class NoticeError(RuntimeError):
    """公告源取数或结构异常，应显式降级而不应中断调用方、更不应伪造证据。"""


@dataclass(frozen=True)
class Announcement:
    symbol: str
    title: str
    notice_date_raw: str
    art_code: str
    ann_type: str
    url: str


def _ann_http_get(params: dict[str, object]) -> dict[str, object]:
    last_error: Exception | None = None
    query = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{_ANN_URL}?{query}"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                payload = response.read().decode("utf-8")
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise NoticeError("东方财富公告接口返回结构异常")
            return parsed
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise NoticeError(f"东方财富公告请求失败: {last_error}")


def _parse_items(data: dict[str, object]) -> list[dict[str, object]]:
    node = data.get("data")
    items = node.get("list") if isinstance(node, dict) else None
    if not isinstance(items, list):
        return []
    # 只保留必需且既可定位又可哈希的字段；其余由调用方携带在 raw_locator 中。
    parsed: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        art_code = item.get("art_code")
        if not isinstance(title, str) or not title or not isinstance(art_code, str) or not art_code:
            continue
        parsed.append(
            {
                "title": title,
                "notice_date": str(item.get("notice_date") or ""),
                "art_code": art_code,
                "ann_type": str(item.get("columns_name") or ""),
            }
        )
    return parsed


def parse_notice_timestamp(raw: str | None) -> datetime | None:
    """尽量把 notice_date 解析为 aware 时间；解析不了就置 None（不猜测）。"""
    if not raw:
        return None
    if raw.isdigit():
        ms = int(raw)
        try:
            return datetime.fromtimestamp(ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def fetch_cn_announcements(symbol: str, limit: int = 20) -> list[Announcement]:
    """拉取 A 股公告（东方财富公告接口），返回可定位的公告记录。

    模块级 TTL 缓存 + 失败冷却，与 market_feed 一致；失败抛 NoticeError 由调用方降级，
    不返回空壳也不生成任何证据。
    """
    cache_key = f"{symbol}:{limit}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        # v22 修复：缓存存的是 __dict__ 列表（TTL 内二次取数必须重建 dataclass，
        # 否则返回 dict 导致调用方炸 'dict' object has no attribute 'notice_date_raw'）。
        return [item if isinstance(item, Announcement) else Announcement(**item) for item in cached[1]]
    fail_at = _fail_cache.get(cache_key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise NoticeError(f"{symbol} 公告近期拉取失败，处于冷却期")

    params = {
        "sr": "-1",
        "page_size": str(limit),
        "page_index": "1",
        "ann_type": "A",
        "stock_list": symbol,
        "f_node": "0",
        "s_node": "0",
    }

    def _article_url(art_code: str) -> str:
        return _ARTICLE_URL_TEMPLATE.format(art_code=art_code)

    try:
        data = _ann_http_get(params)
        items = _parse_items(data)
        if not items:
            raise NoticeError(f"东方财富未返回 {symbol} 的公告")
    except NoticeError:
        _fail_cache[cache_key] = time.monotonic()
        raise

    announcements = [
        Announcement(
            symbol=symbol,
            title=str(item["title"]),
            notice_date_raw=str(item["notice_date"]),
            art_code=str(item["art_code"]),
            ann_type=str(item["ann_type"]),
            url=_article_url(str(item["art_code"])),
        )
        for item in items[:limit]
    ]
    _cache[cache_key] = (time.monotonic(), [a.__dict__ for a in announcements])
    return announcements


def content_hash_for(symbol: str, art_code: str, title: str, notice_date: str) -> str:
    """公告证据内容哈希（v23 查重归一）：以 (symbol, 标题, 公告日期) 确定身份。

    art_code 参数保留兼容旧调用但不参与哈希——实测同一公告被源站换号重发时
    （同标题同日期、不同 art_code）旧哈希会各入一条，造成重复证据。
    """
    payload = f"ann\x1f{symbol}\x1f{title.strip()}\x1f{notice_date}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def raw_locator_for(api: str, symbol: str, item: Announcement) -> str:
    """保留原始来源定位（ADR-0004 要求），便于后续回链与审计。"""
    return json.dumps(
        {
            "api": api,
            "symbol": symbol,
            "art_code": item.art_code,
            "title": item.title,
            "ann_type": item.ann_type,
            "notice_date": item.notice_date_raw,
        },
        ensure_ascii=False,
    )


# —— 新闻检索证据（东财搜索 JSONP；对 finance get_news 约定的纯 stdlib 移植） ——


class NewsError(RuntimeError):
    """新闻检索源取数或结构异常，应显式降级而非伪造证据。"""


@dataclass(frozen=True)
class News:
    symbol: str
    title: str
    content: str
    published_raw: str
    media: str
    code: str
    url: str | None


def _clean_text(value: object) -> str:
    """去掉东财搜索命中高亮标签与全角空格（对齐 finance get_news 的 _clean）。"""
    return (
        str(value or "")
        .replace("<em>", "")
        .replace("</em>", "")
        .replace("\u3000", "")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def _strip_jsonp_callback(text: str, callback: str) -> str:
    prefix = f"{callback}("
    if text.startswith(prefix) and text.endswith(")"):
        return text[len(prefix) : -1]
    return text


def _fetch_news_payload(symbol: str, limit: int) -> dict[str, object]:
    callback = "jQuery351004247" + str(int(time.time()))
    inner_param = {
        "uid": "",
        "keyword": symbol,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": min(max(1, int(limit)), 100),
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    query = "&".join(
        (
            f"cb={urllib.parse.quote(str(callback))}",
            f"param={urllib.parse.quote(json.dumps(inner_param, ensure_ascii=False))}",
            f"_={time.time_ns()}",
        )
    )
    url = f"{_NEWS_URL}?{query}"

    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": f"https://so.eastmoney.com/news/s?keyword={urllib.parse.quote(symbol)}",
                },
            )
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                text = response.read().decode("utf-8")
            payload_text = _strip_jsonp_callback(text, callback)
            payload = json.loads(payload_text)
            if not isinstance(payload, dict):
                raise NewsError("东方财富新闻搜索返回结构异常")
            return payload
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise NewsError(f"东方财富新闻检索失败: {last_error}")


def fetch_cn_news(symbol: str, limit: int = 20) -> list[News]:
    """拉取 A 股相关新闻（东财搜索 JSONP）。模块级 TTL 缓存 + 失败冷却，与公告一致。"""
    cache_key = f"news:{symbol}:{limit}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        # v22 修复：同公告缓存，命中时重建 dataclass（否则返回 dict 炸 'published_raw'）。
        return [item if isinstance(item, News) else News(**item) for item in cached[1]]
    fail_at = _fail_cache.get(cache_key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise NewsError(f"{symbol} 新闻近期拉取失败，处于冷却期")

    try:
        payload = _fetch_news_payload(symbol, limit)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        items = result.get("cmsArticleWebOld") or []
        if not isinstance(items, list) or not items:
            raise NewsError(f"东方财富未返回 {symbol} 的新闻")
    except NewsError:
        _fail_cache[cache_key] = time.monotonic()
        raise

    news: list[News] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"))
        code = str(item.get("code") or "")
        if not title or not code:
            continue
        news.append(
            News(
                symbol=symbol,
                title=title,
                content=_clean_text(item.get("content")),
                published_raw=str(item.get("date") or ""),
                media=_clean_text(item.get("mediaName")) or "东方财富",
                code=code,
                url=f"https://finance.eastmoney.com/a/{code}.html",
            )
        )
    _cache[cache_key] = (time.monotonic(), [n.__dict__ for n in news])
    return news


def content_hash_for_news(symbol: str, code: str, title: str, content: str) -> str:
    """新闻证据内容哈希（v23 查重归一）：以 (symbol, 标题) 确定身份。

    code（稿件号）与正文摘要不参与哈希——同一新闻被不同稿件号/不同截断长度的
    接口重复返回时（同标题）旧哈希会各入一条，造成重复证据。
    """
    payload = f"news\x1f{symbol}\x1f{title.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()