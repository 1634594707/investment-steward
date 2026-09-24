"""v2.2 模型方案草稿态探测测试（2026-09-10 用户要求）。

需求（用户原话）：「模型需要可以设置拉取模型，然后测连通性，可以看延迟」——
即在**新增/编辑方案弹窗内**，不保存配置就能：
1. 拉取服务商 `/models` 列表（模型名不用手打）；
2. 试调一次看真实延迟。

设计红线（本文件逐条守卫）：
- 两个端点都是**只读探测**：不落库模型方案、不写凭据库——「点一下测试」不得产生配置副作用；
- 密钥可以是**尚未保存的明文**（先试后存），也可以留空（本地端点）；
- 端点不可用时**如实报错**，绝不猜模型名、绝不返回假延迟。

一律 monkeypatch 模型调用，**绝不真实联网**。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import _file_client  # noqa: F401

from investment_steward_core import model_client as mc


# ---------------------------------------------------------------------------
# 纯函数：明文密钥判定与解析
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ref, expected",
    [
        ("sk-abcdef", True),
        ("sk-" + "x" * 30, True),
        ("a" * 24, True),                    # 24 位无空白连续串
        ("a" * 23, False),                   # 23 位不算
        ("model_api_key", False),            # key_id 13 位，不得误判
        ("model_api_key_2", False),          # key_id 15 位
        ("bjPc", False),                     # 凭据尾号
        ("", False),
        ("has space and is quite long here", False),
    ],
)
def test_looks_like_plaintext_key(ref, expected):
    assert mc.looks_like_plaintext_key(ref) is expected


def test_resolve_probe_api_key_prefers_plaintext_over_store(tmp_path, monkeypatch):
    """明文密钥原样返回——即使凭据库里没有这条记录（先试后存的核心）。"""
    plaintext = "sk-" + "y" * 28
    assert mc.resolve_probe_api_key(None, plaintext) == plaintext


def test_resolve_probe_api_key_falls_back_to_store(tmp_path, monkeypatch):
    """key_id 走凭据库解析（复用既有 resolve_credential 的 key_id/尾号兜底）。"""
    monkeypatch.setattr(mc, "resolve_credential", lambda store, ref: f"resolved:{ref}")
    assert mc.resolve_probe_api_key(object(), "model_api_key") == "resolved:model_api_key"
    assert mc.resolve_probe_api_key(object(), "") == ""


# ---------------------------------------------------------------------------
# 端点：测连通性（带延迟）
# ---------------------------------------------------------------------------

def test_probe_returns_latency_without_persisting(tmp_path, monkeypatch):
    """★ 只读：探测成功返回延迟，且不产生任何模型方案/凭据副作用。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    before_profiles = test_client.get("/model-profiles", headers=headers).json()
    before_creds = test_client.get("/credentials", headers=headers).json()

    captured: dict[str, object] = {}

    def fake_probe(base_url, model, api_key, *, timeout):
        captured.update({"base_url": base_url, "model": model, "api_key": api_key, "timeout": timeout})
        return mc.ModelReply(content="ok", model=model, provider=base_url, latency_ms=734)

    monkeypatch.setattr(mc, "probe_completion", fake_probe)

    body = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
              "credential_ref": "sk-" + "z" * 28},
        headers=headers,
    ).json()

    assert body["ok"] is True
    assert body["latency_ms"] == 734
    assert "734" in body["detail"] or "应答" in body["detail"]
    # 明文密钥被透传给探测函数，且库中未新增任何记录
    assert captured["api_key"] == "sk-" + "z" * 28
    assert test_client.get("/model-profiles", headers=headers).json() == before_profiles
    assert test_client.get("/credentials", headers=headers).json() == before_creds


def test_probe_requires_model_name(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    body = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://api.deepseek.com", "model": "", "credential_ref": ""},
        headers=headers,
    ).json()
    assert body["ok"] is False
    assert "模型名" in body["detail"]


