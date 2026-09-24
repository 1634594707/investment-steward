"""对 Core 的薄 HTTP 客户端：worker 侧所有出网调用收敛于此，便于审计与降级。

仅用标准库 urllib，不引入新依赖。会话鉴权头与 web-shell/desktop-host 一致：
`X-Core-Session-Token`。网络/非 2xx 一律抛 CoreClientError，由调度循环捕获并记为工具调用失败。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class CoreClientError(Exception):
    """Core 不可达或返回非 2xx。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CoreClient:
    def __init__(self, base_url: str, session_token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.session_token = session_token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "X-Core-Session-Token": self.session_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CoreClientError(f"{path} -> HTTP {exc.code}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise CoreClientError(f"{path} -> {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CoreClientError(f"{path} -> 非 JSON 响应") from exc

    def generate_today_brief(self) -> dict[str, Any]:
        return self._request("POST", "/brief/today/generate")

    def patrol_evidence(self) -> dict[str, Any]:
        return self._request("POST", "/evidence/freshness/patrol")

    def evaluate_notifications(self) -> list[dict[str, Any]]:
        data = self._request("POST", "/notifications/evaluate")
        return data if isinstance(data, list) else []