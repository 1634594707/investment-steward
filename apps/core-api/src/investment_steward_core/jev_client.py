"""Jev 决策模型调用器（JV01）：`state` + 类型化 `questions` → 类型化 `answers`。

**定位**（对齐《Jev 接入契约》2026-09-21）：Jev 是 System One 决策模型，只做「是/否、选哪个、打几分」
的原子判断，不生成正文。它与 chat 链路是**两套协议**：

- chat 走 `POST {base_url}/chat/completions`，由 `ModelProfile`（「恰好一个使用中」）代理；
- Jev 走 decisions 协议：官方是 `POST {base_url}/systemone`，网关给的是完整端点（见 `resolve_endpoint`）。
  连接参数由调用方以 `JevConfig` 传入——**刻意不进 `ModelProfile`**（`domain/models.py:313`），
  避免「恰好一个使用中」的语义被第二种协议污染。

**工程口径**：
- 纯 stdlib（`urllib.request`），零新增依赖，镜像 `model_client.py` 风格；**不引入 typesafe-sdk**；
- 密钥只进 Authorization 头，永不落日志、不进审计 payload、不回传前端；错误只暴露无密钥误差类；
- 用量经 `model_client.emit_model_call_record` 落 `model_calls`——**同一个钩子、同一张表、
  同一个注入点**（`api/app.py:1394`），不另开需要同步注入的第二钩子；
- 任何不可用（总闸关闭 / 未启用 / 凭据缺失 / 超时 / 网络 / 非 2xx / 响应畸形）→ `JevUnavailable`，
  由调用方降级并**如实标注「本轮 Jev 未参与」**，绝不假装质检已跑。

**官方协议事实**（docs.typesafe.ai，2026-09-21 读取）：

| 项 | 事实 |
| --- | --- |
| 端点 | 官方 `POST {base_url}/systemone`（`base_url` 形如 `https://api.typesafe.ai/v1`）；**网关给出的 URL 本身就是完整端点**（OpenRouter `https://openrouter.ai/api/alpha/decisions`、jevtypesafe 托管 key `https://jevtypesafeai.com/api/v1/decide`），再拼一段必然 404 → 解析规则见 `resolve_endpoint` |
| 请求 | `{state, model, questions}`；`state` 仅文本（string / object / array） |
| `noul` 应答 | `{type, noul: 0–1}`——**没有 confidence**（两结果分布由单值完整描述） |
| `choice` 应答 | `{type, choice, probabilities, confidence}`；options 上限 **255** |
| `score` 应答 | `{type, score, legend, probabilities, confidence}`；criteria 为**有序等级数组 2–10 级** |
| `score` 语义 | 等级轴上的**概率加权均值**（`Σ 等级号 × 概率`，可为小数），**不是 0–100 分制** |
| `usage` | 只有 `input_tokens` / `output_tokens`，**无 `total_tokens`**（网关同样口径，OpenRouter 另多一个可选 `cost`） |
| 错误码 | 401 密钥无效 / 422 请求体校验失败 / 429 限流 / 529 过载；网关另有 402 余额不足、403 拒绝、413 请求体过大、5xx 上游异常 |
| 硬上限 | 64k tokens/请求；`state` + 最长单题 ≤ 32k |

⚠️ **中文（CJK）风险（R6）**：官方声明英文为训练主语言、精度最佳，CJK「handled but not equally
well」。本模块内置的 `JEV_NOUL_YES` / `JEV_NOUL_NO` **已于 2026-09-21 用 50 条中文样本标定**
（报告：`docs/evidence/jev-cjk-calibration-2026-09-21.md`），不再是官方英文范例值；换端点、
换模型版本或样本分布变化后须重跑标定。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from investment_steward_core.domain.models import JevSettings
from investment_steward_core.model_client import (
    MODEL_USER_AGENT,
    emit_model_call_record,
    resolve_credential,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

JEV_DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
#: 官方默认别名；响应 `model` 会回真实版本号（如 `jev-1.13.0`）。别名随发布移动，
#: 已按某版本标定过 confidence 阈值的场景应改钉版本号（官方 /models#aliases 建议）。
#: ⚠️ 走网关时模型名要带命名空间：OpenRouter `typesafe/jev`（或钉版本 `typesafe/jev-1.13`）、
#: Vercel AI Gateway `typesafe-ai/jev`。网关不会认官方别名 `jev-latest`。
JEV_MODEL_ALIAS = "jev-latest"
#: 题型与 state 结构版本。改动必须升版本（对齐 `tactics.TACTIC_WEIGHT_VERSION` 惯例）。
JEV_QUESTION_SCHEMA_VERSION = "v1"

#: 官方端点的路径段：`base_url` 是「根 + 版本段」时由本模块补上它。
JEV_DECISION_PATH = "systemone"
#: 版本段判定（`v1` / `v2` …）。路径以它结尾 → 视为 API 根，需要补 `systemone`。
_VERSION_SEGMENT = re.compile(r"^v\d+$")
#: 已知网关的模型列表端点。官方是 `{base_url}/models`，网关各不相同（OpenRouter 是
#: `/api/v1/models`，与 decisions 端点不同源），所以只能按 host 查表 + 兜底。
_GATEWAY_MODELS_URL: dict[str, str] = {
    "openrouter.ai": "https://openrouter.ai/api/v1/models",
}
#: OpenRouter 上挂着 400+ 模型，拉取列表时只保留 TypeSafe 家的（否则设置页下拉框没法用）。
_GATEWAY_MODEL_PREFIX: dict[str, str] = {
    "openrouter.ai": "typesafe/",
}

_JEV_REQUEST_TIMEOUT = 60.0  # System One 是「瞬时判断」，秒级返回；60s 已是很宽松的上限
# 公开别名：草稿态探测与设置页需要内置默认超时，私有名不跨模块引用。
JEV_REQUEST_TIMEOUT = _JEV_REQUEST_TIMEOUT

#: 官方硬上限（超界由服务端 422 拒绝，本地先拦以省一次计费请求）。
JEV_CHOICE_MAX_OPTIONS = 255
JEV_SCORE_MIN_LEVELS = 2
JEV_SCORE_MAX_LEVELS = 10

#: `noul` 三路划分默认阈值。
#:
#: ⚠️ 这两个值**不是官方英文范例值**（官方范例是 NO=0.2 / YES=0.8），而是 R6 用 50 条中文样本
#: 标定出来的（2026-09-21，`scripts/calibrate-jev-cjk.py`，报告见
#: `docs/evidence/jev-cjk-calibration-2026-09-21.md`）：
#: 受控探针 20 条（10 正 / 10 负）上 AUC=1.0，正类中位数 0.95、负类中位数 0.15；
#: 取 YES=0.85 / NO=0.3 时编造召回 100%、误降级 0%、明确判定率 90%（不确定带 10%）。
#:
#: 为什么中文的 YES 要**更高**、NO 也要**更高**（不确定带整体上移）：官方声明 CJK 精度低于
#: 英文，实测负类里出现 0.53 / 0.75 两个高位离群值——中文「没编造」也会偶发偏高。把 NO 抬到
#: 0.3 是承认这个上偏，把 YES 抬到 0.85 是要求更高的证据强度才敢判「编造」。样本量仅 50 条，
#: 样本一换就可能要重标——**改这两个数必须重跑标定并更新报告，不要凭手感调**。
JEV_NOUL_YES = 0.85
JEV_NOUL_NO = 0.3

# ---------------------------------------------------------------------------
# WAF 特征（与 `model_client.py` 的 `_WAF_BLOCK_MARKERS` 同源，改动需两侧同步）
#
# 官方托管端点通常不经用户自建 WAF，但 base_url 允许用户改填自建中转，故保留同款判定：
# 403 到底是「密钥被拒」还是「客户端特征被 WAF 拦」，对用户是两种截然不同的处理方向。
# ---------------------------------------------------------------------------
_WAF_BLOCK_MARKERS = ("error-1010", "cloudflare", "cf-error", '"code":1010', "code: 1010")

_WAF_BLOCK_HINT = (
    "被网关的 WAF 按客户端特征拦截（Cloudflare 1010），不是密钥问题"
    "——请确认 base_url 是否指向官方端点或中转层是否要求特定 User-Agent／IP 白名单"
)


class JevUnavailable(Exception):
    """Jev 调用不可用：总闸关闭 / 未启用 / 凭据缺失 / 超时 / 网络 / 非 2xx / 响应畸形。

    刻意**不继承** `model_client.ModelUnavailable`：chat 链路大量以 `except ModelUnavailable`
    做「回退本地确定性引擎」，若继承，Jev 的失败会被那些处理器误当成 chat 失败而走错降级分支。
    两者是并列的两套协议，异常也并列。

    `status_code` 只在「服务端确实返回了 HTTP 状态码」时才有值（其余为 None）。批量调用方
    （JV05/JV08）的退避策略必须**依据它**而不是解析中文文案——文案是给人看的，会改；
    状态码是协议的一部分，不会。另见 `retryable`。
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """是否值得按指数退避重试。

        429（限流）与 529（过载）是**暂时性**的，退避后大概率能过；5xx 上游异常同理。
        401/402/403/404/413/422 是**确定性**失败——重试多少次都还是同一个结果，只会白烧配额，
        所以判为不可重试。**本地判定**（总闸关闭/未启用/凭据缺失/响应畸形，`status_code=None`）
        也一律不重试：这些重试没有意义（前两者重试仍关闭，后者是模型回了个畸形结构）。
        """
        code = self.status_code
        if code is None:
            return False
        return code == 429 or code == 529 or code >= 500


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JevConfig:
    """Jev 连接参数（**不是** `ModelProfile`）。

    `credential_ref` 与 chat 链路同口径：可填凭据库 key_id、凭据尾号（last4），
    或由调用方在草稿态直接给明文密钥（仅探测路径，见 `probe_systemone`）。
    """

    base_url: str = JEV_DEFAULT_BASE_URL
    model: str = JEV_MODEL_ALIAS
    credential_ref: str = ""
    timeout_secs: float = JEV_REQUEST_TIMEOUT
    #: 场景级开关（`STEWARD_JEV_ENABLED` / 设置页「启用」）：与全局出网总闸是两层，
    #: 总闸关闭时连 Jev 配置都不看。
    enabled: bool = True

    @property
    def resolved_base_url(self) -> str:
        """规范化 base_url：去空白、去尾斜杠；空值回退官方默认。"""
        return (self.base_url or "").strip().rstrip("/") or JEV_DEFAULT_BASE_URL

    @property
    def resolved_endpoint(self) -> str:
        """**实际 POST 的完整 URL**（见 `resolve_endpoint`）。"""
        return resolve_endpoint(self.resolved_base_url)

    @property
    def resolved_model(self) -> str:
        return (self.model or "").strip() or JEV_MODEL_ALIAS


