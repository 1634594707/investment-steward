"""三方取数健康度（F02/F03，桌面端升级路线图 2026-09-18）。

问题：`data_source_quality` 此前只能从**已经落库的证据**复算覆盖率，成功率与延迟一律
返回 None，理由写「本机无采集成功/失败日志」——因为**取数本身没有被记录**：只有成功
入账的证据留下痕迹，失败的采集（连接被断、HTML 错误页、接口 5xx、JSON 结构异常）什么
都不留，用户看到的永远是「数据不足」。

做法：在 6 个三方 feed 的**低层 HTTP 出口**埋点，每次尝试写一行 `feed_attempts`
（来源、端点、参数指纹、开始时刻、延迟、结果、错误类别、第几次）。埋点走模块级
recorder 钩子（与 F01 的 `model_client.set_model_call_recorder` 同一模式），由
`create_app` 注入落库闭包；钩子未安装时（离线单测、脚本）是空操作，**绝不影响取数本身**。

三条口径（不要放宽）：
1. 延迟是本次 HTTP 往返的墙钟毫秒，不含上游缓存命中（命中缓存的调用不产生尝试记录，
   故「本窗口无采集日志」与「有日志但都成功」是两件事，界面须分开表达）。
2. 错误类别取自异常类型，**不解析供应商文案**（文案随版本变，字符串匹配必然腐化）。
3. 来源标签是 feed 侧的唯一登记处；与证据来源的合并**只做精确同名匹配**，不做模糊
   归一——宁可同一源出现两行，也不把两个不同口径的源合并成一个好看的数字。
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Callable

# —— 来源标签登记（feed 侧唯一来源，改这里即改全局口径） ——
SOURCE_CN_MARKET_TENCENT = "中国市场行情（腾讯行情）"
SOURCE_CN_MARKET_EASTMONEY = "中国市场行情（东方财富）"
SOURCE_CN_MARKET_SINA = "中国市场行情（新浪行情）"
SOURCE_LHB_EASTMONEY = "龙虎榜披露（东方财富）"
SOURCE_MACRO_EASTMONEY = "东财数据中心（公开接口）"
SOURCE_MACRO_FRED = "FRED（公开接口）"
SOURCE_MACRO_WORLDBANK = "世界银行（公开接口）"
SOURCE_MACRO_OKX = "OKX（公开接口）"
SOURCE_CFFEX = "中金所公开日频统计（cffex.com.cn）"
SOURCE_COMTRADE = "UN Comtrade（公开接口）"
SOURCE_VALUATION_EASTMONEY = "东方财富·估值分析"

#: 全部登记标签，供界面与自检使用（新增埋点必须在此登记）。
ALL_SOURCES: tuple[str, ...] = (
    SOURCE_CN_MARKET_TENCENT,
    SOURCE_CN_MARKET_EASTMONEY,
    SOURCE_CN_MARKET_SINA,
    SOURCE_LHB_EASTMONEY,
    SOURCE_MACRO_EASTMONEY,
    SOURCE_MACRO_FRED,
    SOURCE_MACRO_WORLDBANK,
    SOURCE_MACRO_OKX,
    SOURCE_CFFEX,
    SOURCE_COMTRADE,
    SOURCE_VALUATION_EASTMONEY,
)

#: 采集结果枚举（表内只写这两个值）。
RESULT_OK = "ok"
RESULT_ERROR = "error"

Recorder = Callable[[dict[str, Any]], None]

_recorder: Recorder | None = None


def set_feed_attempt_recorder(recorder: Recorder | None) -> None:
    """注入尝试记录回调：`recorder(record_dict)`；传 None 撤销（测试/脚本用）。"""
    global _recorder
    _recorder = recorder


def fingerprint(params: Any) -> str:
    """参数指纹：对参数做稳定序列化后取 sha1 前 12 位，用于按「同一类请求」聚合。

    不抛异常（不可序列化的对象退化为类型名），因为指纹失败不该影响取数。
    """
    try:
        payload = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - 指纹是附属信息，失败不阻断
        payload = type(params).__name__
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


#: 本仓库各 feed 的自有错误类（类名小写）→ feed_error：「上游响应不满足本模块校验」
#: （结构异常、空数据、HTML 错误页）。按名字匹配是刻意的——feed_health 不能 import 各
#: feed（会成环），而这里只需要区分「网络层失败」与「应用层判废」。
_FEED_ERROR_CLASSES = frozenset(
    {"feederror", "lhberror", "cffexerror", "valuationerror", "macroerror", "comtradeerror"}
)


def error_kind_of(error: BaseException) -> str:
    """按异常类型归类（不解析供应商文案）。

    异常可自带 `error_kind` 字符串属性显式声明类别，优先采用（降级信号类异常的类名对
    使用者无意义，靠类名反推只会得到 `_degraded` 这种噪声）。
    """
    explicit = getattr(error, "error_kind", None)
    if isinstance(explicit, str) and explicit:
        return explicit[:40]
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, urllib.error.HTTPError):
        return "http_error"
    if isinstance(error, urllib.error.URLError):
        # URLError 的 reason 为超时时归 timeout，其余（断连/DNS/TLS 指纹）归 network。
        reason = getattr(error, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid_payload"
    if isinstance(error, ValueError):
        return "invalid_payload"
    name = type(error).__name__.strip().lower()
    if name in _FEED_ERROR_CLASSES:
        return "feed_error"
    return name[:40] or "unknown"


def _emit(record: dict[str, Any]) -> None:
    if _recorder is None:
        return
    try:
        _recorder(record)
    except Exception:  # noqa: BLE001 - 记录失败绝不影响取数
        pass


@contextmanager
def attempt(
    source: str,
    endpoint: str,
    params: Any = None,
    *,
    attempt_index: int = 1,
) -> Iterator[None]:
    """包住**一次** HTTP 尝试：测量延迟、归类错误、上报后原样抛出。

    放在重试循环内部（而非函数外层），这样「第几次才失败」是可查的事实；
    `attempt_index` 由调用方按 1 起计传入。
    """
    started_at = datetime.now(UTC)
    started = time.monotonic()
    result = RESULT_OK
    error_kind: str | None = None
    try:
        yield
    except BaseException as error:  # noqa: BLE001 - 记录后原样抛出，不改控制流
        result = RESULT_ERROR
        error_kind = error_kind_of(error)
        raise
    finally:
        _emit(
            {
                "source": source,
                "endpoint": endpoint,
                "params_fingerprint": fingerprint(params),
                "started_at": started_at.isoformat(),
                "latency_ms": int(round((time.monotonic() - started) * 1000.0)),
                "result": result,
                "error_kind": error_kind,
                "attempt_index": int(attempt_index),
            }
        )


def percentile(values: list[float], ratio: float) -> float | None:
    """最近秩法（nearest-rank，`P_p = x_⌈p·n⌉`）：无样本返回 None，不插值、不外推。

    选最近秩而非插值：延迟分布长尾且样本常只有几十条，插值会给出一个**从未发生过的**
    延迟值；最近秩的每个结果都对应一次真实往返。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(ratio * len(ordered))
    index = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[index]


def summarize(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """把一批尝试行聚合成单源健康度（纯函数，便于单测与复算）。"""
    total = len(attempts)
    ok = sum(1 for row in attempts if row.get("result") == RESULT_OK)
    latencies = [float(row["latency_ms"]) for row in attempts if row.get("latency_ms") is not None]
    error_kinds: dict[str, int] = {}
    for row in attempts:
        if row.get("result") != RESULT_OK:
            kind = str(row.get("error_kind") or "unknown")
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
    return {
        "attempts": total,
        "ok": ok,
        "errors": total - ok,
        # 样本为 0 时给 None（「数据不足」），不写 0——0 是「全都失败」，含义不同。
        "success_rate": round(ok / total, 4) if total else None,
        "latency_ms": {
            "avg": round(sum(latencies) / len(latencies)) if latencies else None,
            "p50": _round_or_none(percentile(latencies, 0.5)),
            "p95": _round_or_none(percentile(latencies, 0.95)),
            "max": int(max(latencies)) if latencies else None,
        },
        "error_kinds": [
            {"kind": kind, "count": count} for kind, count in sorted(error_kinds.items(), key=lambda kv: -kv[1])
        ],
    }


def _round_or_none(value: float | None) -> int | None:
    return None if value is None else int(round(value))
