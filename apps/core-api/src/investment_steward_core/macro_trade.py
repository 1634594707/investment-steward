"""UN Comtrade trade-flow adapter and response guards.

双端点策略（G0，2026-09-05）：
- 有订阅 key（凭据库 key_id=comtrade_key，尾 9FC3）→ 订阅端点 /data/v1/get/（header Ocp-Apim-Subscription-Key），
  不触 500 行上限（实测德国对华 TOTAL 1897 行可完整求和）；
- 无 key → 公共 preview 端点（100 次/天 · ≥5s 间隔 · max 500 行触顶）。
两类响应的 data 行结构一致（primaryValue / flowCode），聚合逻辑共用。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

COMTRADE_API = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
COMTRADE_SUBSCRIPTION_API = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
COMTRADE_SUBSCRIPTION_MONTHLY_API = COMTRADE_SUBSCRIPTION_API.replace("/C/A/", "/C/M/")

def trade_guard(payload: dict[str, Any], reporter: int | None = None, *, subscription: bool = False) -> dict[str, Any]:
    """Annotate truncation/unusable EU aggregate responses without inventing values."""
    limit = 250000 if subscription else 500  # 订阅版实测不触 500 上限；preview 触顶即截断
    count = int(payload.get("count") or len(payload.get("data") or []))
    truncated = bool(payload.get("truncated")) or count >= limit or reporter == 97
    payload = dict(payload)
    payload["truncated"] = truncated
    if reporter == 97:
        payload["pending"] = True
        payload.setdefault("degraded_reason", "EU-27 聚合口径数据残缺，改用成员国代理")
    return payload

def fetch_trade(reporter: int, partner: int, year: int, cmd_code: str = "TOTAL", flow_code: str = "M,X", retries: int = 3, api_key: str | None = None) -> dict[str, Any]:
    subscription = bool(api_key)
    params: dict[str, Any] = {"reporterCode": reporter, "period": year, "partnerCode": partner, "cmdCode": cmd_code, "flowCode": flow_code, "maxRecords": 250000 if subscription else 500}
    if subscription:
        # 订阅端点必须显式锁定聚合层：否则同一笔贸易按 customsCode(C00/C01/C04/C20)×motCode
        # 拆成 ~2000 行明细，全行求和会把对华量放大 ~8 倍（2026-09-05 真机实测并修正）。
        params.update({"partner2Code": 0, "customsCode": "C00", "motCode": 0})
    base = COMTRADE_SUBSCRIPTION_API if subscription else COMTRADE_API
    url = f"{base}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "investment-steward-core/0.1"}
    if subscription:
        headers["Ocp-Apim-Subscription-Key"] = api_key or ""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            rows = data.get("data", []) if isinstance(data, dict) else []
            export = sum(float(x.get("primaryValue") or 0) for x in rows if x.get("flowCode") == "X")
            imp = sum(float(x.get("primaryValue") or 0) for x in rows if x.get("flowCode") == "M")
            return trade_guard({"reporter": reporter, "partner": partner, "period": str(year), "export_usd": export, "import_usd": imp, "balance_usd": export-imp, "count": len(rows), "source": "UN Comtrade subscription" if subscription else "UN Comtrade preview", "data": rows}, reporter, subscription=subscription)
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误统一重试，最终由调用方降级
            last = exc
            if attempt + 1 < retries: time.sleep(5)
    raise RuntimeError(f"Comtrade 拉取失败: {last}")


def fetch_trade_monthly(reporter: int, partner: int, periods: list[str], cmd_code: str = "TOTAL", flow_code: str = "M,X", retries: int = 3, api_key: str | None = None) -> dict[str, Any]:
    """订阅端点月度贸易流（尽可能新的数据，2026-09-05 实测）：periods 为 YYYYMM 列表，
    订阅端点支持 period 逗号多值——单请求拿多月（实测 202604,202605,202606,202607 → count=6），
    未发布月份自动缺省。月度发布滞后约 1-2 个月。仅订阅可用（preview 不支持 freq=M），无 key 直接拒绝。"""
    if not api_key:
        raise RuntimeError("月度贸易数据需要 Comtrade 订阅 key（凭据库 key_id=comtrade_key）")
    params = {"reporterCode": reporter, "period": ",".join(periods), "partnerCode": partner, "partner2Code": 0, "cmdCode": cmd_code, "flowCode": flow_code, "customsCode": "C00", "motCode": 0, "maxRecords": 250000}
    url = f"{COMTRADE_SUBSCRIPTION_MONTHLY_API}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "investment-steward-core/0.1", "Ocp-Apim-Subscription-Key": api_key}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            rows = data.get("data", []) if isinstance(data, dict) else []
            groups: dict[str, dict[str, Any]] = {}
            for x in rows:
                p = str(x.get("period"))
                g = groups.setdefault(p, {"period": p, "export_usd": 0.0, "import_usd": 0.0, "count": 0})
                value = float(x.get("primaryValue") or 0)
                if x.get("flowCode") == "X":
                    g["export_usd"] += value
                elif x.get("flowCode") == "M":
                    g["import_usd"] += value
                g["count"] += 1
            series = []
            for g in sorted(groups.values(), key=lambda item: item["period"]):
                g["balance_usd"] = g["export_usd"] - g["import_usd"]
                series.append(g)
            return {"reporter": reporter, "partner": partner, "freq": "M", "series": series, "count": len(rows), "source": "UN Comtrade subscription"}
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries: time.sleep(5)
    raise RuntimeError(f"Comtrade 月度拉取失败: {last}")
