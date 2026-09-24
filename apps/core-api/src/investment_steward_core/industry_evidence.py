"""行业细分证据适配器（M2-C04/C05，2026-09-15 路线图）。

接入原则（ADR-0006 + 路线图 §8）：只接入**实际取得并核验过结构**的来源，
没有实际取得的数据不写成已经接入。当前接入：
- 快递：国家邮政局「统计信息」栏目（月度运行情况公告，HTML 文本格式稳定，正则解析）。
  2026-09-15 实测：列表页可访问，最新公告《国家邮政局公布2026年1-7月邮政行业运行情况》
  （2026-08-14 发布），含当月/累计件量、收入、同比、CR8；公告链接为**相对路径**。
- 航运：上海航运交易所官方接口 `GET /index/getIndexData?indexType=<t>`（JSON 直出，
  2026-09-15 实测，周度两期）：
    scfi  → SCFI 综合（t）                     2026-09-04 → 2026-09-11
    ccfi  → CCFI 综合（t）                     2026-09-04 → 2026-09-11
    scfis → SCFIS 结算运价（欧线 eu/美西 uswc）  2026-09-07 → 2026-09-14
    cbfi  → CBFI 中国进口干散货运价指数（综合 t）
    ctfi  → CTFI 中国进口原油运价指数（综合 t、ct1_WS 等）
  注意：ctfi 的 *_TCE 字段数值口径无法核验（量级异常），**不输出**（不猜口径）；
  油运 BDTI / VLCC TCE（波罗的海交易所口径）无公开授权源，未接入，以 CTFI 为替代口径并如实标注。
"""

from __future__ import annotations

import json as _json
import re
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) investment-steward-core/0.1 (industry-evidence; local-first)"
_REQUEST_TIMEOUT = 15.0

# 缓存：月度公告 6 小时 / 周度指数 6 小时（证据层非行情层，不需要分钟级新鲜度）；
# 失败冷却 10 分钟（与 macro_feed._OKX_FAIL_COOLDOWN_MINUTES 同风格，避免高频重试打源站）。
_CACHE_TTL_SECONDS = 6 * 3600.0
_FAIL_COOLDOWN_SECONDS = 600.0

_SPB_LIST_URL = "https://www.spb.gov.cn/gjyzj/c100276/common_list.shtml"
_SSE_HOME_URL = "https://www.sse.net.cn/"
# 2026-09-15 实测：列表页公告链接为**相对路径**（/gjyzj/c100015/c100016/...shtml），
# 锚文本含「国家邮政局公布…运行情况」与发布日期。
_SPB_ARTICLE_LINK = re.compile(
    r"href=\"((?:https://www\.spb\.gov\.cn)?/gjyzj/[^\"]+\.shtml)\"[^>]*>([\s\S]{0,400}?)</a>",
)
# 标题期间含跨月连字符（如「2026年1-7月」），字符类必须包含 `-`，否则跨月公告全部漏配
# （2026-09-15 实测踩坑：原 `[\d年月]+` 只配到《2025年》年报，最新《2026年1-7月》被漏掉）。
_SPB_RUN_TITLE = re.compile(r"国家邮政局公布([\d年月\-]+)邮政行业运行情况")
_SPB_BASE = "https://www.spb.gov.cn"
_express_cache: tuple[float, dict[str, Any]] | None = None
_express_fail_until = 0.0
_shipping_cache: tuple[float, dict[str, Any]] | None = None
_shipping_fail_until = 0.0


def _http_get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
        raw = response.read()
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------------------
# C04：快递最小证据包（国家邮政局月度运行情况）
# ---------------------------------------------------------------------------

# 公告正文句式（2026-09-15 实测样本）：
#   「7月份，…快递业务量完成170.8亿件，同比增长4.1%」「快递业务收入完成1303.7亿元，同比增长8.1%」
#   「快递业务量累计完成1174.7亿件，同比增长4.8%」「快递业务收入累计完成9017.7亿元，同比增长7.4%」
#   「快递业务收入品牌集中度指数CR8为87.0」
_MONTHLY_VOLUME = re.compile(r"(\d+)月份[，,].{0,40}?快递业务量完成([\d.]+)亿件，同比增长(-?[\d.]+)%")
_MONTHLY_REVENUE = re.compile(r"(\d+)月份[，,].{0,40}?快递业务收入完成([\d.]+)亿元，同比增长(-?[\d.]+)%")
_CUM_VOLUME = re.compile(r"快递业务量累计完成([\d.]+)亿件，同比增长(-?[\d.]+)%")
_CUM_REVENUE = re.compile(r"快递业务收入累计完成([\d.]+)亿元，同比增长(-?[\d.]+)%")
_CR8 = re.compile(r"快递业务收入品牌集中度指数CR8为([\d.]+)")
_PUBLISH_DATE = re.compile(r"日期：(\d{4}-\d{2}-\d{2})")


