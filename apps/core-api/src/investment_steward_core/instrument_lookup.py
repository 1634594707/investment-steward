"""M4-E01：标的查找（代码 ⇄ 名称），供新建持仓/自选的搜索框使用。

名称源沿用平台**已在用的行情供应方**（同一批主机，不新接主数据供应商）：
  ① 东方财富搜索建议（`searchapi.eastmoney.com`，与估值横截面同源）——主源：
     UTF-8 JSON，逐条带 `Classify`（AStock / Fund / HK / Option…）；
  ② 腾讯 smartbox（`smartbox.gtimg.cn`，与 K 线行情同源）——回退：GBK，
     类型标记为 `GP-A`（A 股）/ `ETF` / `GP`（港股）/ `QZ`（权证）。

只保留 **6 位 A 股 / 场内 ETF** 代码：港股、期权、美股、场外基金等一律剔除——
与 `POST /holdings` 的 `normalize_instrument()` 口径一致（lookup 给出的 kind 必然能通过落库校验）。
任何网络/解析失败都返回空列表，绝不抛 500（E01 验收：非法输入返回空列表）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request

from .instruments import normalize_instrument

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_REQUEST_TIMEOUT = 8

# 东方财富搜索建议。token 为其 Web 前端 JS 内公开常量，非用户凭据。
_EM_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
_EM_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"
_EM_REFERER = "https://www.eastmoney.com/"
# 东财 Classify → 本平台候选（再经 normalize_instrument 终判 kind）。
_EM_CLASSIFY_KEEP = frozenset({"AStock", "Fund", "Index"})

# 腾讯 smartbox。
_TX_SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"
_TX_REFERER = "https://gu.qq.com/"
# 腾讯类型标记：GP-A=A 股，ETF=场内基金；GP（港股）/QZ（权证）等剔除。
_TX_TYPE_KEEP = frozenset({"GP-A", "ETF"})

# 搜索结果短缓存：搜索框逐字触发，避免连打供应方；失败不缓存（允许重试）。
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _build_entry(code: str, name: str) -> dict[str, str] | None:
    """6 位码 + 名称 → 统一返回条目；非 6 位或非 A 股/ETF 品种返回 None。"""
    identity = normalize_instrument(code)
    if not identity.key or identity.kind not in ("stock", "etf"):
        return None
    clean_name = name.strip()
    return {
        "key": identity.key,
        "name": clean_name,
        "kind": identity.kind,
        "market": identity.market,
        "display": f"{clean_name}（{identity.key}）" if clean_name else identity.key,
    }


def parse_em_suggest(payload: object) -> list[dict[str, str]]:
    """东财搜索建议响应 → 统一条目（纯函数，按真实报文结构解析）。"""
    if not isinstance(payload, dict):
        return []
    table = payload.get("QuotationCodeTable")
    rows = table.get("Data") if isinstance(table, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("Classify") or "") not in _EM_CLASSIFY_KEEP:
            continue
        entry = _build_entry(str(row.get("Code") or ""), str(row.get("Name") or ""))
        if entry is not None:
            out.append(entry)
    return out


def _unescape_text(value: str) -> str:
    """腾讯把中文名写成 `\\u4e1c\\u745e` 转义序列；用 JSON 字面量解码，非法则原样返回。"""
    if "\\u" not in value:
        return value
    try:
        return json.loads(f'"{value}"')
    except (ValueError, TypeError):
        return value


def parse_tx_smartbox(text: str) -> list[dict[str, str]]:
    """腾讯 smartbox 响应 → 统一条目（纯函数）。

    格式：`v_hint="mkt~code~name~pinyin~type^mkt~code~..."`；无结果时为 `v_hint="N"`。
    """
    match = re.search(r'v_hint="([^"]*)"', str(text or ""))
    if match is None:
        return []
    body = match.group(1).strip()
    if not body or body == "N":
        return []
    out: list[dict[str, str]] = []
    for chunk in body.split("^"):
        fields = chunk.split("~")
        if len(fields) < 5:
            continue
        if fields[4].strip() not in _TX_TYPE_KEEP:
            continue
        entry = _build_entry(fields[1].strip(), _unescape_text(fields[2].strip()))
        if entry is not None:
            out.append(entry)
    return out


def _http_text(url: str, referer: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Referer": referer})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_em(query: str) -> list[dict[str, str]]:
    params = {
        "input": query,
        "type": "14",
        "token": _EM_SUGGEST_TOKEN,
        "count": "10",
    }
    url = f"{_EM_SUGGEST_URL}?{urllib.parse.urlencode(params)}"
    return parse_em_suggest(json.loads(_http_text(url, _EM_REFERER)))


def _fetch_tx(query: str) -> list[dict[str, str]]:
    url = f"{_TX_SMARTBOX_URL}?{urllib.parse.urlencode({'q': query, 't': 'all'})}"
    return parse_tx_smartbox(_http_text(url, _TX_REFERER))


def _query_variants(clean: str) -> list[str]:
    """查询变体：原样 + 抽出 6 位数字（用户可能贴 `sh510300` 这类带前缀写法）。"""
    digits = re.sub(r"\D", "", clean)
    variants = [clean]
    if len(digits) == 6 and digits != clean:
        variants.append(digits)
    return variants


def lookup_instruments(query: str, *, limit: int = 8) -> list[dict[str, str]]:
    """按代码或名称查找标的（6 位码 → 名称；中文 → 6 位码）。

    返回 `[{key, name, kind, market, display}]`，最多 `limit` 条。
    主源东财；主源无命中或不可用时回退腾讯；两者都不可用/无命中 → 空列表（不抛错）。
    """
    clean = str(query or "").strip()
    if not clean:
        return []
    now = time.monotonic()
    cached = _cache.get(clean)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1][:limit]

    found: list[dict[str, str]] = []
    # 先按变体顺序试主源；主源全部无命中再试回退源。
    for source, fetch in (("em", _fetch_em), ("tx", _fetch_tx)):
        for variant in _query_variants(clean):
            try:
                rows = fetch(variant)
            except Exception as error:  # noqa: BLE001 - 搜索失败即降级，不打断调用方
                logger.warning("instrument lookup 源 %s 查询 %r 失败：%s", source, variant, error)
                rows = []
            if rows:
                found = rows
                break
        if found:
            break

    _cache[clean] = (now, found)
    return found[:limit]


def reset_lookup_cache() -> None:
    """清空查找缓存（测试与手动排障用）。"""
    _cache.clear()