def test_probe_reports_unknown_credential_reference(tmp_path, monkeypatch):
    """引用既不是明文也不是库中条目 → 明确提示三种填法，不静默空跑。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    body = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
              "credential_ref": "not_a_real_key_id"},
        headers=headers,
    ).json()
    assert body["ok"] is False
    assert "不存在" in body["detail"]


def test_probe_surfaces_provider_error(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise mc.ModelUnavailable("服务端返回 403（认证被拒绝）")

    monkeypatch.setattr(mc, "probe_completion", boom)
    body = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
              "credential_ref": "sk-" + "q" * 28},
        headers=headers,
    ).json()
    assert body["ok"] is False
    assert "403" in body["detail"]


# ---------------------------------------------------------------------------
# 端点：拉取模型列表
# ---------------------------------------------------------------------------

def test_discover_models_returns_list_without_persisting(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    before_profiles = test_client.get("/model-profiles", headers=headers).json()

    monkeypatch.setattr(
        mc, "list_provider_models",
        lambda base_url, api_key, timeout: ["deepseek-chat", "deepseek-reasoner"],
    )
    body = test_client.post(
        "/model-profiles/discover-models",
        json={"base_url": "https://api.deepseek.com", "credential_ref": "sk-" + "w" * 28},
        headers=headers,
    ).json()

    assert body["ok"] is True
    assert body["models"] == ["deepseek-chat", "deepseek-reasoner"]
    assert test_client.get("/model-profiles", headers=headers).json() == before_profiles


def test_discover_models_reports_endpoint_without_models_api(tmp_path, monkeypatch):
    """网关只开放 chat/completions 时如实报错，不猜模型名。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise mc.ModelUnavailable("该端点未提供 /models 接口（部分网关只开放 chat/completions），请手动填写模型名")

    monkeypatch.setattr(mc, "list_provider_models", boom)
    body = test_client.post(
        "/model-profiles/discover-models",
        json={"base_url": "https://svc.example/v1", "credential_ref": "sk-" + "e" * 28},
        headers=headers,
    ).json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "/models" in body["detail"]


def test_probe_routes_are_not_shadowed_by_profile_id_route(tmp_path, monkeypatch):
    """字面量路由不得被 `/{profile_id}` 参数化路由抢先匹配（返回结构是探测契约）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    body = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://api.deepseek.com", "model": "x", "credential_ref": ""},
        headers=headers,
    ).json()
    assert set(body.keys()) == {"ok", "models", "latency_ms", "detail"}


# ---------------------------------------------------------------------------
# 错误路径绝不 500（2026-09-10 真机回归）
#
# 真机现象：探测到不通的端点时，`probe_completion` 让底层 HTTPError/URLError
# 直接冒到 ASGI 层 → 前端拿到 500 + `{"detail":"Internal Server Error"}`，
# 既看不到原因也不能操作。回归守卫：任何底层异常都必须转成 ok=false + 中文诊断。
# ---------------------------------------------------------------------------

def _http_error(code: int):
    return urllib.error.HTTPError("https://x/v1/chat/completions", code, "boom", None, None)


@pytest.mark.parametrize(
    "code, keyword",
    [
        (401, "认证被拒绝"),
        (403, "认证被拒绝"),
        (404, "/v1"),
        (429, "限流"),
        (502, "上游网关"),
        (503, "上游网关"),
    ],
)
def test_probe_completion_maps_http_status_code(code, keyword, monkeypatch):
    """HTTP 状态码 → 可操作的中文诊断（不回传服务端 body）。"""
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: (_ for _ in ()).throw(_http_error(code)))
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.probe_completion("https://x/v1", "m", "sk-" + "a" * 28, timeout=30)
    assert str(code) in str(excinfo.value)
    assert keyword in str(excinfo.value)


def test_probe_completion_maps_connection_refused(monkeypatch):
    """连接被拒 → 明确提示检查网络/base_url/代理，而不是 WinError 原文。"""
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))),
    )
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.probe_completion("http://127.0.0.1:1/v1", "m", "", timeout=30)
    message = str(excinfo.value)
    assert "无法连接端点" in message
    assert "代理设置" in message


def test_probe_completion_maps_timeout(monkeypatch):
    """超时必须说说清楚，不能只说「失败」。"""
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.probe_completion("https://x/v1", "m", "", timeout=30)
    assert "超时" in str(excinfo.value)


def test_probe_endpoint_never_returns_5xx_on_unexpected_exception(tmp_path, monkeypatch):
    """兜底：探针内部任何未预期异常都不允许冒成 500。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mc, "probe_completion", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )
    response = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://x/v1", "model": "m", "credential_ref": "sk-" + "b" * 28},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["latency_ms"] == 0
    assert body["detail"]