def fetch_express_evidence(force_refresh: bool = False) -> dict[str, Any]:
    """快递行业月度证据（国家邮政局公告）：件量 / 收入 / 同比 / CR8 / 单票收入。

    单票收入 = 当月快递业务收入 ÷ 当月快递业务量（分子分母同源同月，可追溯）。
    返回 {ok, source, source_url, release_date, retrieved_at, metrics, coverage, note}
    或 {ok: False, detail}（解析失败/不可访问，缺口如实声明）。
    """
    global _express_cache, _express_fail_until
    now = time.monotonic()
    if not force_refresh and _express_cache and now - _express_cache[0] < _CACHE_TTL_SECONDS:
        return _express_cache[1]
    if not force_refresh and now < _express_fail_until:
        return {"ok": False, "detail": "国家邮政局取数近期失败，冷却中（10 分钟后自动重试）"}
    try:
        list_html = _http_get_text(_SPB_LIST_URL)
        candidates: list[tuple[str, str, str]] = []  # (url, 期间标签, 发布日期)
        for match in _SPB_ARTICLE_LINK.finditer(list_html):
            href, anchor_text = match.group(1), match.group(2)
            title_match = _SPB_RUN_TITLE.search(anchor_text)
            if title_match is None:
                continue
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", anchor_text)
            article_url = href if href.startswith("http") else f"{_SPB_BASE}{href}"
            candidates.append((article_url, title_match.group(1), date_match.group(1) if date_match else ""))
        if not candidates:
            raise RuntimeError("统计信息列表页未解析到「运行情况」公告链接（页面结构可能变更）")
        # 页面锚点顺序不保证最新在前（侧栏/推荐位可能插旧公告），按发布日期取最新。
        article_url, period_label, release_date = max(candidates, key=lambda item: item[2])
        article_text = _strip_html(_http_get_text(article_url))
        metrics: list[dict[str, Any]] = []
        monthly_volume = _MONTHLY_VOLUME.search(article_text)
        monthly_revenue = _MONTHLY_REVENUE.search(article_text)
        cum_volume = _CUM_VOLUME.search(article_text)
        cum_revenue = _CUM_REVENUE.search(article_text)
        cr8 = _CR8.search(article_text)
        unit_price = None
        if monthly_volume:
            metrics.append({
                "name": "快递业务量", "value": float(monthly_volume.group(2)), "unit": "亿件",
                "yoy_pct": float(monthly_volume.group(3)), "scope": f"{monthly_volume.group(1)}月当月",
            })
        if monthly_revenue:
            metrics.append({
                "name": "快递业务收入", "value": float(monthly_revenue.group(2)), "unit": "亿元",
                "yoy_pct": float(monthly_revenue.group(3)), "scope": f"{monthly_revenue.group(1)}月当月",
            })
        if monthly_revenue and monthly_volume and monthly_volume.group(1) == monthly_revenue.group(1):
            # 单票收入：分子=当月收入（亿元），分母=当月件量（亿件）→ 元/件（分子分母同源可追溯）。
            unit_price = round(float(monthly_revenue.group(2)) / float(monthly_volume.group(2)), 3)
            metrics.append({
                "name": "单票收入（推算）", "value": unit_price, "unit": "元/件",
                "yoy_pct": None,
                "scope": f"{monthly_volume.group(1)}月当月",
                "derivation": f"{monthly_revenue.group(2)}亿元 ÷ {monthly_volume.group(2)}亿件",
            })
        if cum_volume:
            metrics.append({
                "name": "快递业务量（累计）", "value": float(cum_volume.group(1)), "unit": "亿件",
                "yoy_pct": float(cum_volume.group(2)), "scope": "累计",
            })
        if cum_revenue:
            metrics.append({
                "name": "快递业务收入（累计）", "value": float(cum_revenue.group(1)), "unit": "亿元",
                "yoy_pct": float(cum_revenue.group(2)), "scope": "累计",
            })
        if cr8:
            metrics.append({
                "name": "快递业务收入品牌集中度 CR8", "value": float(cr8.group(1)), "unit": "",
                "yoy_pct": None, "scope": "累计",
            })
        if not metrics:
            raise RuntimeError("公告正文未解析到任何指标（格式可能变更）")
        # required 名与 metrics 全名对齐（否则 CR8 会被误报为缺失）。
        required = ["快递业务量", "快递业务收入", "单票收入（推算）", "快递业务收入品牌集中度 CR8"]
        available = {str(item["name"]).replace("（累计）", "") for item in metrics}
        missing = [name for name in required if name not in available]
        payload: dict[str, Any] = {
            "ok": True,
            "source": "国家邮政局「统计信息」栏目（月度运行情况公告）",
            "source_url": article_url,
            "period_label": period_label,
            "release_date": release_date,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "frequency": "monthly",
            "metrics": metrics,
            # C07：覆盖率如实标注——必需指标可得数 / 缺失数 / 观测期。
            "coverage": {
                "required": required,
                "available": sorted(available),
                "missing": missing,
                "observation_period": period_label,
                "missing_note": "上市公司经营公告量价（顺丰/中通/圆通等分企口径）未接入" if missing else "",
            },
            "note": "月度值，观测期=公告标注的期间；同比增长为官方口径；单票收入为收入÷件量推算值",
        }
        _express_cache = (now, payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - 单来源失败如实标注
        _express_fail_until = time.monotonic() + _FAIL_COOLDOWN_SECONDS
        return {"ok": False, "detail": f"国家邮政局取数失败：{exc}"}


# ---------------------------------------------------------------------------
# C05：航运细分证据（上海航运交易所官方 JSON 接口，周度两期）
# ---------------------------------------------------------------------------

# (indexType, 名称, 子序列键列表)：t=综合指数；scfis 无 t，取 eu/uswc 两条子序列。
# 2026-09-15 实测核验的结构；未知键不输出（不猜口径）。
_SSE_INDEX_SPECS: list[tuple[str, str, tuple[str, ...]]] = [
    ("scfi", "SCFI 上海出口集装箱运价指数（综合）", ("t",)),
    ("ccfi", "CCFI 中国出口集装箱运价指数（综合）", ("t",)),
    ("scfis", "SCFIS 上海出口集装箱结算运价指数", ("eu", "uswc")),
    ("cbfi", "CBFI 中国进口干散货运价指数（综合）", ("t",)),
    ("ctfi", "CTFI 中国进口原油运价指数（综合）", ("t", "ct1_WS")),
]
_SSE_SUB_INDEX_LABELS = {"t": "综合", "eu": "欧线", "uswc": "美西线", "ct1_WS": "中东-中国航线（WS 点数）"}


def _fetch_sse_index(index_type: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://www.sse.net.cn/index/getIndexData?indexType={index_type}",
        headers={"User-Agent": _USER_AGENT, "Referer": "https://www.sse.net.cn/"},
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
        payload = _json.loads(response.read().decode("utf-8"))
    if not isinstance(payload.get("curIndex"), dict) or not isinstance(payload.get("lastIndex"), dict):
        raise RuntimeError(f"indexType={index_type} 响应缺少 curIndex/lastIndex")
    return payload


def fetch_shipping_indices(force_refresh: bool = False) -> dict[str, Any]:
    """上海航运交易所周度运价指数（官方 JSON 接口，最近两期）。

    覆盖集运（SCFI/CCFI/SCFIS）、干散货（CBFI）、油运（CTFI，上海航交所官方口径，
    **非 BDTI**）。指数为**运价水平点数**（周度），不是收益率/涨跌幅收益，
    禁止与日收益混用（C05 口径）。ctfi 的 *_TCE 字段口径未核验，不输出。
    """
    global _shipping_cache, _shipping_fail_until
    now = time.monotonic()
    if not force_refresh and _shipping_cache and now - _shipping_cache[0] < _CACHE_TTL_SECONDS:
        return _shipping_cache[1]
    if not force_refresh and now < _shipping_fail_until:
        return {"ok": False, "detail": "上海航运交易所取数近期失败，冷却中（10 分钟后自动重试）"}
    try:
        indices: list[dict[str, Any]] = []
        failures: list[str] = []
        for index_type, label, keys in _SSE_INDEX_SPECS:
            try:
                payload = _fetch_sse_index(index_type)
            except Exception as exc:  # noqa: BLE001 - 单指数失败不影响其余
                failures.append(f"{label}：{exc}")
                continue
            cur, last = payload["curIndex"], payload["lastIndex"]
            for key in keys:
                current_value = cur.get(key)
                previous_value = last.get(key)
                if current_value is None or previous_value is None:
                    continue  # 缺期不编造
                sub_label = _SSE_SUB_INDEX_LABELS.get(key, key)
                indices.append({
                    "name": label if key == "t" and len(keys) == 1 else f"{label} · {sub_label}",
                    "previous": float(previous_value),
                    "previous_date": str(last.get("mdate") or payload.get("lastDate") or ""),
                    "current": float(current_value),
                    "current_date": str(cur.get("mdate") or payload.get("curDate") or ""),
                    "change": round(float(current_value) - float(previous_value), 4),
                    "unit": "点" if key != "ct1_WS" else "WS 点",
                })
        if not indices:
            raise RuntimeError("所有航运指数均未取得数据")
        payload_out: dict[str, Any] = {
            "ok": True,
            "publisher": "上海航运交易所官方接口 /index/getIndexData（每周五发布；SCFIS 为周一）",
            "frequency": "weekly",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "indices": indices,
            "partial_failures": failures,
            # C07：覆盖率——集运/干散货/油运（CTFI 替代口径）可得；BDTI 波罗的海口径未接入。
            "coverage": {
                "required": ["SCFI（集运）", "CCFI（集运）", "干散货运价", "油运运价（BDTI/VLCC TCE）"],
                "available": sorted({item["name"].split(" · ")[0] for item in indices}),
                "missing": ["BDTI / VLCC TCE（波罗的海交易所口径）"],
                "observation_period": f"{min(i['previous_date'] for i in indices)} ~ {max(i['current_date'] for i in indices)}",
                "missing_note": "油运用 CTFI（上海航交所官方）替代，口径与 BDTI 不同，不可直接对比；BDI（波罗的海干散货）未接入",
            },
            "note": "指数为运价水平点数（周度），非收益率；本管道只存最近两期，环比可算、同比不可算（不编造）",
        }
        _shipping_cache = (now, payload_out)
        return payload_out
    except Exception as exc:  # noqa: BLE001
        _shipping_fail_until = time.monotonic() + _FAIL_COOLDOWN_SECONDS
        return {"ok": False, "detail": f"上海航运交易所取数失败：{exc}"}


# ---------------------------------------------------------------------------
# 方向研判接入：按主题关键词选择细分行业证据
# ---------------------------------------------------------------------------

_EXPRESS_KEYWORDS: tuple[str, ...] = ("快递", "物流", "邮政", "包裹")
_SHIPPING_KEYWORDS: tuple[str, ...] = ("航运", "海运", "集运", "集装箱", "油运", "干散", "散货", "港口", "运价")


def topic_industry_hits(topic: str) -> list[str]:
    """主题命中的细分行业证据通道（express/shipping）；未命中返回空。"""
    hits: list[str] = []
    if any(word in topic for word in _EXPRESS_KEYWORDS):
        hits.append("express")
    if any(word in topic for word in _SHIPPING_KEYWORDS):
        hits.append("shipping")
    return hits


def direction_industry_evidence(topic: str) -> tuple[list[dict[str, Any]], list[str]]:
    """按主题取细分行业证据条目，返回 (items, failures)。

    items 形状与 `_build_direction_evidence` 的 snapshot 兼容（evidence_id 由调用方编排）：
    {source_type, source, title, content, retrieved_at}。失败进 failures（不静默）。
    """
    items: list[dict[str, Any]] = []
    failures: list[str] = []
    hits = topic_industry_hits(topic)
    if "express" in hits:
        payload = fetch_express_evidence()
        if payload.get("ok"):
            metrics_text = "；".join(
                f"{item['name']} {item['value']}{item['unit']}"
                + (f"，同比 {item['yoy_pct']}%" if item.get("yoy_pct") is not None else "")
                + f"（{item['scope']}）"
                for item in payload["metrics"]
            )
            coverage = payload["coverage"]
            items.append({
                "source_type": "industry_data",
                "source": f"{payload['source']}；发布 {payload.get('release_date') or '未知'}",
                "title": f"快递行业量价证据（{payload['period_label']}）",
                "content": (
                    f"{metrics_text}。"
                    f"频率：月度；观测期：{coverage['observation_period']}；"
                    f"覆盖率：可得 {len(coverage['available'])} 项，缺失 {len(coverage['missing'])} 项"
                    f"（{('、'.join(coverage['missing']) or '无')}——{coverage['missing_note']}）。"
                    f"{payload['note']}"
                ),
                "retrieved_at": payload["retrieved_at"],
                "source_url": payload.get("source_url", ""),
            })
        else:
            failures.append(f"快递行业数据（国家邮政局）：{payload.get('detail', '取数失败')}")
    if "shipping" in hits:
        payload = fetch_shipping_indices()
        if payload.get("ok"):
            indices_text = "；".join(
                f"{item['name']} 本期 {item['current']} 点（上期 {item['previous']}，涨跌 {item['change']}）"
                for item in payload["indices"]
            )
            coverage = payload["coverage"]
            items.append({
                "source_type": "industry_data",
                "source": payload["publisher"],
                "title": "集运运价指数（周度）",
                "content": (
                    f"{indices_text}。"
                    f"频率：周度（每周五发布）；观测期：{coverage['observation_period']}；"
                    f"覆盖率：SCFI/CCFI 可得，缺失 {('、'.join(coverage['missing']) or '无')}"
                    f"（{coverage['missing_note']}）。"
                    f"{payload['note']}"
                ),
                "retrieved_at": payload["retrieved_at"],
            })
        else:
            failures.append(f"航运运价数据（上海航运交易所）：{payload.get('detail', '取数失败')}")
    return items, failures