# ---------------------------------------------------------------------------
# 端点解析（官方 base 需要补路径；网关给的是完整端点）
#
# 为什么不能一律拼 `/systemone`：2026-09-21 用户实测——把 base_url 填成
# `https://openrouter.ai/api/alpha/decisions`（OpenRouter 的 Decisions 端点）后，
# 客户端拼成 `…/decisions/systemone` → 404「路径不存在」。各网关路径互不相同
# （OpenRouter `/api/alpha/decisions`、jevtypesafe 托管 key `/api/v1/decide`），
# 枚举不完，所以规则改成：**只给「API 根」补路径，其余按用户填的原样使用**——
# 用户填的是完整端点，我们不该替他猜。
# ---------------------------------------------------------------------------


def resolve_endpoint(base_url: str) -> str:
    """把用户填的 `base_url` 解析成实际请求的完整 URL。

    规则（简单到能一眼预测，避免「有时拼有时不拼」的隐式行为）：

    - 路径为空（`https://api.typesafe.ai`）或以**版本段**结尾（`…/v1`、`…/v2`）
      → 视为 API 根，补 `/{JEV_DECISION_PATH}`；
    - 其余一律**原样使用**：`…/v1/systemone`（官方完整端点，补了会变 `/systemone/systemone`）、
      `https://openrouter.ai/api/alpha/decisions`、`https://jevtypesafeai.com/api/v1/decide`
      都是完整端点。

    注意 `…/v1/decide` 这类「版本段后面还有内容」的路径会落进第二种，符合预期；
    只有**紧邻的最后一个路径段**是版本段才补。
    """
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        cleaned = JEV_DEFAULT_BASE_URL
    path = urllib.parse.urlsplit(cleaned).path.rstrip("/")
    if not path:
        return f"{cleaned}/{JEV_DECISION_PATH}"
    last_segment = path.rsplit("/", 1)[-1]
    if _VERSION_SEGMENT.match(last_segment):
        return f"{cleaned}/{JEV_DECISION_PATH}"
    return cleaned


