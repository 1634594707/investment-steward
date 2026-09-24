from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Any

MAX_REQUEST_BYTES = 256_000
MAX_RESPONSE_BYTES = 1_000_000
ALLOWED_METHODS = {
    "runner.health",
    "runner.context",
    "runner.network",
    "golden.evidence",
    "golden.learning",
}

# G3-5 零密钥不变式：runner 进程环境禁止出现任何疑似凭据变量。Core 只注入
# 白名单网络域（STEWARD_NETWORK_ALLOWLIST）和运行必需的非敏感键；此门控在
# runner 自身再兜底一道，防止第三方代码拿到密钥。
_SECRET_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "APIKEY",
    "API_KEY",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "CREDENTIAL",
)


def secret_env_vars() -> set[str]:
    """扫描当前进程环境，返回命中疑似密钥标记的变量名（成立于名字，不改动任何值）。"""
    return {
        name
        for name in os.environ
        if any(marker in name.upper() for marker in _SECRET_KEY_MARKERS)
    }


def _env_allowlist() -> list[str]:
    raw = os.environ.get("STEWARD_NETWORK_ALLOWLIST", "[]")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(host) for host in value] if isinstance(value, list) else []


def _configured_capabilities() -> list[str] | None:
    """读取 Host 注入的安装授权；未配置时返回 None 以兼容独立协议测试。"""
    raw = os.environ.get("STEWARD_GRANTED_CAPABILITIES")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return sorted({str(item) for item in value}) if isinstance(value, list) else []


def assert_host_allowed(host: str) -> None:
    """network_allowlist 出网门控：只允许白名单域及子域（由 Core 经 env 注入）。

    精确或子域匹配（`api.eastmoney.com` 命中白名单 `eastmoney.com`）均放行；
    白名单为空时拒绝一切出网（最保守默认）。
    """
    allowed = _env_allowlist()
    if any(host == base or host.endswith(f".{base}") for base in allowed):
        return
    raise ValueError(f"host not allowed by STEWARD_NETWORK_ALLOWLIST: {host}")


def http_get(url: str, timeout: float = 10.0) -> str:
    """受 network_allowlist 门控的出网读取，并校验最终重定向、大小和类型。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("url must use http/https and contain no credentials")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    host = parsed.hostname.lower()
    assert_host_allowed(host)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        final_url = response.geturl() if hasattr(response, "geturl") else url
        final = urlparse(final_url)
        if final.scheme not in {"http", "https"} or not final.hostname:
            raise ValueError("redirected URL is not http/https")
        assert_host_allowed(final.hostname.lower())
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if content_type and not (content_type.startswith("text/") or content_type in {"application/json", "application/javascript"}):
            raise ValueError(f"unsupported response content type: {content_type}")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return body.decode("utf-8")

# 能力代理：context 中每个资源属一个能力，Runner 只把「插件已获授权能力」对应的资源交给插件。
# 插件不能靠在 params 里塞 resource_type 骗过授权——门控在 Runner 侧，且只读被注入的 context，不直连 Core。
_CAPABILITY_RESOURCES: dict[str, set[str]] = {
    "read_market_data": {"market_data"},
    "read_portfolio": {"portfolio_holdings"},
    "read_thesis": {"thesis"},
}


def _authorized_resource_types(granted: list[str]) -> set[str]:
    allowed: set[str] = set()
    for capability in granted:
        allowed |= _CAPABILITY_RESOURCES.get(capability, set())
    return allowed


def _context(
    params: dict[str, Any],
    granted: list[str],
) -> dict[str, Any]:
    requested = params.get("context")
    if not isinstance(requested, dict):
        raise TypeError("context must be an object")
    allowed = _authorized_resource_types(granted)
    if not allowed:
        # 未授予任何读取能力：即使请求了 context，也不返回任何资源。
        return {"granted_resources": [], "data": {}}
    dicts = {k: v for k, v in requested.items() if v is not None}
    keys = set(dicts)
    denied = keys - allowed
    if denied:
        raise ValueError(f"context includes unauthorized resources: {sorted(denied)}")
    return {"granted_resources": sorted(allowed), "data": dicts}


def _evidence(params: dict[str, Any]) -> dict[str, Any]:
    summary = str(params.get("summary", ""))[:10_000]
    if not summary:
        raise ValueError("summary is required")
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    return {
        "kind": "evidence",
        "evidence_type": "analysis",
        "summary": summary,
        "content_hash": digest,
        "relation": "unknown",
        "freshness": "fixture",
        "source_name": "golden-evidence-runner",
        "license_status": "test-fixture",
    }


def _learning(params: dict[str, Any]) -> dict[str, Any]:
    title = str(params.get("title", "")).strip()
    if not title:
        raise ValueError("title is required")
    return {
        "kind": "learning_unit",
        "title": title,
        "objective": str(params.get("objective", "用自己的话解释一个投资概念"))[:2000],
        "activity_type": "reflection",
    }


def _network(params: dict[str, Any]) -> dict[str, Any]:
    url = str(params.get("url", ""))
    from urllib.parse import urlparse

    host = urlparse(url).netloc.split(":")[0]
    try:
        assert_host_allowed(host)
    except ValueError as exc:
        return {"allowed": False, "host": host, "reason": str(exc)}
    return {"allowed": True, "host": host, "allowlist": _env_allowlist()}


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    if method not in ALLOWED_METHODS:
        raise ValueError("method is not allowed")
    if method == "runner.health":
        leaked = sorted(secret_env_vars())
        return {
            "status": "ready",
            "runner_version": "0.1.0",
            "allowed_methods": sorted(ALLOWED_METHODS),
            "env_credentials": len(leaked),
            "secret_env_vars": leaked,
            "network_allowlist": _env_allowlist(),
        }
    # 除 health 外一切方法：环境若存在密钥泄漏一律拒绝执行（G3-5 兜底）。
    leaked = sorted(secret_env_vars())
    if leaked:
        raise ValueError(f"runner env leaks credentials: {leaked}")
    params = request.get("params")
    if not isinstance(params, dict):
        raise TypeError("params must be an object")
    if method == "runner.context":
        configured = _configured_capabilities()
        claimed = [str(c) for c in params.get("granted_capabilities", []) or []]
        if configured is not None:
            unauthorized = set(claimed) - set(configured)
            if unauthorized:
                raise ValueError(f"context includes capabilities not granted by Host: {sorted(unauthorized)}")
            return _context(params, configured)
        return _context(params, claimed)
    if method == "runner.network":
        return _network(params)
    if method == "golden.evidence":
        return _evidence(params)
    return _learning(params)


def handle_line(line: str) -> str:
    if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
        return json.dumps({"jsonrpc": "2.0", "error": {"code": -32001, "message": "request too large"}})
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        result = dispatch(request)
        response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
    except (ValueError, TypeError) as exc:
        response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": str(exc)}}
    serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32002, "message": "response too large"}})
    return serialized