def test_discover_models_never_returns_5xx_on_unexpected_exception(tmp_path, monkeypatch):
    """拉取模型同样不允许 500。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mc, "list_provider_models", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )
    response = test_client.post(
        "/model-profiles/discover-models",
        json={"base_url": "https://x/v1", "credential_ref": "sk-" + "c" * 28},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["models"] == []


def test_list_provider_models_wraps_connection_error(monkeypatch):
    """拉取模型遇到连不通时抛 ModelUnavailable（而不是裸 URLError）。"""
    def boom(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.list_provider_models("http://127.0.0.1:1/v1", "", 30)
    assert "无法连接端点" in str(excinfo.value)


def test_list_provider_models_rejects_non_json_body(monkeypatch):
    """非 OpenAI 兼容端点会返回 HTML → 必须报「不是合法 JSON」而不是 JSONDecodeError。"""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"<html>nope</html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _Resp())
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.list_provider_models("https://x/v1", "", 30)
    assert "JSON" in str(excinfo.value)


def test_probe_timeout_bounds_are_enforced_by_request_model(tmp_path, monkeypatch):
    """请求模型限制 30–1800：小于 30 由 FastAPI 拦下（前端 parseTimeoutSecs 已同步同口径）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.post(
        "/model-profiles/probe",
        json={"base_url": "https://x/v1", "model": "m", "timeout_secs": 5},
        headers=headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# User-Agent（2026-09-10 真机缺陷）
#
# 真机现象：CC Switch（Electron，自带 Chrome UA）能从自建网关拉到模型，
# 本程序拉不到 → 抓包定位为 Cloudflare **error-1010**：CF 托管规则按
# 「浏览器特征」封禁 `Python-urllib/x.y` 默认 UA，返回 403（与密钥无关）。
# 实测同一端点同一密钥：Python-urllib → 403；任意自有 UA → 200。
# 回归守卫：模型出网请求必须显式声明自有 UA，且 403 要能区分「WAF 拦截」与「认证失败」。
# ---------------------------------------------------------------------------

_CF_403_BODY = json.dumps({
    "type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/",
    "title": "Error 1010",
    "detail": "The owner of this website has banned your access based on your browser's signature.",
})


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._raw


def _capture_ua(monkeypatch, payload: dict) -> dict:
    """替换 urlopen，记录请求头后返回给定 JSON 响应。"""
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.headers)
        captured["url"] = request.full_url
        return _FakeResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def test_probe_completion_declares_user_agent(monkeypatch):
    """测连通性（POST /chat/completions）必须带自有 UA，不能是 Python-urllib。"""
    captured = _capture_ua(monkeypatch, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    mc.probe_completion("https://svc.example/v1", "m", "sk-" + "d" * 28, timeout=30)
    ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent") or ""
    assert ua == mc.MODEL_USER_AGENT
    assert "urllib" not in ua.lower()


def test_list_provider_models_declares_user_agent(monkeypatch):
    """拉取模型（GET /models）同样必须带自有 UA。"""
    captured = _capture_ua(monkeypatch, {"data": [{"id": "m1"}]})
    assert mc.list_provider_models("https://svc.example/v1", "sk-" + "e" * 28, 30) == ["m1"]
    ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent") or ""
    assert ua == mc.MODEL_USER_AGENT
    assert "urllib" not in ua.lower()


def test_model_user_agent_matches_market_feed_convention():
    """与既有适配器（market_feed）同口径，避免出现第二套自称。"""
    from investment_steward_core import market_feed

    assert mc.MODEL_USER_AGENT == market_feed._USER_AGENT


@pytest.mark.parametrize("use_list", [False, True])
def test_waf_403_is_reported_as_waf_not_auth(use_list, monkeypatch):
    """CF 1010 的 403 必须说「被 WAF 拦截」，不能说「认证被拒绝」——两者处理方向完全不同。"""
    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(_CF_403_BODY.encode()))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        if use_list:
            mc.list_provider_models("https://svc.example/v1", "sk-" + "f" * 28, 30)
        else:
            mc.probe_completion("https://svc.example/v1", "m", "sk-" + "f" * 28, timeout=30)
    message = str(excinfo.value)
    assert "WAF" in message
    assert "1010" in message
    assert "认证被拒绝" not in message