def resolve_models_url(base_url: str) -> str:
    """「拉取模型」要打的 URL。

    官方是 `{base}/models`（响应 `{"models": [{"name", …}]}`）；网关的模型列表在**另一个**
    端点（OpenRouter 是 `https://openrouter.ai/api/v1/models`，响应 `{"data": [{"id"}]}`），
    所以按 host 查表，查不到就退回官方形状——查不到时设置页会如实显示服务端返回的错误，
    不会静默给个空列表。
    """
    cleaned = (base_url or "").strip().rstrip("/") or JEV_DEFAULT_BASE_URL
    host = (urllib.parse.urlsplit(cleaned).hostname or "").lower()
    known = _GATEWAY_MODELS_URL.get(host)
    return known or f"{cleaned}/models"


# ---------------------------------------------------------------------------
# 生效配置与判定回调（JV02 优先级 + JV04 语义层胶水）
#
# 为什么放在本模块而不是 `api/app.py`：研报端点与协同流水线**都要**构造判定回调，
# 若各自实现一遍「设置页 > 环境变量 > 默认」的优先级，两处迟早会漂移成两个口径。
# 这里按**鸭子类型**取 `core`（只读 `database` / `local_user_id` / `settings`），
# 不反向 import API 层，也就没有循环依赖。
# ---------------------------------------------------------------------------


def effective_settings(core: Any) -> JevSettings:
    """生效配置：设置页保存值（`jev_config` 表）> `STEWARD_JEV_*` 环境变量 > 内置默认。

    JV02 拍板的优先级；设置页保存过就以保存值为准（含显式关闭）。
    """
    saved = core.database.get_jev_settings(core.local_user_id)
    if saved is not None:
        return saved
    settings = core.settings
    return JevSettings(
        user_id=core.local_user_id,
        enabled=settings.jev_enabled,
        base_url=settings.jev_base_url,
        model=settings.jev_model,
        credential_ref=settings.jev_credential_ref,
        timeout_secs=settings.jev_timeout_secs,
    )


