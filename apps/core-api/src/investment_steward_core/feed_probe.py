"""数据源探测（F03，桌面端升级路线图 2026-09-18）。

「仅重试失败项」需要一个**真实但最小**的请求：本模块为每个可探测来源登记一次代表性
取数，由 `POST /data-source/retry` 触发；请求本身经 F02 的埋点自动落 `feed_attempts`，
所以**重试的结果就是下一次健康度统计的样本**，不需要第二套记录。

三条纪律：
1. **只打失败过的源。** `only_failed` 只探测窗口内失败过的来源，不对健康的源重复打上游
   （与 D03 的礼貌限速同一原则）。
2. **绕缓存，否则探测没有意义。** 取数函数大多有 60–300 秒 TTL 缓存；缓存命中不会发请求、
   也就不会产生样本，面板会显示"重试了但没有新样本"——这比不重试更让人困惑。各条探测
   一律走模块低层函数或显式清对应缓存，并在注释里写明绕的是哪个缓存。
3. **需要外部输入的源不提供探测。** 龙虎榜与中金所按**交易日**取数，而自然日不等于交易日：
   拿今天去探，非交易日拿到 HTML 错误页，会记下一条**假的源故障**。这两条如实声明
   「不在此重试」并把用户指回对应页面，不猜日期。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from investment_steward_core import (
    comtrade_feed,
    feed_health,
    macro_feed,
    market_feed,
    valuation_evidence,
)
from investment_steward_core.credential_store import CredentialStore

#: 探测用标的：沪深 300ETF，两市场都能取到、无停牌歧义、非 ST。
_PROBE_ETF = "510300"

#: 不提供探测的来源 → 原因（面向用户，直接出现在面板上）。
UNSUPPORTED: dict[str, str] = {
    feed_health.SOURCE_LHB_EASTMONEY: "龙虎榜按交易日取数，请从「游资雷达 → 今日席位」选好交易日重跑。",
    feed_health.SOURCE_CFFEX: "中金所按交易日取数（非交易日文件不存在），请从方向研判证据包页面重跑。",
}


class _Skip(RuntimeError):
    """缺前置条件（如凭据）→ 未发起请求，**不计失败也不计成功**。"""


class _ProbeFailed(RuntimeError):
    """探测拿到了响应但内容不可用（如 Comtrade 的 available=false）。"""


def _probe_cn_market_tencent() -> str:
    # 走低层函数：不经 fetch_cn_kline 的 90 秒 TTL 缓存与失败冷却（那里缓存命中不会发请求）。
    rows = market_feed._fetch_tx_kline(_PROBE_ETF, 5)
    return f"取到 {len(rows)} 根日 K，最新 {str(rows[-1]['timestamp'])[:10]}"


def _probe_cn_market_eastmoney() -> str:
    rows = market_feed._fetch_em_kline(_PROBE_ETF, 5)
    return f"取到 {len(rows)} 根日 K，最新 {str(rows[-1]['timestamp'])[:10]}"


def _probe_cn_market_sina() -> str:
    # 行业板块清单有 5 分钟模块缓存：置空它，确保这次是真实请求。
    market_feed._sector_list_cache = None
    rows = market_feed.fetch_cn_sector_list()
    return f"取到 {len(rows)} 个行业板块"


def _probe_valuation() -> str:
    # _query 的缓存键是 (flt, columns, page_number)：用**生产路径没用过的列集合**作探测键，
    # 既绕过 90 秒缓存与失败冷却（保证真实请求），又不覆盖生产口径的缓存内容。
    rows = valuation_evidence._query(
        "SECURITY_CODE,TRADE_DATE,PE_TTM", "", page_size=1, sort_column="TRADE_DATE", desc=True
    )
    if not rows:
        raise _ProbeFailed("估值源未返回任何行")
    return f"取到最新交易日 {str(rows[0].get('TRADE_DATE'))[:10]}"


def _probe_macro_eastmoney() -> str:
    rows = macro_feed.fetch_em_macro_series("RPT_ECONOMY_PMI|MAKE_INDEX", limit=3)
    if not rows:
        raise _ProbeFailed("东财宏观未返回 PMI 观测")
    return f"取到 {len(rows)} 期 PMI，最新 {rows[0]['obs_date']}"


def _probe_macro_fred(store: CredentialStore) -> str:
    api_key = store.get("macro_data_key")
    if not api_key:
        raise _Skip("凭据库无 FRED key（设置 → 数据源与密钥录入）")
    rows = macro_feed.fetch_fred_series("FEDFUNDS", api_key, limit=3)
    if not rows:
        raise _ProbeFailed("FRED 未返回观测")
    return f"取到 {len(rows)} 期联邦基金利率，最新 {rows[0]['obs_date']}"


def _probe_macro_worldbank() -> str:
    rows = macro_feed.fetch_worldbank_series("CHN", "FP.CPI.TOTL.ZG")
    if not rows:
        raise _ProbeFailed("世界银行未返回观测")
    return f"取到 {len(rows)} 期中国 CPI，最新 {rows[0]['obs_date']}"


def _probe_macro_okx() -> str:
    rows = macro_feed.fetch_okx_series("XAUT-USDT", limit=3)
    if not rows:
        raise _ProbeFailed("OKX 未返回观测")
    return f"取到 {len(rows)} 根日线，最新 {rows[0]['obs_date']}"


def _probe_comtrade(store: CredentialStore) -> str:
    api_key = store.get("comtrade_key") or store.get("comtrade_api_key")
    if not api_key:
        raise _Skip("凭据库无 comtrade_key（设置 → 数据源与密钥录入）")
    payload = comtrade_feed.fetch_preview(api_key, max_records=5)
    if not payload.get("available"):
        raise _ProbeFailed(str(payload.get("degraded_reason") or "Comtrade 未返回可用数据"))
    return f"取到 {payload.get('count')} 条贸易记录"


#: 可探测来源 → 探测函数。**必须覆盖 feed_health.ALL_SOURCES 减去 UNSUPPORTED**，
#: 由测试断言（新增埋点标签时不会漏登记探测）。
PROBES: dict[str, Callable[[CredentialStore], str]] = {
    feed_health.SOURCE_CN_MARKET_TENCENT: lambda _store: _probe_cn_market_tencent(),
    feed_health.SOURCE_CN_MARKET_EASTMONEY: lambda _store: _probe_cn_market_eastmoney(),
    feed_health.SOURCE_CN_MARKET_SINA: lambda _store: _probe_cn_market_sina(),
    feed_health.SOURCE_VALUATION_EASTMONEY: lambda _store: _probe_valuation(),
    feed_health.SOURCE_MACRO_EASTMONEY: lambda _store: _probe_macro_eastmoney(),
    feed_health.SOURCE_MACRO_FRED: _probe_macro_fred,
    feed_health.SOURCE_MACRO_WORLDBANK: lambda _store: _probe_macro_worldbank(),
    feed_health.SOURCE_MACRO_OKX: lambda _store: _probe_macro_okx(),
    feed_health.SOURCE_COMTRADE: _probe_comtrade,
}


def retry_plan(sources: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """每个来源「能否在这里重试」及原因（界面据此决定给不给按钮，而不是前端再抄一份名单）。"""
    plan: dict[str, dict[str, Any]] = {}
    for source in sources:
        if source in PROBES:
            plan[source] = {"retryable": True, "reason": None}
        elif source in UNSUPPORTED:
            plan[source] = {"retryable": False, "reason": UNSUPPORTED[source]}
        else:
            plan[source] = {"retryable": False, "reason": "该来源未登记探测方法（可能来自旧版本埋点）。"}
    return plan


def probe_sources(sources: list[str], store: CredentialStore) -> list[dict[str, Any]]:
    """逐个探测；任一源失败不中断其余源（一次源抖动不该让整块面板变成错误页）。"""
    results: list[dict[str, Any]] = []
    for source in sources:
        run = PROBES.get(source)
        if run is None:
            results.append(
                {
                    "source": source,
                    "supported": False,
                    "ok": None,
                    "reason": UNSUPPORTED.get(source, "未登记该来源的探测方法。"),
                }
            )
            continue
        try:
            detail = run(store)
        except _Skip as error:
            results.append({"source": source, "supported": True, "ok": None, "skipped": True, "reason": str(error)})
        except Exception as error:  # noqa: BLE001 - 逐源报告，不中断整批
            results.append(
                {
                    "source": source,
                    "supported": True,
                    "ok": False,
                    "reason": f"{type(error).__name__}: {error}"[:300],
                }
            )
        else:
            results.append({"source": source, "supported": True, "ok": True, "detail": detail})
    return results