def test_plain_403_still_reports_auth_failure(monkeypatch):
    """没有 CF 特征体的 403 仍按认证失败报（不要把所有 403 都推给 WAF）。"""
    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, io.BytesIO(b'{"error":{"message":"invalid key"}}'))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.list_provider_models("https://api.deepseek.com/v1", "sk-" + "g" * 28, 30)
    assert "认证被拒绝" in str(excinfo.value)
    assert "WAF" not in str(excinfo.value)


def test_is_waf_block_only_matches_403(monkeypatch):
    """500 里出现 cloudflare 字样不该被当成 WAF 封禁（避免误判）。"""
    assert mc._is_waf_block(403, _CF_403_BODY) is True
    assert mc._is_waf_block(500, _CF_403_BODY) is False
    assert mc._is_waf_block(403, "") is False


# ---------------------------------------------------------------------------
# call_active_model 超时文案指路（2026-09-10 真机回归）
#
# 真机现象：协同流水线初稿（deepseek-v4-pro 长输出）触顶方案超时 120s，用户看到的
# 是泛化的「检查网络、端点和协议配置」，无从下手；且前端失败后无限自动重试锁死工作台。
# 后端守卫：超时必须单列——报出方案超时秒数 + 指路「调大方案超时」。
# ---------------------------------------------------------------------------

def _active_profile(timeout_secs: int = 120) -> "object":
    from uuid import uuid4

    from investment_steward_core.domain import ModelProfile, ModelProfileStatus

    return ModelProfile(
        profile_id=uuid4(),
        name="超时测试方案",
        base_url="https://x/v1",
        model="deepseek-v4-pro",
        credential_ref="cred_key",
        status=ModelProfileStatus.ACTIVE,
        timeout_secs=timeout_secs,
    )


def test_call_active_model_timeout_names_seconds_and_fix(monkeypatch):
    """超时 → 文案含方案超时秒数并指路「调大方案超时」，不是泛化的检查网络。"""
    monkeypatch.setattr(mc, "resolve_credential", lambda store, ref: "sk-" + "a" * 28)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.call_active_model(_active_profile(timeout_secs=120), object(), [{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "超时" in message
    assert "120" in message
    assert "调大" in message


def test_call_active_model_non_timeout_keeps_generic_message(monkeypatch):
    """非超时异常维持通用文案（不把底层异常原文回传 UI）。"""
    monkeypatch.setattr(mc, "resolve_credential", lambda store, ref: "sk-" + "a" * 28)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.call_active_model(_active_profile(), object(), [{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "检查网络、端点和协议配置" in message
    assert "secret detail" not in message


def test_call_active_model_wrapped_timeout_in_urlerror(monkeypatch):
    """urllib 把读 body 阶段超时包进 URLError(reason=TimeoutError) → 同样必须识别为超时。"""
    monkeypatch.setattr(mc, "resolve_credential", lambda store, ref: "sk-" + "a" * 28)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError("timed out"))),
    )
    with pytest.raises(mc.ModelUnavailable) as excinfo:
        mc.call_active_model(_active_profile(timeout_secs=60), object(), [{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "超时" in message
    assert "60" in message
