"""E4 · 通知投递通道（best-effort）。

评估产生的「新」通知按已配置通道尽力投递；任何失败 / 未配置都保持站内 pending，
绝不抛出异常去影响评估主流程。系统/桌面通知无需 secret，由前端轮询
`GET /notifications/pending` 弹出，Core 不主动外呼。

投递密钥一律经凭据库（credential store）读取，不进入 env / 明文配置。
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Protocol

from investment_steward_core.domain import Notification

NOTIFY_WEBHOOK_KEY = "notify_webhook"

# 通道注册表：投递密钥 → 通道元数据（可观测表面）。
NOTIFY_CHANNELS: dict[str, dict[str, str]] = {
    NOTIFY_WEBHOOK_KEY: {"kind": "dingtalk", "label": "钉钉 Webhook（机器人）"},
}

_HTTP_TIMEOUT = 4.0

# 显式 UA：部分服务端 WAF（Cloudflare 等）按浏览器特征封禁 `Python-urllib` 默认 UA
# （实测模型网关返回 403 + error-1010）。与 market_feed / model_client 同口径。
_USER_AGENT = "InvestmentSteward/0.1"


class _CredentialSource(Protocol):
    def get(self, key_id: str) -> str | None: ...


def _webhook_payload(notification: Notification) -> dict[str, Any]:
    text = f"[投资管家提醒] {notification.title}\n{notification.summary}"
    if notification.instrument:
        text += f"\n标的：{notification.instrument}"
    return {"msgtype": "text", "text": {"content": text}}


def _post_dingtalk(webhook_url: str, notification: Notification, timeout: float) -> bytes:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(_webhook_payload(notification)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(200)


def deliver_notification(
    notification: Notification,
    credentials: _CredentialSource,
    timeout: float = _HTTP_TIMEOUT,
) -> list[dict[str, Any]]:
    """按配置通道尽力投递，返回逐通道回执（从不抛出）。未配置 → 站内 pending。"""
    receipts: list[dict[str, Any]] = []
    webhook = credentials.get(NOTIFY_WEBHOOK_KEY)
    if not webhook:
        receipts.append(
            {
                "channel": NOTIFY_WEBHOOK_KEY,
                "kind": "dingtalk",
                "delivered": False,
                "detail": "未配置投递通道，保持站内 pending",
            }
        )
        return receipts
    try:
        _post_dingtalk(webhook, notification, timeout)
        receipts.append(
            {"channel": NOTIFY_WEBHOOK_KEY, "kind": "dingtalk", "delivered": True, "detail": "已投递钉钉 Webhook"}
        )
    except Exception as exc:  # noqa: BLE001 - 投递失败统一不中断评估
        receipts.append(
            # 保留可诊断的异常类别/短消息，但抹除可能包含 token 的完整 URL。
            {"channel": NOTIFY_WEBHOOK_KEY, "kind": "dingtalk", "delivered": False, "detail": f"投递失败：{_redact_error(exc)}"}
        )
    return receipts


def _redact_error(error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    detail = re.sub(r"https?://[^\s]+", "<redacted-url>", detail, flags=re.IGNORECASE)
    return detail[:300]


def channel_status(credentials: _CredentialSource) -> list[dict[str, Any]]:
    """通道可观测表面：定义 + 是否已配置（供前端「设置 ▸ 通知」展示）。"""
    return [
        {
            "channel": key,
            "kind": meta["kind"],
            "label": meta["label"],
            "configured": bool(credentials.get(key)),
        }
        for key, meta in NOTIFY_CHANNELS.items()
    ]
