"""模型调用器（阶段 A）：读「使用中」的模型方案 → 解析凭据 → OpenAI 兼容 chat/completions。

- 纯 stdlib（urllib.request），零新增依赖，对齐 `market_feed.py` / `financial_evidence.py` 风格。
- `model_access` 能力出网一律经当前「使用中」方案代理（G3-3 既定原则）。
- 密钥原文永不落日志、不进审计 payload、不进插件进程环境、不回传前端；
  失败时只暴露无密钥误差类，不暴露密钥/请求正文。
- 无激活方案 / 凭据缺失 / 任何网络或解析异常 → 抛 `ModelUnavailable`，由调用方降级（阶段 B3）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from investment_steward_core.domain import ModelProfile

_MODEL_REQUEST_TIMEOUT = 120.0  # 推理模型（deepseek-v4-flash 等）思考耗时明显更长，30s 不够
# 公开别名：草稿态探测（弹窗内「测连通性 / 拉取模型」）需要内置默认超时，私有名不跨模块引用。
MODEL_REQUEST_TIMEOUT = _MODEL_REQUEST_TIMEOUT

# ---------------------------------------------------------------------------
# User-Agent（真机缺陷修复 2026-09-10）
#
# urllib 默认 UA 是 `Python-urllib/3.x`。自建网关（new-api / One API）常挂在
# Cloudflare 之后，CF 托管规则对该 UA 有封禁特征 → 网关返回 **403 + error-1010**
# （「banned your access based on your browser's signature」），与密钥完全无关。
# 实测同一端点同一密钥：`Python-urllib` → 403；任意自有 UA → 200。
# 因此所有模型出网请求必须显式声明 UA。
# ---------------------------------------------------------------------------
MODEL_USER_AGENT = "InvestmentSteward/0.1"

# CF 封禁特征（只在 403 判定里用，命中即提示 WAF 而非「认证被拒绝」）
_WAF_BLOCK_MARKERS = ("error-1010", "cloudflare", "cf-error", '"code":1010', "code: 1010")


def _base_headers() -> dict[str, str]:
    """模型出网请求的公共请求头（显式 UA，绕开 CF 对 Python-urllib 的封禁）。"""
    return {"User-Agent": MODEL_USER_AGENT, "Accept": "application/json"}


def _is_waf_block(status_code: int, body: str) -> bool:
    """403 是否来自 WAF 特征封禁（而非密钥无效）——两者对用户是两种截然不同的处理方向。"""
    if status_code != 403:
        return False
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _WAF_BLOCK_MARKERS)


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    """安全读取错误响应体（仅用于特征判定，绝不原文回传前端）。"""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - 读不到就当没有
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")[:2000]
    return str(raw)[:2000]


_WAF_BLOCK_HINT = (
    "被网关的 WAF 按客户端特征拦截（Cloudflare 1010），不是密钥问题"
    "——请确认服务商是否要求特定 User-Agent／IP 白名单"
)


class ModelUnavailable(Exception):
    """模型调用不可用：未配置激活方案 / 凭据缺失 / 超时 / 网络 / 非 2xx / 响应畸形。"""


class ReasoningBudgetExhausted(ModelUnavailable):
    """推理模型的思考耗尽了 max_tokens 预算（content 为空、finish_reason=length）。

    单独建类以便 call_active_model 捕获后用「关闭思考」自动重试一次。
    """


@dataclass(frozen=True)
class ModelReply:
    content: str
    model: str
    provider: str
    latency_ms: int
    # F01（桌面端升级路线图 2026-09-18）：用量与调用元数据——payload["usage"] 缺失记 null
    #（不按字数反推）；retried 标记 thinking-disabled 重试（该次调用实际发生了两次计费请求）。
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    retried: bool = False
    purpose: str | None = None


def resolve_credential(store: Any, credential_ref: str) -> str:
    """从凭据库取明文密钥；缺失返回空串，由调用方判定不可用。

    解析顺序：① 按 key_id 精确取；② 尾号兜底——引用填了某条凭据的 last4
    （如 HGDI）时按尾号匹配唯一凭据（大小写不敏感）。单人本机产品，允许
    用户凭直觉填尾号，而不是逼他记住 key_id。
    """
    ref = credential_ref.strip()
    candidates: list[str] = [ref]
    try:
        records = store.list_records()
    except Exception:  # noqa: BLE001 - 凭据层任何异常都按不可用处理
        records = []
    for record in records:
        tail = (getattr(record, "last4", "") or "").strip().upper()
        if tail and tail == ref.upper() and record.key_id != ref and record.key_id not in candidates:
            candidates.append(record.key_id)
    for key_id in candidates:
        try:
            secret = store.get(key_id)
        except Exception:  # noqa: BLE001, S112 - 凭据取数尽力而为，任一失败跳过继续
            continue
        if isinstance(secret, str) and secret:
            return secret
    return ""


def looks_like_plaintext_key(ref: str) -> bool:
    """是否为「直接粘贴的明文密钥」（sk- 前缀，或 ≥24 位无空白连续串）。

    与前端 `SettingsPage.looksLikePlaintextKey` 同口径：`model_api_key` 这类 key_id（13 位）
    与凭据尾号（4 位）不会被误判为明文密钥。
    """
    text = (ref or "").strip()
    if not text:
        return False
    if text.startswith("sk-"):
        return True
    return len(text) >= 24 and not any(char.isspace() for char in text)


def resolve_probe_api_key(store: Any, credential_ref: str) -> str:
    """草稿态探测的密钥解析：明文密钥直接用（尚未保存也要能先测），其余走凭据库 key_id/尾号。"""
    ref = (credential_ref or "").strip()
    if not ref:
        return ""
    if looks_like_plaintext_key(ref):
        return ref
    return resolve_credential(store, ref)


def _chat_completion_payload(
    messages: list[dict[str, str]], model: str, timeout: float, *, thinking_disabled: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        # 推理模型（如 deepseek-v4-flash）先烧 token 生成 reasoning_content 再出正文；
        # 预算太小（初版 1200 → 8000 仍被长链路思考耗尽）会导致 content 为空、finish_reason=length。
        # 16000 实测（含证据输入的研报提示）finish=stop、正文完整。
        "max_tokens": 16000,
        "temperature": 0.2,
        "stream": False,
    }
    if thinking_disabled:
        # DeepSeek v4 混合推理开关（实测生效：reasoning_tokens=None，耗时大幅下降）。
        payload["thinking"] = {"type": "disabled"}
    return payload


def _post_json(
    base_url: str,
    json_payload: dict[str, Any],
    api_key: str | None,
    timeout: float,
) -> dict[str, Any]:
    """OpenAI 兼容 POST {base_url}/chat/completions，返回解析后的 JSON dict。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = _base_headers()
    headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(json_payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            if _is_waf_block(exc.code, _read_error_body(exc)):
                raise ModelUnavailable(_WAF_BLOCK_HINT) from exc
            host = urllib.parse.urlparse(url).hostname or ""
            gateway_note = ""
            if api_key and api_key.startswith("sk_") and "deepseek" in host:
                gateway_note = "；检测到密钥为 sk_ 开头（One API/new-api 网关 key 格式），与 DeepSeek 官方端点不匹配——请改 base_url 为网关地址，或换成官方 sk- 密钥"
            raise ModelUnavailable(
                f"服务端返回 {exc.code}（认证被拒绝）：请确认密钥与 base_url 匹配（官方 key 与网关 key 不可混用）、密钥未失效且未欠费{gateway_note}"
            ) from exc
        raise
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ModelUnavailable("模型返回结构异常（非 JSON 对象）")
    return parsed


def _extract_usage(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """F01：解析 payload["usage"] 的 token 三项；缺失或形状异常一律 None（不按字数反推）。"""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None, None

    def _int(value: Any) -> int | None:
        return int(value) if isinstance(value, int) else None

    return _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens")), _int(usage.get("total_tokens"))


# F01：模型调用记录落表钩子——create_app 注入（写 model_calls 表），model_client 保持无 DB 依赖。
# 记录失败绝不影响调用本身（钩子内部已 try/except）。
_model_call_recorder: Any = None


def set_model_call_recorder(recorder: Any) -> None:
    """注入用量记录回调：`recorder(record_dict)`，record 字段见 _emit_model_call_record。传 None 撤销。"""
    global _model_call_recorder
    _model_call_recorder = recorder


def _emit_model_call_record(record: dict[str, Any]) -> None:
    if _model_call_recorder is None:
        return
    try:
        _model_call_recorder(record)
    except Exception:
        pass


def emit_model_call_record(record: dict[str, Any]) -> None:
    """公开出口：供其他协议的出网客户端（如 `jev_client`）复用同一 recorder 落 `model_calls`。

    为什么需要它：`set_model_call_recorder` 只在 `create_app` 注入一次（`api/app.py:1394`），
    而 Jev 走的是**另一套协议**（`/systemone`，非 chat/completions），不能复用 `call_active_model`。
    若让 jev_client 自己再开一个钩子，就会多出一个必须同步注入的点——漏注入即静默丢用量。
    因此这里把私有发射器公开出来：**同一个钩子、同一张表、同一个注入点**。
    """
    _emit_model_call_record(record)


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelUnavailable("模型返回缺少 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelUnavailable("模型返回 choices 项结构异常")
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        finish = first.get("finish_reason")
        if finish == "length":
            raise ReasoningBudgetExhausted(
                "模型思考耗尽了输出预算（finish_reason=length），正文为空"
            )
        reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
        if isinstance(reasoning, str) and reasoning.strip():
            # DeepSeek 推理模型偶发把最终答案整个写进 reasoning_content（content 空、finish=stop）。
            # finish=stop 表示模型认为已答完，此时 reasoning_content 即答案，直接采用。
            return reasoning
        # 兜底诊断：仍为空则带回原始结构定位（仅模型输出，无密钥）。
        raise ModelUnavailable(
            f"模型返回空内容（finish_reason={finish}）raw={json.dumps(first, ensure_ascii=False)[:600]}"
        )
    return content


def call_active_model(
    profile: ModelProfile,
    credential_store: Any,
    messages: list[dict[str, str]],
    *,
    timeout: float | None = None,
    purpose: str | None = None,
) -> ModelReply:
    """用指定模型方案发起一次 chat/completions 调用。

    `credential_store` 只要求有 `.get(key_id) -> str | None`（与 `credential_store.CredentialStore` 对齐）。
    密钥只在本函数内使用于 Authorization 头，绝不外泄。
    超时优先级：显式传入 > 方案配置 timeout_secs > 内置默认 _MODEL_REQUEST_TIMEOUT。
    F01（桌面端升级路线图 2026-09-18）：解析 payload["usage"]（缺失记 null），每次调用
    （成功或失败）经模块级 recorder 落 model_calls 表；retried=True 表示发生过
    thinking-disabled 重试（两次计费请求，token 三项为两次之和）。
    """
    effective_timeout = float(timeout or getattr(profile, "timeout_secs", None) or _MODEL_REQUEST_TIMEOUT)
    api_key = resolve_credential(credential_store, profile.credential_ref)
    started = time.monotonic()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    retried = False

    def _accumulate_usage(payload: dict[str, Any]) -> None:
        nonlocal prompt_tokens, completion_tokens, total_tokens
        attempt_prompt, attempt_completion, attempt_total = _extract_usage(payload)
        # 两次计费请求（重试）token 相加；任一侧缺失时以另一侧为准（缺失侧保持 None 的语义
        # 由 total/prompt 的 None 传播体现——缺失记 null，不猜 0）。
        if attempt_prompt is not None:
            prompt_tokens = attempt_prompt if prompt_tokens is None else prompt_tokens + attempt_prompt
        if attempt_completion is not None:
            completion_tokens = attempt_completion if completion_tokens is None else completion_tokens + attempt_completion
        if attempt_total is not None:
            total_tokens = attempt_total if total_tokens is None else total_tokens + attempt_total

    def _record(outcome: str) -> None:
        _emit_model_call_record({
            "profile_id": getattr(profile, "profile_id", None),
            "model": profile.model,
            "purpose": purpose,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "retried": retried,
            "outcome": outcome,
        })

    try:
        if not api_key:
            _record("error")
            raise ModelUnavailable(f"模型方案 {profile.name} 关联的凭据 {profile.credential_ref} 未配置")
        try:
            payload = _post_json(profile.base_url, _chat_completion_payload(messages, profile.model, effective_timeout), api_key, effective_timeout)
            _accumulate_usage(payload)
            content = _extract_content(payload)
        except ReasoningBudgetExhausted:
            # 推理模型思考把预算烧光（content 为空）→ 关闭思考重试一次，保证出结果。
            retried = True
            retry_payload = _post_json(profile.base_url, _chat_completion_payload(messages, profile.model, effective_timeout, thinking_disabled=True), api_key, effective_timeout)
            _accumulate_usage(retry_payload)
            content = _extract_content(retry_payload)
        latency_ms = int((time.monotonic() - started) * 1000)
        _record("ok")
        return ModelReply(
            content=content,
            model=profile.model,
            provider=profile.name,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            retried=retried,
            purpose=purpose,
        )
    except ModelUnavailable as exc:
        # 首次请求本身的结构/内容错误（非重试路径）：在重试前也会走到这里——如实记 error。
        if not retried:
            _record("error")
        else:
            _record("error")
        exc.latency_ms = int((time.monotonic() - started) * 1000)  # type: ignore[attr-defined]
        exc.retried = retried  # type: ignore[attr-defined]
        raise
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        _record("timeout" if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(exc, TimeoutError) else "error")
        # 不将 endpoint、请求正文或可能包含敏感信息的底层异常原文回传给 UI。
        # 超时单列（2026-09-10 真机：协同流水线初稿阶段长输出触顶 120s，用户看到的却是
        # 泛化的「检查网络」文案，无从下手）——指路到方案超时设置。
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise ModelUnavailable(
                f"模型调用超时（方案超时 {int(effective_timeout)} 秒已用尽，实际等待 {latency_ms / 1000:.0f}s）。"
                "长输出阶段（初稿/修订/批量研报）容易触顶：可在「设置 → 模型方案」调大该方案的超时时间后重试。"
            ) from exc
        raise ModelUnavailable(f"模型调用失败（{latency_ms}ms），请检查网络、端点和协议配置") from exc


# ---------------------------------------------------------------------------
# 草稿态探测（2026-09-10 用户要求）：「拉取模型」+ 弹窗内「测连通性看延迟」。
# 两者都只读：端点/模型/密钥由调用方直接给出（密钥可以是尚未保存的明文），
# 不读模型方案表、不写凭据库——避免「点一下测试」就产生配置副作用。
# ---------------------------------------------------------------------------

_PROBE_SYSTEM = "你是投资管家用于连通性测试的小助手，只回复一个词：ok。"
_PROBE_USER = "连通性测试"


def list_provider_models(base_url: str, api_key: str, timeout: float) -> list[str]:
    """GET {base_url}/models（OpenAI 兼容），返回模型 id 列表。

    仅服务「拉取模型」按钮；不写任何配置。端点不提供该路径时如实报错（不猜模型名）。
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = _base_headers()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if _is_waf_block(exc.code, _read_error_body(exc)):
            raise ModelUnavailable(f"拉取模型：{_WAF_BLOCK_HINT}") from exc
        if exc.code in (401, 403):
            raise ModelUnavailable(
                f"服务端返回 {exc.code}（认证被拒绝）：请确认密钥与 base_url 匹配、密钥未失效且未欠费"
            ) from exc
        if exc.code == 404:
            raise ModelUnavailable(
                "该端点未提供 /models 接口（部分网关只开放 chat/completions），请手动填写模型名"
            ) from exc
        raise ModelUnavailable(f"服务端返回 {exc.code}：无法拉取模型列表") from exc
    except ModelUnavailable:
        raise
    except Exception as exc:
        raise ModelUnavailable(f"拉取模型失败：{_probe_exception_detail(exc)}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelUnavailable("响应不是合法 JSON：该端点可能不是 OpenAI 兼容接口") from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        raise ModelUnavailable("模型列表返回结构异常（缺 data 数组）")
    models: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if model_id and model_id not in models:
            models.append(model_id)
    return models


def probe_completion(base_url: str, model: str, api_key: str, *, timeout: float) -> ModelReply:
    """草稿态连通性探测：用给定连接参数发起最小 chat/completions，返回应答与延迟。

    与 `call_active_model` 的区别：端点/模型/密钥由调用方直接给出（密钥可以是尚未保存的
    明文），因此不读数据库、不写凭据、不影响「使用中」方案。
    """
    started = time.monotonic()
    messages = format_messages(_PROBE_SYSTEM, _PROBE_USER)
    try:
        payload = _post_json(
            base_url, _chat_completion_payload(messages, model, timeout), api_key or None, timeout
        )
        content = _extract_content(payload)
    except ReasoningBudgetExhausted:
        # 推理模型思考把预算烧光 → 关闭思考重试一次，与主链路同口径。
        payload = _post_json(
            base_url,
            _chat_completion_payload(messages, model, timeout, thinking_disabled=True),
            api_key or None,
            timeout,
        )
        content = _extract_content(payload)
    except urllib.error.HTTPError as exc:
        # 认证/协议类错误对用户最有诊断价值，按状态码如实转述（不回传 body，避免泄漏）。
        raise ModelUnavailable(_probe_http_error_detail(exc.code)) from exc
    except ModelUnavailable:
        raise
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        raise ModelUnavailable(
            f"连通性测试失败（{latency_ms}ms）：{_probe_exception_detail(exc)}"
        ) from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    return ModelReply(content=content, model=model, provider=base_url, latency_ms=latency_ms)


def _probe_http_error_detail(status_code: int) -> str:
    """按 HTTP 状态码给出可操作的中文诊断，不回传服务端 body。"""
    if status_code in (401, 403):
        return (
            f"服务端返回 {status_code}：可能认证被拒绝（base_url 与密钥不匹配、密钥失效/欠费），"
            "也可能被网关 WAF 按客户端特征拦截——密钥无误时优先怀疑后者"
        )
    if status_code == 404:
        return "服务端返回 404：路径不存在，请确认 base_url 是否已带 /v1（如 https://host/v1）"
    if status_code == 429:
        return "服务端返回 429（触发限流）：稍后重试或改用更小的模型"
    if status_code >= 500:
        return f"服务端返回 {status_code}（上游网关/服务异常）：请确认 base_url 直连服务商而非中转层"
    return f"服务端返回 {status_code}：请求被拒绝"


def _probe_exception_detail(exc: Exception) -> str:
    """把底层异常压成一句人话（不泄漏请求正文）。"""
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        return f"无法连接端点（{reason}）：请检查网络、base_url、代理设置"
    if isinstance(exc, TimeoutError):
        return "请求超时：端点无响应或超时过短，可调大超时秒数"
    if isinstance(exc, json.JSONDecodeError):
        return "响应不是合法 JSON：该端点可能不是 OpenAI 兼容接口"
    return "请检查网络、端点和协议配置"


def active_model_profile(
    profiles: list[ModelProfile],
) -> ModelProfile | None:
    """从方案列表中取唯一「使用中」方案；没有（或异常多个）时返回 None。"""
    from investment_steward_core.domain import ModelProfileStatus

    active = [item for item in profiles if item.status == ModelProfileStatus.ACTIVE]
    if len(active) != 1:
        return None
    return active[0]


def format_messages(sys_prompt: str, user_text: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_text}]
