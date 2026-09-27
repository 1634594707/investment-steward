"""Jev 决策模型域路由（JV02）：System One 的另一套协议，**不进** chat 方案表。

为什么单开一域而不是塞进 model-profiles：Jev 走 `POST {base_url}/systemone`，返回类型化
answers（无正文生成、无需解析模型散文），与 chat/completions 是两套协议。`ModelProfile`
的「同一时刻恰好一个使用中」语义只属于 chat 链路，混进来会让两边都讲不清。
密钥同样不在本域任何响应里出现——只存凭据库引用。

A06（架构改进路线图 2026-09-25）第二批：把 4 条路由与它们的 3 个辅助从 `api/app.py`
整体迁出。**只搬位置、不改行为**：函数体、状态码与响应形状逐字保留；迁移前后的真实
路由清单由 `scripts/route_inventory.py` 比对，必须完全一致。

`effective_jev_settings` 是**公开**的：战法雷达（`tactics_ai_review`）也按同一优先级取配置，
所以它不属于本域的私有实现——原先两处各写一遍迟早漂移成两个口径，收敛到 `jev_client` 一处。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from investment_steward_core import jev_client, model_client
from investment_steward_core.api.deps import collaborators
from investment_steward_core.credential_store import JEV_API_KEY
from investment_steward_core.domain import JevSettings


class JevSettingsRequest(BaseModel):
    """Jev 决策模型配置保存负载（JV02）。

    密钥**不在本负载里**——只接受 `credential_ref`（凭据库 key_id / 凭据尾号）。
    明文密钥走既有 `/credentials/{key_id}` 通道加密入库，避免多开一个明文入口。
    """

    enabled: bool = False
    base_url: str = Field(default="https://api.typesafe.ai/v1", min_length=1, max_length=300)
    model: str = Field(default="jev-latest", max_length=120)
    credential_ref: str = Field(default="", max_length=120)
    timeout_secs: float = Field(default=60.0, ge=5, le=600)

    # 注意：这两个校验器是端点 422 行为的唯一来源。迁移时漏掉它们会让非法 base_url /
    # 空模型名一路走到 `JevSettings(...)` 才炸成 500——A06 迁移第一版就踩过这个坑，
    # 由 `test_jev_config.py::test_put_rejects_invalid_payload` 抓住。
    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        text = (value or "").strip()
        if not text.startswith(("http://", "https://")):
            raise ValueError("Jev base_url 必须以 http:// 或 https:// 开头")
        return text

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("Jev 模型名不能为空（官方默认别名 jev-latest）")
        return text


class JevProbeRequest(BaseModel):
    """Jev 草稿态探测（设置页「测连通性 / 拉取模型」）：只读，不落库、不写凭据。

    与 chat 侧 `ModelProbeRequest` 同惯例：`credential_ref` 允许是**尚未保存的明文密钥**
    （先试后存），任何字段都不会被持久化。
    """

    base_url: str = Field(default="https://api.typesafe.ai/v1", min_length=1, max_length=300)
    model: str = Field(default="jev-latest", max_length=120)
    credential_ref: str = Field(default="", max_length=500)
    timeout_secs: float | None = Field(default=None, ge=5, le=600)


def effective_jev_settings(core: Any) -> JevSettings:
    """生效配置：设置页保存值（jev_config 表）> STEWARD_JEV_* 环境变量 > 内置默认。

    实现在 `jev_client.effective_settings` —— 协同流水线（`collab.py`）与战法雷达也要按同一
    优先级取配置，两处各写一遍迟早漂移成两个口径，故收敛到一处。
    """
    return jev_client.effective_settings(core)


def _normalize_jev_credential_ref(store: Any, ref: str) -> tuple[str, str | None]:
    """把「用户粘贴的明文密钥 / 凭据尾号」规范成可长期保存的 `credential_ref`。

    返回 `(引用, 说明)`；说明非 None 时前端要如实告诉用户密钥被搬到了哪里。

    为什么在**服务端**做转换（chat 侧是在 `SettingsPage` 前端转换的）：
    1. `call_jev` 用 `resolve_credential` 解析密钥，它**只认 key_id 与尾号，不认明文**。
       明文原样存进 `jev_config.payload` 会造出一个很坏的错位——「测连通性」通过
       （探测走 `resolve_probe_api_key`，明文可直接用），但协同流水线一调用就
       「凭据解析失败」。用户会以为 Jev 装好了，其实一次都没跑成。
    2. 明文密钥落 `jev_config.payload` 直接违反 P0 验收项「密钥零泄漏」。前端文案
       已承诺「不会写进本配置」，那服务端就必须真的做到——承诺不能只靠调用方自觉。

    尾号分支与 `resolve_credential` 的尾号兜底同口径：单人本机产品，允许用户凭直觉填尾号。
    """
    cleaned = (ref or "").strip()
    if not cleaned:
        return "", None
    if model_client.looks_like_plaintext_key(cleaned):
        record = store.store(JEV_API_KEY, cleaned)
        return JEV_API_KEY, (
            f"检测到明文密钥，已存入本机凭据库（key_id={record.key_id}，尾号 {record.last4}）——"
            "配置里只留 key_id，明文不进 jev_config 表。"
        )
    try:
        records = store.list_records()
    except Exception:  # noqa: BLE001 - 凭据层不可用时按原样保存，不阻断设置页
        records = []
    if not any(record.key_id == cleaned for record in records):
        matched = next(
            (record for record in records if (record.last4 or "").upper() == cleaned.upper()),
            None,
        )
        if matched is not None:
            return matched.key_id, f"按凭据尾号匹配到 key_id={matched.key_id}，已改写为 key_id 保存。"
    return cleaned, None


def _jev_probe_key(core: Any, ref: str) -> tuple[str, dict[str, object] | None]:
    """解析探测用密钥：返回 (api_key, 失败响应)。明文密钥可直接试（尚未保存也要能先测）。

    凭据后端经 `collaborators()` 按调用时取：测试统一 patch `api.app.resolve_store`
    把后端钉死成文件兜底，且多数在 create_app 之后才 patch。
    """
    store = collaborators().resolve_store(core.database)
    cleaned = (ref or "").strip()
    api_key = model_client.resolve_probe_api_key(store, cleaned)
    if cleaned and not api_key:
        return "", {
            "ok": False,
            "latency_ms": 0,
            "detail": (
                f"凭据引用「{cleaned}」在本机凭据库中不存在，也不像明文密钥；"
                "请粘贴密钥、填 key_id（如 jev_api_key）或填凭据尾号。"
            ),
        }
    return api_key, None


def build_jev_router(
    require_session: Callable[..., Any],
    audit: Callable[..., None],
) -> APIRouter:
    """构造 Jev 决策模型路由（依赖由 `api/app.py` 注入，见 `api/deps.py` 说明）。"""
    router = APIRouter()

    @router.get("/jev/config", response_model=dict[str, object])
    def get_jev_config(core: Any = Depends(require_session)) -> dict[str, object]:
        """读取 Jev 配置 + 官方协议上限 + 数据出网说明（设置页据此渲染与提示）。"""
        settings = effective_jev_settings(core)
        return {
            "ok": True,
            "config": settings.model_dump(mode="json"),
            "saved": core.database.get_jev_settings(core.local_user_id) is not None,
            "access_enabled": core.settings.model_access_enabled,
            "schema_version": jev_client.JEV_QUESTION_SCHEMA_VERSION,
            "defaults": {
                "base_url": core.settings.jev_base_url,
                "model": core.settings.jev_model,
                "timeout_secs": core.settings.jev_timeout_secs,
            },
            "limits": {
                "choice_max_options": jev_client.JEV_CHOICE_MAX_OPTIONS,
                "score_min_levels": jev_client.JEV_SCORE_MIN_LEVELS,
                "score_max_levels": jev_client.JEV_SCORE_MAX_LEVELS,
            },
            # noul 没有 confidence，阈值在代码里——把当前口径如实暴露给界面，
            # 避免界面上出现「置信度」这种 Jev 根本不返回的字段。
            "noul_thresholds": {"yes": jev_client.JEV_NOUL_YES, "no": jev_client.JEV_NOUL_NO},
            "data_handling": {
                "trains_on_input": False,
                "zero_data_retention": "enterprise_only",
                # 立场（用户 2026-09-21 拍板）：**不申请**企业版 ZDR。
                # 这不是「厂商不给」，是本项目主动不申请——所以白名单必须按最严口径执行，
                # 不得因为「以后也许能拿到 ZDR」而放宽 state。如实暴露给界面，避免读者误以为已有 ZDR。
                "zdr_applied": False,
                "retention": "无固定期限（官方表述为按提供服务之合理必要期间保留）",
                "hosted_in": "美国",
            },
            "notes": [
                (
                    "开启后 state 会出网到 TypeSafe（美国托管）：官方声明不用客户输入训练模型，"
                    "但默认非零留存且无固定保留期，零数据留存（ZDR）仅企业版——**本项目已决定不申请 ZDR**，"
                    "故 state 白名单按最严口径执行。"
                ),
                "计费只算输入 token（输出免费），state 越精简越省——与「state 数据最小化」是同一条约束。",
                "官方声明英文精度最佳，中文（CJK）可处理但精度较低：中文场景的判定阈值须用自有样本重新标定后再依赖。",
                (
                    "Jev 只做「是/否、选哪个、打几分」的原子判断；判定一律作为软校验/分诊层——"
                    "只标注、降级、排序，不改写模型原话、不阻断交付。"
                ),
            ],
        }

    @router.put("/jev/config", response_model=dict[str, object])
    def update_jev_config(
        body: JevSettingsRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        """保存 Jev 配置（明文密钥自动转入凭据库，配置表只留 key_id）。"""
        existing = core.database.get_jev_settings(core.local_user_id)
        credential_ref, credential_note = _normalize_jev_credential_ref(
            collaborators().resolve_store(core.database), body.credential_ref
        )
        settings = JevSettings(
            user_id=core.local_user_id,
            enabled=body.enabled,
            base_url=body.base_url,
            model=body.model,
            credential_ref=credential_ref,
            timeout_secs=body.timeout_secs,
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        core.database.upsert_jev_settings(settings)
        audit(core, "jev_config.updated", "jev_config", str(core.local_user_id))
        return {
            "ok": True,
            "saved": True,
            "config": settings.model_dump(mode="json"),
            "credential_note": credential_note,
        }

    @router.post("/jev/probe", response_model=dict[str, object])
    def probe_jev_connection(
        body: JevProbeRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        """草稿态「测连通性」：只读，不读配置表、不写凭据库。

        与 chat 侧 `model-profiles/probe` 同惯例——探测**不落 `model_calls`**（那边也不落），
        因为它不是流水线调用而是用户主动的一次性自检。
        """
        if not core.settings.model_access_enabled:
            return {
                "ok": False,
                "latency_ms": 0,
                "detail": "模型出网总闸已关闭（STEWARD_MODEL_ACCESS=0），无法真实测试",
            }
        api_key, failure = _jev_probe_key(core, body.credential_ref)
        if failure is not None:
            return failure
        timeout = float(body.timeout_secs or jev_client.JEV_REQUEST_TIMEOUT)
        try:
            reply = jev_client.probe_systemone(body.base_url, body.model, api_key, timeout=timeout)
        except jev_client.JevUnavailable as exc:
            return {"ok": False, "latency_ms": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都不让设置页白屏
            return {"ok": False, "latency_ms": 0, "detail": f"连通性测试失败：{exc}"}

        sample = reply.sample
        verdict = sample.noul_verdict() or "uncertain"
        return {
            "ok": True,
            "latency_ms": reply.latency_ms,
            "detail": (
                f"真实调用成功（模型 {reply.model}，{reply.latency_ms}ms，"
                f"输入 {reply.input_tokens} / 输出 {reply.output_tokens} token）。"
                f"中文探测题回执 noul={sample.noul:.2f}（{verdict}）——"
                "明显偏离 1 提示中文判定不稳，中文场景阈值须按自有样本重新标定。"
            ),
            "model": reply.model,
            "requested_model": reply.requested_model,
            "probe_noul": sample.noul,
            "probe_verdict": verdict,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        }

    @router.post("/jev/models", response_model=dict[str, object])
    def discover_jev_models(
        body: JevProbeRequest, core: Any = Depends(require_session)
    ) -> dict[str, object]:
        """「拉取模型」：读官方 `GET /models`（只读，不落库、不写凭据）。

        ⚠️ 官方返回形状是 `{"models": [{"name", "description", "release_date"}]}`，
        与 OpenAI 兼容端点的 `{"data": [{"id"}]}` 不同——客户端按官方形状解析。
        """
        if not core.settings.model_access_enabled:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": "模型出网总闸已关闭（STEWARD_MODEL_ACCESS=0），无法拉取"}
        api_key, failure = _jev_probe_key(core, body.credential_ref)
        if failure is not None:
            return {**failure, "models": []}
        timeout = float(body.timeout_secs or jev_client.JEV_REQUEST_TIMEOUT)
        try:
            models = jev_client.list_jev_models(body.base_url, api_key, timeout)
        except jev_client.JevUnavailable as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "models": [], "latency_ms": 0, "detail": f"拉取模型失败：{exc}"}
        return {"ok": True, "models": models, "latency_ms": 0, "detail": f"拉取到 {len(models)} 个模型/别名。"}

    return router
