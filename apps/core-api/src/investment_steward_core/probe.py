"""连接测试代理（G3-4）：Core 服务端探测并回 {ok, latency_ms, detail}。

- 模型方案：对 base_url 发起一次最小 HTTP GET（带超时），实测往返延迟；
- 单实例密钥：只做本机存在性与形态校验，**绝不向凭据指向的外部端点发请求**
  （电影 webhook / 行情 token 不是可安全探测的返回通道，避免误触发或泄露指纹）。
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any
from urllib.parse import urlparse


def probe_base_url(base_url: str, timeout: float = 1.5) -> dict[str, Any]:
    """探测模型服务 base_url 连通性，返回 {ok, latency_ms, detail}。"""
    url = base_url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "latency_ms": 0, "detail": "base_url 必须为 http(s) 端点"}
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            latency_ms = int((time.monotonic() - started) * 1000)
            body_preview = b""
            try:
                body_preview = response.read(200)
            except Exception:  # noqa: BLE001, S110 - 只影响预览回显
                pass
            reached = response.status < 500
            return {
                "ok": reached,
                "latency_ms": latency_ms,
                "detail": f"HTTP {response.status} 到达；返回前 {len(body_preview)} 字节。",
            }
    except Exception as exc:  # noqa: BLE001 - 网络探测失败统一按不可达处理
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"ok": False, "latency_ms": latency_ms, "detail": f"不可达：{exc}"}


def probe_credential(key_id: str, secret: str | None) -> dict[str, Any]:
    """单实例密钥的本机校验（不发外网请求）。"""
    if not secret:
        return {"ok": False, "latency_ms": 0, "detail": "该密钥尚未配置"}
    if key_id == "notify_webhook" and not str(secret).startswith(("http://", "https://")):
        return {"ok": False, "latency_ms": 0, "detail": "Webhook 必须为 http(s) 地址"}
    if key_id == "jev_api_key":
        # Jev 是**可安全探测**的端点（一次性最小请求、有明确回执），但真实调用要花钱且需要
        # 端点/模型参数，因此不在本按钮里做——指路到「设置 → Jev 决策模型」的「测连通性」。
        return {
            "ok": True,
            "latency_ms": 0,
            "detail": "本机凭据已存在。真实连通性请到「设置 → Jev 决策模型」点「测连通性」（会发起一次最小调用）。",
        }
    return {
        "ok": True,
        "latency_ms": 0,
        "detail": "本机凭据已校验（未发起外网请求，避免误触发/指纹泄漏）。",
    }