def build_judge(
    core: Any,
    credential_store: Any,
    *,
    purpose: str = "jev:claim-support",
) -> Callable[[Any, Mapping[str, Any]], dict[str, Any]] | None:
    """把「生效配置 + 凭据库 + 总闸」封装成研报质量闸门可用的语义判定回调（JV04）。

    **返回 None 表示整层不运行**——全局出网总闸关闭、或 Jev 未启用时如此。调用方把 None
    原样交给 `report_quality.validate_report(jev_judge=None)`，闸门行为与接入前逐字节一致
    （JV00 铁律 6：关掉就真的关掉，不留半截行为）。

    回调本身**只负责出网**：state/题目构造与结果合并都在 `report_quality`（保持纯函数）。
    回调**不吞异常**——`JevUnavailable` 直接抛给闸门，由闸门写进 `jev.note` 如实标注
    「这一层没跑」，绝不静默当成「校验通过」。
    """
    settings = effective_settings(core)
    if not settings.enabled:
        return None
    if not bool(getattr(core.settings, "model_access_enabled", False)):
        return None
    config = JevConfig(
        base_url=settings.base_url,
        model=settings.model,
        credential_ref=settings.credential_ref,
        timeout_secs=settings.timeout_secs,
        enabled=settings.enabled,
    )

    def judge(state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        reply = call_jev(
            config,
            credential_store,
            state,
            questions,
            purpose=purpose,
            access_enabled=True,
        )
        return {"answers": reply.answers, "model": reply.model}

    return judge


# ---------------------------------------------------------------------------
# 批量分片复核胶水（JV05 扫描去误报 / JV08 通知分诊共用）
#
# 与 `build_judge` 同源，但多了一件 `build_judge` 没有的事：**分片级重试**。
# 为什么必须在这一层做（而不是让 `call_jev` 自己重试）：`call_jev` 的 docstring 已定下
# 「不做自动重试」——输入 token 计费，静默重试等于静默双倍计费。但批量场景（几十票 ×
# 若干分片）遇到 429 限流时不退避就会整批失败，代价更大。折中是：**重试决策交给知道
# 「这一批有多少分片、还有多少预算」的调用方**，且只在明确可重试的状态码上重试。
#
# 粒度选「分片」而不是「整轮」：一个分片失败不该让另外三个分片的成功结果作废。
# 失败的分片如实带 `error` 回去，由各场景的合并函数（`tactics_ai.jev_scan_annotations` /
# `notifications.jev_triage_findings`）标成「未取得判定」——绝不把「没评」渲染成「评过了」。
#
# **两个场景共用同一个实现**（而不是各写一遍）：退避序列、可重试状态码判定、分片失败隔离
# 这三件事与「评的是什么」无关；各写一遍迟早会漂成两套口径，而其中一套会悄悄少一次退避。
# 差异只有 `purpose`（`model_calls` 归集用，JV10 成本对账靠它分场景）——所以 `purpose`
# 是**必填**而不是给默认值：默认值会让新场景忘了改，把成本记到别的场景头上。
# ---------------------------------------------------------------------------

#: 单个分片最多尝试几次（含首次）。3 次 ≈ 覆盖一次短时限流窗口，再多就是白烧时延。
JEV_SHARD_MAX_ATTEMPTS = 3
#: 退避基数与上限（秒）。1.5 → 3 → 6 …，封顶 12 秒。指数退避是官方 429 文档的建议。
JEV_SHARD_BACKOFF_BASE_SECS = 1.5
JEV_SHARD_BACKOFF_CAP_SECS = 12.0


def build_shard_reviewer(
    core: Any,
    credential_store: Any,
    *,
    purpose: str,
    max_attempts: int = JEV_SHARD_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Mapping[str, Any]], list[dict[str, Any]]] | None:
    """把「生效配置 + 凭据库 + 总闸」封装成批量分片复核可用的分片出网回调。

    调用方：JV05 `purpose="jev:scan-review"`、JV08 `purpose="jev:notify-triage"`。

    **返回 None 表示整层不运行**（总闸关闭 / Jev 未启用）——与 `build_judge` 同一口径：
    调用方拿到 None 就不去构造 state、不去算逐票附加材料，主链路与接入前逐字节一致。

    回调入参是分片包的返回体 `{"shards", "skipped"}`（`tactics_ai.jev_scan_shards` /
    `notifications.jev_triage_shards` 同形状），出参是逐分片的
    `{"evaluated", "answers", "error", "attempts", "model"}`。

    重试口径（`exc.retryable`，见 `JevUnavailable`）：只对 429 / 529 / 5xx 退避重试；
    401/402/403/404/413/422 与本地判定一律**不重试**——它们重试多少次都是同一结果，
    重试只会把一次明确失败拖成三次明确失败并多烧配额。

    `purpose` **必填**：它会落进 `model_calls.purpose`，是 JV10 分场景对账的唯一依据。
    给默认值等于给「新场景忘了改」留后门。

    `sleep` 可注入，便于测试断言退避序列而不真的睡（生产用 `time.sleep`）。
    """
    settings = effective_settings(core)
    if not settings.enabled:
        return None
    if not bool(getattr(core.settings, "model_access_enabled", False)):
        return None
    config = JevConfig(
        base_url=settings.base_url,
        model=settings.model,
        credential_ref=settings.credential_ref,
        timeout_secs=settings.timeout_secs,
        enabled=settings.enabled,
    )
    attempts_allowed = max(1, int(max_attempts))

    def review(shards_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for shard in shards_payload.get("shards") or []:
            evaluated = list(shard.get("evaluated") or [])
            questions = dict(shard.get("questions") or {})
            state = shard.get("state")
            answers: dict[str, JevAnswer] | None = None
            error: str | None = None
            reported_model: str | None = None
            attempts = 0
            for attempt in range(1, attempts_allowed + 1):
                attempts = attempt
                try:
                    reply = call_jev(
                        config,
                        credential_store,
                        state,
                        questions,
                        purpose=purpose,
                        access_enabled=True,
                    )
                except JevUnavailable as exc:
                    if attempt < attempts_allowed and exc.retryable:
                        delay = min(
                            JEV_SHARD_BACKOFF_BASE_SECS * (2 ** (attempt - 1)),
                            JEV_SHARD_BACKOFF_CAP_SECS,
                        )
                        sleep(delay)
                        continue
                    error = str(exc)
                    break
                answers = reply.answers
                reported_model = reply.model
                break
            results.append({
                "evaluated": evaluated,
                "answers": answers or {},
                "error": error,
                "attempts": attempts,
                "model": reported_model,
            })
        return results

    return review


# ---------------------------------------------------------------------------
# 题型构造器（三种题可混在同一请求，官方 fan-out 模式）
# ---------------------------------------------------------------------------


def _require_instructions(instructions: Any) -> Any:
    if isinstance(instructions, str) and not instructions.strip():
        raise ValueError("Jev 题目 instructions 不能为空字符串")
    if instructions is None:
        raise ValueError("Jev 题目必须提供 instructions")
    return instructions


def noul_question(instructions: Any, *, true_means: str | None = None, false_means: str | None = None) -> dict[str, Any]:
    """是非题：返回 0–1 的 `noul` 值（**无 confidence**）。判定阈值由调用方按场景设定。

    官方建议：措辞要让**高值代表「是」**（「是否包含 X」清晰，「是否不含 X」会让读值的代码弄反）；
    边界微妙时补 `criteria` 的 true/false 描述。
    """
    question: dict[str, Any] = {"type": "noul", "instructions": _require_instructions(instructions)}
    if true_means or false_means:
        question["criteria"] = {
            "true": true_means or "问题所述情况成立",
            "false": false_means or "问题所述情况不成立",
        }
    return question


def choice_question(instructions: Any, options: Mapping[str, str | None] | Sequence[str]) -> dict[str, Any]:
    """单选题：返回所选项 + 概率分布 + confidence。

    `options` 接受 `{选项: 描述或 None}` 映射，或纯选项名序列（描述留空）。
    官方上限 **255** 项；本地先拦，避免越界请求白付一次输入 token。
    """
    if isinstance(options, Mapping):
        criteria: dict[str, str | None] = {}
        for option, description in options.items():
            key = str(option).strip()
            if not key:
                raise ValueError("Jev choice 选项名不能为空")
            criteria[key] = description
    elif isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        criteria = {}
        for option in options:
            key = str(option).strip()
            if not key:
                raise ValueError("Jev choice 选项名不能为空")
            criteria[key] = None
    else:
        raise ValueError("Jev choice 的 options 必须是映射或选项名序列")

    if not criteria:
        raise ValueError("Jev choice 至少需要一个选项")
    if len(criteria) > JEV_CHOICE_MAX_OPTIONS:
        raise ValueError(f"Jev choice 选项最多 {JEV_CHOICE_MAX_OPTIONS} 项，当前 {len(criteria)} 项")
    return {"type": "choice", "instructions": _require_instructions(instructions), "criteria": criteria}


def score_question(instructions: Any, levels: Sequence[str]) -> dict[str, Any]:
    """打分题：`levels` 是**有序等级描述数组**（等级编号 = 数组下标，从 0 起）。

    官方要求 **2–10 级**，且必须**描述情境而非程度**——实测「只给数字等级」（如 `["0","1","2"]`）
    会让概率在相邻级间摊平、confidence 掉到 0.33；描述性等级才会给出单峰高置信答案。

    响应 `score` 是等级轴上的概率加权均值（范围 `0 … len(levels)-1`），**不是 0–100**；
    要 0–100 请用 `JevAnswer.score_percent`（本机换算，非 Jev 原生输出）。
    """
    if isinstance(levels, (str, bytes)) or not isinstance(levels, Sequence):
        raise ValueError("Jev score 的 levels 必须是有序等级描述数组")
    cleaned: list[str] = []
    for level in levels:
        text = str(level).strip()
        if not text:
            raise ValueError("Jev score 的等级描述不能为空（空等级会让模型无从匹配）")
        cleaned.append(text)
    if len(cleaned) < JEV_SCORE_MIN_LEVELS:
        raise ValueError(f"Jev score 至少需要 {JEV_SCORE_MIN_LEVELS} 个等级，当前 {len(cleaned)} 个")
    if len(cleaned) > JEV_SCORE_MAX_LEVELS:
        raise ValueError(f"Jev score 最多 {JEV_SCORE_MAX_LEVELS} 个等级，当前 {len(cleaned)} 个")
    return {"type": "score", "instructions": _require_instructions(instructions), "criteria": cleaned}


def _validate_question(question_id: str, question: Any) -> str:
    """校验单道题目结构，返回题型字符串。题型定义越界属代码缺陷 → `ValueError`（不是降级条件）。"""
    if not isinstance(question, dict):
        raise ValueError(f"Jev 题目「{question_id}」必须是 dict")
    qtype = question.get("type")
    if qtype == "noul":
        _require_instructions(question.get("instructions"))
    elif qtype == "choice":
        _require_instructions(question.get("instructions"))
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(f"Jev choice 题「{question_id}」缺少非空 criteria")
        if len(criteria) > JEV_CHOICE_MAX_OPTIONS:
            raise ValueError(f"Jev choice 题「{question_id}」选项超过 {JEV_CHOICE_MAX_OPTIONS} 项")
    elif qtype == "score":
        _require_instructions(question.get("instructions"))
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or not (JEV_SCORE_MIN_LEVELS <= len(criteria) <= JEV_SCORE_MAX_LEVELS):
            raise ValueError(
                f"Jev score 题「{question_id}」的 criteria 必须是 {JEV_SCORE_MIN_LEVELS}–{JEV_SCORE_MAX_LEVELS} 个等级的有序数组"
            )
    else:
        raise ValueError(f"Jev 题目「{question_id}」的 type 必须是 noul / choice / score，当前 {qtype!r}")
    return str(qtype)


def _validate_state(state: Any) -> None:
    """state 仅文本：string / object / array（官方明确不支持图像、音频、视频）。

    空 state / 空数组属**运行时数据条件**（本次没东西可判）→ `JevUnavailable`，由调用方降级；
    类型不对属代码缺陷 → `ValueError`。
    """
    if isinstance(state, str):
        if not state.strip():
            raise JevUnavailable("Jev state 为空字符串，本次无内容可判（调用方应跳过该层）")
        return
    if isinstance(state, Mapping):
        if not state:
            raise JevUnavailable("Jev state 为空对象，本次无内容可判（调用方应跳过该层）")
        return
    if isinstance(state, Sequence) and not isinstance(state, (str, bytes)):
        if not state:
            raise JevUnavailable("Jev state 为空数组，本次无内容可判（调用方应跳过该层）")
        for index, item in enumerate(state):
            if not isinstance(item, (str, Mapping, Sequence)):
                raise ValueError(f"Jev state 第 {index} 项必须是文本 / 对象 / 数组")
        return
    raise ValueError("Jev state 只能是字符串、对象或数组（官方仅接受文本输入，不支持图像/音频/视频）")


def _state_char_count(state: Any) -> int:
    """state 的字符数（仅用于诊断提示，**不**据此推算 token——不猜）。"""
    if isinstance(state, str):
        return len(state)
    try:
        return len(json.dumps(state, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 应答类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JevAnswer:
    """一道题的答案。三题型共用；不适用的字段保持 None（**绝不伪造**）。"""

    question_id: str
    type: str
    #: `noul` 题：答案为「是」的概率（0–1）
    noul: float | None = None
    #: `choice` 题：概率最高的选项
    choice: str | None = None
    #: `score` 题：等级轴上的概率加权均值（**非 0–100**）
    score: float | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    legend: Mapping[str, Any] = field(default_factory=dict)
    #: 仅 `choice` / `score` 有；`noul` 恒为 None（官方不给 noul 的 confidence）
    confidence: float | None = None
    #: `score` 题：请求侧 criteria 的等级数（响应不含，由请求带回，供归一化用）
    levels: int | None = None

    @property
    def normalized_score(self) -> float | None:
        """0–1 归一化：`score / (等级数 - 1)`（官方 composite-scoring 的归一化口径）。

        不同长度量表必须归一化后才能跨题加权合成——这是官方明确要求，且**合成留在代码里**。
        """
        if self.score is None or self.levels is None or self.levels < 2:
            return None
        return self.score / (self.levels - 1)

    @property
    def score_percent(self) -> float | None:
        """0–100 展示分。

        ⚠️ **这是本机换算值，不是 Jev 原生输出**——Jev 的 `score` 上限是 `len(criteria)-1`。
        落库留痕必须同时保存原始 `score`、本换算值与 `confidence`，否则事后无法复算。
        """
        normalized = self.normalized_score
        return None if normalized is None else round(normalized * 100, 2)

    def noul_verdict(self, *, yes: float = JEV_NOUL_YES, no: float = JEV_NOUL_NO) -> str | None:
        """`noul` 三路划分：`yes` / `no` / `uncertain`。非 noul 题返回 None。

        官方范例用 NO=0.2 / YES=0.8 把中间地带交给人工/复核，而不是硬二值化——本方法同口径。
        ⚠️ 默认阈值是官方英文范例值，中文场景须按 R6 重新标定后再用。
        """
        if self.noul is None:
            return None
        if self.noul >= yes:
            return "yes"
        if self.noul <= no:
            return "no"
        return "uncertain"

    def top_probability(self) -> float | None:
        if not self.probabilities:
            return None
        return max(self.probabilities.values())


@dataclass(frozen=True)
class JevReply:
    """一次 decisions 调用的结果。"""

    #: 响应 `model` 回的真实版本号（如 `jev-1.13.0`）——留痕用它，不用别名
    model: str
    requested_model: str
    answers: Mapping[str, JevAnswer]
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    purpose: str | None = None
    schema_version: str = JEV_QUESTION_SCHEMA_VERSION

    def answer(self, question_id: str) -> JevAnswer:
        try:
            return self.answers[question_id]
        except KeyError as exc:
            raise JevUnavailable(f"Jev 应答里没有题目「{question_id}」") from exc


@dataclass(frozen=True)
class JevProbeReply:
    """草稿态探测结果：模型版本、延迟与一道探测题的原始应答。"""

    model: str
    requested_model: str
    latency_ms: int
    sample: JevAnswer
    input_tokens: int | None = None
    output_tokens: int | None = None


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


def _base_headers() -> dict[str, str]:
    """出网公共头（显式 UA，与 chat 链路同源，绕开网关对 Python-urllib 的特征封禁）。"""
    return {"User-Agent": MODEL_USER_AGENT, "Accept": "application/json"}


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    """安全读取错误响应体（仅用于 WAF 特征判定，绝不原文回传前端）。"""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - 错误体读不到就当没有（仅用于 WAF 特征判定）
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")[:2000]
    return str(raw)[:2000]


def _is_waf_block(status_code: int, body: str) -> bool:
    if status_code != 403:
        return False
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _WAF_BLOCK_MARKERS)


def _http_error_detail(status_code: int, body: str) -> str:
    """按状态码给出可操作的中文诊断。

    **刻意不回传服务端 body**：422 的 body 会指明出错字段，但也可能回显请求内容（含用户数据），
    与「不把用户数据带出调用边界」的口径冲突——因此只按状态码给出三条最可能的原因。
    """
    if status_code == 401:
        return (
            "服务端返回 401（密钥缺失或无效）：请检查「设置 → Jev 决策模型」的凭据引用——"
            "可填凭据库 key_id、凭据尾号，或直接粘贴明文密钥（会自动加密存入本机凭据库）"
        )
    if status_code == 402:
        return "服务端返回 402（余额不足）：网关账户余额或额度已用尽，请到网关控制台充值/提额"
    if status_code == 403:
        if _is_waf_block(status_code, body):
            return _WAF_BLOCK_HINT
        return "服务端返回 403：请求被拒绝，请确认密钥有效且 base_url 指向官方端点或网关端点"
    if status_code == 404:
        return (
            "服务端返回 404（路径不存在）：base_url 需要按端点形态填——"
            "① 官方填**API 根** `https://api.typesafe.ai/v1`（本机会自动补 /systemone）；"
            "② 走网关则填**完整端点**，如 OpenRouter `https://openrouter.ai/api/alpha/decisions`、"
            "jevtypesafe 托管 key `https://jevtypesafeai.com/api/v1/decide`（本机不会补任何路径）"
        )
    if status_code == 413:
        return "服务端返回 413（请求体过大）：请缩短 state 或减少单请求题量"
    if status_code == 422:
        return (
            "服务端返回 422（请求体校验失败）：常见三因——state 超过 32k token 上限、"
            "score 等级数不在 2–10、choice 选项超过 255 或必填字段缺失"
        )
    if status_code == 429:
        return (
            "服务端返回 429（触发限流）：官方限速为 250k tokens/s + 1,200 req/min 且会动态调整，"
            "批量场景应减少单请求题量或按指数退避分片后重试"
        )
    if status_code == 529:
        return "服务端返回 529（TypeSafe 暂时过载）：请按指数退避重试"
    if status_code >= 500:
        return f"服务端返回 {status_code}（上游异常）：请确认 base_url 直连服务端而非中转层"
    return f"服务端返回 {status_code}：请求被拒绝"


def _post_endpoint(
    base_url: str,
    json_payload: dict[str, Any],
    api_key: str | None,
    timeout: float,
) -> dict[str, Any]:
    """`POST {resolve_endpoint(base_url)}`，返回解析后的 JSON dict。

    `base_url` 是**用户填的值**（官方 API 根或网关完整端点），路径解析交给 `resolve_endpoint`。
    """
    url = resolve_endpoint(base_url)
    headers = _base_headers()
    headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise JevUnavailable(
            _http_error_detail(exc.code, _read_error_body(exc)), status_code=exc.code
        ) from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevUnavailable("Jev 响应不是合法 JSON（端点可能不是 decisions 协议）") from exc
    if not isinstance(parsed, dict):
        raise JevUnavailable("Jev 返回结构异常（非 JSON 对象）")
    return parsed


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _extract_usage(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """解析 `usage` 的 input/output token。

    ⚠️ Jev 的 usage **只有** `input_tokens` / `output_tokens`，**没有 `total_tokens`**
    （与 chat 的 prompt/completion/total 三项不同）。缺失或形状异常一律 None——不按字数反推。
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _int_or_none(usage.get("input_tokens")), _int_or_none(usage.get("output_tokens"))


def _parse_answers(
    payload: dict[str, Any],
    questions: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, JevAnswer]:
    """逐题类型化解析。**任一处对不上就抛错**——不猜、不降级为「大概没问题」。"""
    raw = payload.get("answers")
    if not isinstance(raw, dict):
        raise JevUnavailable("Jev 返回结构异常（缺少 answers 对象）")

    parsed: dict[str, JevAnswer] = {}
    for question_id, question in questions.items():
        item = raw.get(question_id)
        if not isinstance(item, dict):
            raise JevUnavailable(f"Jev 返回缺少题目「{question_id}」的答案（应答不完整，不可当作已判定）")
        qtype = str(item.get("type") or question.get("type") or "")

        if qtype == "noul":
            value = _float_or_none(item.get("noul"))
            if value is None:
                raise JevUnavailable(f"Jev 的 noul 题「{question_id}」应答缺少数值 noul")
            if not 0.0 <= value <= 1.0:
                raise JevUnavailable(f"Jev 的 noul 题「{question_id}」应答越界（{value}，应在 0–1）")
            # 官方：noul 没有 confidence。即便服务端多给了也一律置 None——不让上层误以为有第二决策轴。
            parsed[question_id] = JevAnswer(question_id=question_id, type="noul", noul=value)

        elif qtype == "choice":
            criteria = question.get("criteria") or {}
            options = [str(key) for key in criteria]
            chosen = item.get("choice")
            if not isinstance(chosen, str) or not chosen.strip():
                raise JevUnavailable(f"Jev 的 choice 题「{question_id}」应答缺少选项名")
            if options and chosen not in options:
                raise JevUnavailable(
                    f"Jev 的 choice 题「{question_id}」返回了未定义的选项「{chosen}」（预期：{'/'.join(options)}）"
                )
            parsed[question_id] = JevAnswer(
                question_id=question_id,
                type="choice",
                choice=chosen,
                probabilities=_probability_map(item.get("probabilities")),
                confidence=_float_or_none(item.get("confidence")),
            )

        elif qtype == "score":
            value = _float_or_none(item.get("score"))
            levels = len(question.get("criteria") or [])
            if value is None:
                raise JevUnavailable(f"Jev 的 score 题「{question_id}」应答缺少数值 score")
            upper = max(levels - 1, 0)
            if value < 0.0 or value > upper + 1e-6:
                # 官方 score 范围是 0 … len(criteria)-1；越界说明请求/解析对不上，宁可报错也不裁剪。
                raise JevUnavailable(
                    f"Jev 的 score 题「{question_id}」应答越界（{value}，应在 0–{upper}）——请求侧等级数与响应不一致"
                )
            parsed[question_id] = JevAnswer(
                question_id=question_id,
                type="score",
                score=min(value, float(upper)),
                probabilities=_probability_map(item.get("probabilities")),
                legend=dict(item.get("legend") or {}) if isinstance(item.get("legend"), dict) else {},
                confidence=_float_or_none(item.get("confidence")),
                levels=levels or None,
            )

        else:
            raise JevUnavailable(f"Jev 返回了未知题型「{qtype}」（题目「{question_id}」）")

    _ = model  # 仅为签名一致性保留：留痕用的模型号由调用方从 payload 取
    return parsed


def _probability_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw in value.items():
        number = _float_or_none(raw)
        if number is not None:
            out[str(key)] = number
    return out


# ---------------------------------------------------------------------------
# 可用性前置判定（**在发请求之前**）
# ---------------------------------------------------------------------------


def _ensure_available(config: JevConfig, *, access_enabled: bool) -> None:
    """总闸与场景开关判定。关闭时抛错且**绝不发请求**（对齐 JV00 铁律 4）。"""
    if not access_enabled:
        raise JevUnavailable("模型出网总闸已关闭（STEWARD_MODEL_ACCESS=0），Jev 未参与本轮判定")
    if not config.enabled:
        raise JevUnavailable("Jev 决策模型未启用（设置 → Jev 决策模型），本轮未参与判定")


# ---------------------------------------------------------------------------
# 主调用
# ---------------------------------------------------------------------------


def _record(
    *,
    model: str,
    purpose: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    outcome: str,
) -> None:
    """落一条 `model_calls` 记录（经 model_client 的公共钩子，与 chat 链路同表同注入点）。

    `total_tokens` 恒记 None：Jev 不提供该字段，而表里 `total_tokens` 的既有语义是
    「服务端口径的合计」。虽然 input+output 是精确算术，但本地加总会让跨协议统计
    `SUM(total_tokens)` 失真——宁可留 null（F01 既有口径：缺失记 null 不猜）。
    """
    emit_model_call_record(
        {
            # Jev 不走方案表，没有 profile_id；表列可空。
            "profile_id": None,
            "model": model,
            "purpose": purpose or "jev:unspecified",
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": None,
            "latency_ms": latency_ms,
            "retried": False,
            "outcome": outcome,
        }
    )


def call_jev(
    config: JevConfig,
    credential_store: Any,
    state: Any,
    questions: Mapping[str, Any],
    *,
    purpose: str | None = None,
    timeout: float | None = None,
    access_enabled: bool = True,
) -> JevReply:
    """发起一次 decisions 调用：多道题并行评估，返回逐题类型化答案。

    - `credential_store` 只要求有 `.get(key_id) -> str | None` 与 `.list_records()`（与 `credential_store.CredentialStore` 对齐）；
    - `purpose` 约定为 `"jev:<场景>"`（如 `jev:tactics-review`、`jev:claim-support`），用于 `model_calls` 归集；
    - `access_enabled` 必须由调用方传入 `core.settings.model_access_enabled`（全局出网总闸）；
    - 超时优先级：显式传入 > `config.timeout_secs` > 内置默认；
    - **不做自动重试**：输入 token 计费，静默重试等于静默双倍计费。429/529 的退避与分片策略
      由批量调用方（JV05/JV08）决定并显式实现。
    """
    _ensure_available(config, access_enabled=access_enabled)

    if not questions:
        raise JevUnavailable("Jev 本次没有题目（questions 为空），调用方应跳过该层")
    for question_id, question in questions.items():
        _validate_question(question_id, question)
    _validate_state(state)

    effective_timeout = float(timeout or config.timeout_secs or _JEV_REQUEST_TIMEOUT)
    requested_model = config.resolved_model
    base_url = config.resolved_base_url
    started = time.monotonic()
    input_tokens: int | None = None
    output_tokens: int | None = None
    reported_model = requested_model

    api_key = resolve_credential(credential_store, config.credential_ref)
    if not api_key:
        latency_ms = int((time.monotonic() - started) * 1000)
        _record(
            model=requested_model,
            purpose=purpose,
            input_tokens=None,
            output_tokens=None,
            latency_ms=latency_ms,
            outcome="error",
        )
        raise JevUnavailable(
            f"Jev 凭据「{config.credential_ref or '（未填写）'}」在本机凭据库中不存在。"
            "三种填法任选：直接粘贴明文密钥（自动加密存入本机凭据库）、"
            "填 key_id（如 jev_api_key）、或填凭据尾号（自动匹配）。"
        )

    try:
        payload = _post_endpoint(
            base_url,
            {
                "state": state,
                "model": requested_model,
                "questions": dict(questions),
            },
            api_key,
            effective_timeout,
        )
        input_tokens, output_tokens = _extract_usage(payload)
        reported = payload.get("model")
        if isinstance(reported, str) and reported.strip():
            reported_model = reported.strip()
        answers = _parse_answers(payload, questions, model=reported_model)
    except JevUnavailable:
        _record(
            model=reported_model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            outcome="error",
        )
        raise
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        reason = getattr(exc, "reason", None)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        # 状态码能带就带：正常路径由 `_post_endpoint` 转成 `JevUnavailable(status_code=…)`，
        # 但万一有异常绕过了那层（HTTPError 逃逸），批量调用方（JV05/JV08）仍需要看到
        # 「可重试 / 不可重试」的信号——靠中文文案猜状态码是错的。
        code = getattr(exc, "code", None)
        status_code = code if isinstance(code, int) else None
        _record(
            model=reported_model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            outcome="timeout" if timed_out else "error",
        )
        if timed_out:
            raise JevUnavailable(
                f"Jev 调用超时（超时 {int(effective_timeout)} 秒已用尽，实际等待 {latency_ms / 1000:.0f}s）。"
                "System One 通常秒级返回，超时多为端点不可达或代理问题——请检查网络与 base_url。"
            ) from exc
        if isinstance(exc, urllib.error.HTTPError):
            raise JevUnavailable(
                _http_error_detail(exc.code, _read_error_body(exc)), status_code=exc.code
            ) from exc
        if isinstance(exc, urllib.error.URLError):
            raise JevUnavailable(
                f"无法连接 Jev 端点（{getattr(exc, 'reason', exc)}）：请检查网络、base_url 与代理设置",
                status_code=status_code,
            ) from exc
        raise JevUnavailable(
            f"Jev 调用失败（{latency_ms}ms），请检查网络与端点配置", status_code=status_code
        ) from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    _record(
        model=reported_model,
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        outcome="ok",
    )
    return JevReply(
        model=reported_model,
        requested_model=requested_model,
        answers=answers,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        purpose=purpose,
    )


# ---------------------------------------------------------------------------
# 草稿态探测（只读）：不读配置表、不写凭据库
#
# 对齐 `model_client.py:363` 的草稿态探测惯例：端点/模型/密钥由调用方直接给出
# （密钥可以是尚未保存的明文），因此「点一下测试」不会产生任何配置副作用。
# ---------------------------------------------------------------------------

_PROBE_STATE = "连通性测试：投资管家正在检查 Jev 端点是否可用。"
_PROBE_QUESTION_ID = "probe_ok"


def _probe_questions() -> dict[str, Any]:
    """中文探测题。

    刻意用中文 state + 中文题：既验证端点，也顺带暴露 R6（CJK 精度）的第一手信号——
    答案应是接近 1.0 的 `noul`，明显偏低即提示中文判定不稳，设置页应如实展示该值。
    """
    return {
        _PROBE_QUESTION_ID: noul_question(
            "这段文本是否提到了「连通性测试」？",
            true_means="文本中出现了「连通性测试」",
            false_means="文本中未出现「连通性测试」",
        )
    }


def probe_systemone(base_url: str, model: str, api_key: str, *, timeout: float) -> JevProbeReply:
    """草稿态连通性探测：用给定连接参数试调一次，返回模型版本、延迟与探测题应答。"""
    started = time.monotonic()
    questions = _probe_questions()
    try:
        payload = _post_endpoint(
            base_url,
            {"state": _PROBE_STATE, "model": (model or "").strip() or JEV_MODEL_ALIAS, "questions": questions},
            api_key or None,
            timeout,
        )
        answers = _parse_answers(payload, questions, model=str(payload.get("model") or model))
    except JevUnavailable:
        raise
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        if isinstance(exc, urllib.error.URLError):
            raise JevUnavailable(
                f"无法连接 Jev 端点（{getattr(exc, 'reason', exc)}）：请检查网络与 base_url"
            ) from exc
        raise JevUnavailable(f"连通性测试失败（{latency_ms}ms）：请检查网络与端点配置") from exc

    input_tokens, output_tokens = _extract_usage(payload)
    reported = payload.get("model")
    return JevProbeReply(
        model=str(reported).strip() if isinstance(reported, str) and reported.strip() else (model or JEV_MODEL_ALIAS),
        requested_model=(model or "").strip() or JEV_MODEL_ALIAS,
        latency_ms=int((time.monotonic() - started) * 1000),
        sample=answers[_PROBE_QUESTION_ID],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def list_jev_models(base_url: str, api_key: str, timeout: float) -> list[str]:
    """拉取可用模型名/别名列表（设置页「拉取模型」按钮，只读、不写任何配置）。

    两种响应形状都要认（网关与官方不同）：

    - 官方 TypeSafe：`{"models": [{"name", "description", "release_date"}]}`；
    - OpenAI/OpenRouter 式：`{"data": [{"id": "typesafe/jev-1.13"}]}`。

    网关的模型列表端点在**另一个路径**（见 `resolve_models_url`）。OpenRouter 上挂着 400+
    模型，所以命中已知网关时只保留该家的（`typesafe/`），否则下拉框没法用——过滤后的结果里
    也会带上被过滤掉的条数提示，不让人误以为网关只有这几个模型。
    """
    url = resolve_models_url(base_url)
    headers = _base_headers()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise JevUnavailable(
            f"拉取模型：{_http_error_detail(exc.code, _read_error_body(exc))}", status_code=exc.code
        ) from exc
    except JevUnavailable:
        raise
    except Exception as exc:
        raise JevUnavailable(f"拉取模型失败：{exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevUnavailable("响应不是合法 JSON：该端点可能不是模型列表接口") from exc
    if not isinstance(parsed, dict):
        raise JevUnavailable("模型列表返回结构异常（非 JSON 对象）")

    entries = parsed.get("models")
    if not isinstance(entries, list):
        entries = parsed.get("data")
    if not isinstance(entries, list):
        raise JevUnavailable("模型列表返回结构异常（既没有 models 数组，也没有 data 数组）")

    models: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
        else:
            name = str(item).strip()
        if name and name not in models:
            models.append(name)

    host = (urllib.parse.urlsplit((base_url or "").strip()).hostname or "").lower()
    prefix = _GATEWAY_MODEL_PREFIX.get(host)
    if prefix:
        models = [name for name in models if name.startswith(prefix)]
    return models


def describe_reply(reply: JevReply) -> str:
    """把应答压成一句可读摘要（用于设置页/日志的**非密钥**回执）。"""
    parts: list[str] = []
    for question_id, answer in reply.answers.items():
        if answer.type == "noul":
            verdict = answer.noul_verdict() or "uncertain"
            parts.append(f"{question_id}=noul:{answer.noul:.2f}({verdict})")
        elif answer.type == "choice":
            confidence = "—" if answer.confidence is None else f"{answer.confidence:.2f}"
            parts.append(f"{question_id}=choice:{answer.choice}(conf {confidence})")
        elif answer.type == "score":
            percent = "—" if answer.score_percent is None else f"{answer.score_percent:.0f}%"
            parts.append(f"{question_id}=score:{answer.score:.2f}/{answer.levels}→{percent}")
    usage = f"in={reply.input_tokens} out={reply.output_tokens}"
    return f"{reply.model} · {reply.latency_ms}ms · {usage} · " + "，".join(parts)
