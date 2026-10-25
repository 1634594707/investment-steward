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
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_ANN_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
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

# —— D1（研报产出与交付链路路线图 2026-09-27）：公告正文抓取 ——
#
# 背景：`/evidence/announcements` 与报告链路此前只拿到公告**标题**，于是
# `report_quality.SOURCE_PROFILES["S3"]` 只能判 B 级，整条链路拿不到任何 A 级证据；
# 而 A 股投资者最认的恰恰是交易所披露原文。本段补上「正文」这一层。
#
# 取数口径（实测 2026-09-28）：`np-cnotice-stock/api/content/ann?art_code=<code>` 返回
#   data.notice_content —— 披露原文全文（含「董事会保证…」声明、重要内容提示、落款），
#   `is_ai_summary=0`，**不是摘要**；`data.attach_url_web` 为原文 PDF 链接；
#   失败形态是 `{"success":0,"error":…}`（无 data 节点），据此如实判定失败。
#   page_index 不影响正文（1/2/3 返回同一份），无需翻页。
#
# 成本边界：每条正文 1 次请求，因此只对**最近 ANNOUNCEMENT_BODY_LIMIT 条**取正文，
# 不随公告条数线性放大。单条失败只跳过该条（正文是加分项，不应让整批公告降级）。

ANNOUNCEMENT_BODY_LIMIT = 3
# 进提示词的单条正文字数上限（超出显式标注截断，不静默截）。
ANNOUNCEMENT_BODY_CHARS = 2000
# 源站定位号形状：`AN202608141827994407`（实测列表接口返回）/ `20280820000123`。
# 形状不符（空串、占位号、测试桩）**不发起请求**——不发无意义请求，也不猜测。
_ART_CODE_PATTERN = re.compile(r"^(?:AN)?\d{10,}$")
_CONTENT_CACHE_TTL_SECONDS = 300.0
_content_cache: dict[str, tuple[float, "AnnouncementBody"]] = {}
_content_fail_cache: dict[str, float] = {}


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


@dataclass(frozen=True)
class AnnouncementBody:
    """一条公告的**披露原文**（D1）。

    `content` 是归一后的全文纯文本（不是摘要）；`attach_url` 是源站给出的原文 PDF 链接，
    取不到则为 None。本 dataclass 只在正文确实取到时才被构造——空正文一律抛
    `NoticeError`，不产出空壳。
    """

    art_code: str
    content: str
    attach_url: str | None


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


# —— D1：公告正文抓取（东财公告正文接口；纯 stdlib，不引新依赖） ——


def clean_notice_content(raw: object) -> str:
    """正文归一：统一换行、压平行内多余空白、最多保留一个空行。

    源站正文用大量空格做排版缩进（含表格列对齐）。直接送进提示词既费 token，也会让模型
    把「对齐用的空格」当成语义；归一后只保留「行」这一层结构——**不改字、不删句、不截断**。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    out: list[str] = []
    blank = 0
    for line in text.split("\n"):
        flat = re.sub(r"[ \t]{2,}", " ", line).strip()
        if flat:
            blank = 0
            out.append(flat)
        elif blank == 0 and out:
            blank = 1
            out.append("")
    return "\n".join(out).strip()


def _ann_content_http_get(art_code: str) -> dict[str, object]:
    """公告正文接口的原始请求（重试 + 退避），与 `_ann_http_get` 同构。"""
    query = urllib.parse.urlencode(
        {"art_code": art_code, "client_source": "web", "page_index": 1}
    )
    url = f"{_ANN_CONTENT_URL}?{query}"
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                payload = response.read().decode("utf-8")
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise NoticeError("东方财富公告正文接口返回结构异常")
            return parsed
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise NoticeError(f"东方财富公告正文请求失败: {last_error}")


def fetch_announcement_content(art_code: str) -> AnnouncementBody:
    """抓取单条公告的披露原文全文（东财公告正文接口）。

    - 正文来自数据商转载的交易所披露原文（含董事会保证声明与落款），**不是摘要**；
    - 取不到（形状不符 / 结构异常 / `notice_content` 为空）一律抛 `NoticeError`，
      调用方据此如实降级为「仅标题口径」——**不返回空壳、不用标题拼接充数**；
    - 模块级 TTL 缓存 + 失败冷却，与列表接口一致（正文不会变，故 TTL 更长）。
    """
    key = str(art_code or "")
    if not _ART_CODE_PATTERN.match(key):
        raise NoticeError(f"art_code 形状不符（{key!r}），不发起正文请求")
    now = time.monotonic()
    cached = _content_cache.get(key)
    if cached and now - cached[0] < _CONTENT_CACHE_TTL_SECONDS:
        return cached[1]
    fail_at = _content_fail_cache.get(key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise NoticeError(f"{key} 公告正文近期拉取失败，处于冷却期")

    try:
        data = _ann_content_http_get(key)
    except NoticeError:
        _content_fail_cache[key] = time.monotonic()
        raise

    node = data.get("data")
    # 失败形态是 {"success": 0, "error": …}——没有 data 节点，据实判失败（不猜原因）。
    content = clean_notice_content(node.get("notice_content")) if isinstance(node, dict) else ""
    if not content:
        _content_fail_cache[key] = time.monotonic()
        raise NoticeError(f"{key} 公告正文为空（接口未返回 notice_content）")

    attach = ""
    if isinstance(node, dict):
        for candidate in (node.get("attach_url_web"), node.get("attach_url")):
            if isinstance(candidate, str) and candidate.strip():
                attach = candidate.strip()
                break
    body = AnnouncementBody(art_code=key, content=content, attach_url=attach or None)
    _content_cache[key] = (time.monotonic(), body)
    return body


def announcement_body_targets(
    items: Sequence[Announcement], limit: int = ANNOUNCEMENT_BODY_LIMIT
) -> list[Announcement]:
    """挑出「值得且可以」取正文的公告：最近 `limit` 条 + art_code 形状合格。

    形状预校验放在这里（而不是等 `fetch_announcement_content` 抛），是为了让调用方**事先**
    知道「打算取几条」，从而把「N 条尝试 / M 条成功」如实写进降级说明，而不是笼统地说
    「正文没取到」。
    """
    targets: list[Announcement] = []
    for item in items:
        if len(targets) >= max(0, limit):
            break
        if _ART_CODE_PATTERN.match(str(getattr(item, "art_code", "") or "")):
            targets.append(item)
    return targets


def fetch_announcement_bodies(
    items: Sequence[Announcement], limit: int = ANNOUNCEMENT_BODY_LIMIT
) -> dict[str, AnnouncementBody]:
    """逐条尽力取正文，返回 `{art_code: AnnouncementBody}`（**只含取到的**）。

    单条失败只跳过该条并记日志：正文是加分项，某一条取不到不应让整批公告一起降级，
    更不应影响报告生成。调用方用 `len(announcement_body_targets(items, limit)) - len(结果)`
    得到失败条数，据此如实标注（不把「没取到」说成「没有正文」）。
    """
    bodies: dict[str, AnnouncementBody] = {}
    for item in announcement_body_targets(items, limit):
        try:
            bodies[item.art_code] = fetch_announcement_content(item.art_code)
        except NoticeError as error:
            logger.warning("公告正文抓取失败 art_code=%s: %s", item.art_code, error)
    return bodies


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