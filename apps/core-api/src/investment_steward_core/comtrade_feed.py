"""UN Comtrade 贸易数据预览适配器。

只返回 Comtrade API 实际返回的记录；凭据缺失、网络失败或响应不可解析时，
返回明确的 available=False，不用占位数据填充。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from investment_steward_core import feed_health

COMTRADE_API = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
_USER_AGENT = "investment-steward-core/0.1 (un-comtrade; local-first)"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SOURCE = feed_health.SOURCE_COMTRADE


class _Degraded(Exception):
    """内部信号：本次取数应降级为 `available=False`，message 即对用户可见的原因。

    F02：`error_kind` 显式声明，使取数日志不必靠类名反推错误类别（本类名对使用者无意义）。
    """

    error_kind = "invalid_payload"


def _degraded(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "source": "UN Comtrade",
        "data": [],
        "count": 0,
        "degraded_reason": reason,
    }


def fetch_preview(
    api_key: str | None,
    *,
    reporter_code: int | None = None,
    partner_code: int = 0,
    period: str | None = None,
    cmd_code: str = "TOTAL",
    flow_code: str = "M",
    max_records: int = 100,
) -> dict[str, Any]:
    """查询 Comtrade preview API，返回可安全展示的轻量响应。"""
    if not api_key:
        return _degraded("凭据库无 comtrade_key（设置 → 数据源与密钥录入）")

    params: dict[str, str] = {
        "cmdCode": cmd_code,
        "flowCode": flow_code,
        "partnerCode": str(partner_code),
        "partner2Code": "0",
        "motCode": "0",
        "maxRecords": str(max_records),
    }
    if reporter_code is not None:
        params["reporterCode"] = str(reporter_code)
    if period:
        params["period"] = period
    url = f"{COMTRADE_API}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        # F02：取数埋点。降级的四种情形（HTTP 错误、不可达、超限、结构不可用）全部经此
        # 记为失败——正是「只留下 available=False、没有日志」的那条链路。
        with feed_health.attempt(_SOURCE, COMTRADE_API, params):
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise _Degraded("Comtrade 响应超过本地安全大小限制")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise _Degraded("Comtrade 返回结构无法解析")
            rows = payload.get("data")
            if not isinstance(rows, list):
                detail = payload.get("error") or payload.get("message")
                raise _Degraded(f"Comtrade 返回无 data 列表{f'：{detail}' if detail else ''}")
    except urllib.error.HTTPError as error:
        return _degraded(f"Comtrade HTTP {error.code}（请检查 key 或查询参数）")
    except _Degraded as error:
        return _degraded(str(error))
    except (urllib.error.URLError, OSError, UnicodeError, ValueError) as error:
        return _degraded(f"Comtrade 暂不可达：{error}")

    return {
        "available": True,
        "source": "UN Comtrade",
        "source_url": COMTRADE_API,
        "query": {key: value for key, value in params.items() if key != "maxRecords"},
        "count": int(payload.get("count") or len(rows)),
        "data": rows[:max_records],
        "as_of": datetime.now(UTC).isoformat(),
    }
