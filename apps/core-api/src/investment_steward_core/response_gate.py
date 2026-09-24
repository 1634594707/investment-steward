"""回答闸门（阶段 B）：模型原始文本 → 结构化 AgentResponse，并做引用真实性校验。

职责（全部纯函数，给定证据集即可测，不触达数据库）：
- B1 从模型输出中抽取 JSON 并校验为 `AgentResponse`（pydantic）；
- B2 引用真实性：`supporting_refs` / `contradicting_refs` 中的证据 id 必须已存在于证据账本，
  不存在的引用被剔除并写入 `missing_information`，**绝不放行虚构引用**。

约定：模型文本可能夹带代码围栏 / 前后缀文字，本模块做宽容抽取（取首个合法 JSON 对象）；
抽取与校验失败以 `ResponseGateError` 抛出，由调用方走阶段 B3 降级链（如实记录、不伪装成模型产出）。
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from investment_steward_core.domain import ActionMode, AgentResponse, Evidence

# 兼容 JSON 注释：模型常输出 // 或 # 注释，宽容抽取前先剥离但不改字符串字面量（先做保守的关键字保护）。
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ResponseGateError(Exception):
    """模型输出无法通过闸门：非 JSON / 非 AgentResponse / 字段缺失 / 引用校验失败。"""


def _strip_code_fence(text: str) -> str:
    block = _JSON_BLOCK.search(text)
    return block.group(1) if block else text


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出中定位首个可解析的 JSON 对象并返回 dict；失败抛 ResponseGateError。"""
    # 先整体尝试（模型可能裸输出 JSON）
    candidates = [text] + [text.lstrip()]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass

    # 逐个候选起点使用 JSONDecoder.raw_decode，按 JSON 自己的字符串/嵌套
    # 规则寻找平衡对象；不能用首个 { 到末个 }，否则后缀杂文或第二个对象会串入。
    decoder = json.JSONDecoder()
    start = 0
    while True:
        start = text.find("{", start)
        if start < 0:
            break
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
        start += 1
    raise ResponseGateError("无法从模型输出中抽取合法的 JSON 对象")


def _normalize_uuids(values: list[Any]) -> list[UUID]:
    seen: set[str] = set()
    out: list[UUID] = []
    for value in values:
        try:
            uid = UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            continue
        key = str(uid)
        if key not in seen:
            seen.add(key)
            out.append(uid)
    return out


def _reference_known(refs: list[UUID], known_evidence_ids: set[UUID]) -> tuple[list[UUID], list[UUID]]:
    """返回 (保留的真实引用, 剔除的未知引用)。"""
    kept: list[UUID] = []
    dropped: list[UUID] = []
    for uid in refs:
        (kept if uid in known_evidence_ids else dropped).append(uid)
    return kept, dropped


def build_gate_payload(raw_model_text: str, known_evidence: list[Evidence], *, run_id: UUID, user_id: UUID) -> dict[str, Any]:
    """从模型文本抽取并规整成构造 AgentResponse 所需的 payload（含引用真实性过滤后结果）。

    调用方（research 链路）用返回的 dict 组装实际 `AgentResponse`（填充 ID/时间等）。
    """
    raw = _strip_code_fence(raw_model_text)
    obj = _extract_json_object(raw)
    return normalize_gate_payload(obj, known_evidence, run_id=run_id, user_id=user_id)


def normalize_gate_payload(
    obj: dict[str, Any],
    known_evidence: list[Evidence],
    *,
    run_id: UUID,
    user_id: UUID,
) -> dict[str, Any]:
    """规整模型 JSON 为 AgentResponse 构造 payload：引用真实性过滤 + 字段默认/强校验。"""
    known_ids = {item.evidence_id for item in known_evidence}

    for key, expected in (("run_id", run_id), ("user_id", user_id)):
        if key not in obj:
            continue
        try:
            actual = UUID(str(obj[key]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ResponseGateError(f"模型回答 {key} 不是合法 UUID") from exc
        if actual != expected:
            raise ResponseGateError(f"模型回答 {key} 与当前请求不一致")

    supporting_raw = obj.get("supporting_refs", [])
    contradicting_raw = obj.get("contradicting_refs", [])
    supporting = _normalize_uuids(supporting_raw if isinstance(supporting_raw, list) else [])
    contradicting = _normalize_uuids(contradicting_raw if isinstance(contradicting_raw, list) else [])

    kept_supporting, dropped_supporting = _reference_known(supporting, known_ids)
    kept_contradicting, dropped_contradicting = _reference_known(contradicting, known_ids)

    # evidence_refs 取支持+相反并集，且只保留已知证据；历史 evidence_refs 语义为「本回答引用」，
    # 未知引用一律剔除（不进入 response）。
    known_evidence_only = _dedupe([*kept_supporting, *kept_contradicting])
    dropped_all = dropped_supporting + dropped_contradicting

    missing_bits = _str_list(obj.get("missing_information", []))
    if dropped_all:
        missing_bits.append(
            f"剔除 {len(dropped_all)} 条模型引用的、本地账本中不存在或非本回答关联的证据 id，未纳入结论（防止虚构引用）。"
        )

    summary = _require_text(obj, "summary", "模型回答缺少 summary")
    confidence = _require_text(obj, "confidence_description", "模型回答缺少 confidence_description")
    action_mode_raw = obj.get("action_mode")
    if not action_mode_raw:
        raise ResponseGateError("模型回答缺少 action_mode（四种行动模式之一）")
    try:
        action_mode = ActionMode(str(action_mode_raw))
    except ValueError as exc:
        raise ResponseGateError(f"未知 action_mode：{action_mode_raw}") from exc

    supported_raw = obj.get("supported_actions", [])
    allowed_actions = {"open_evidence", "create_plan", "start_learning", "review_policy"}
    if supported_raw is not None and not isinstance(supported_raw, list):
        raise ResponseGateError("supported_actions 必须是数组")
    supported_actions = _str_list(supported_raw)
    unknown_actions = [value for value in supported_actions if value not in allowed_actions]
    if unknown_actions:
        raise ResponseGateError(f"未知 supported_actions：{', '.join(unknown_actions)}")
    if not supported_actions:
        supported_actions = {
            ActionMode.OBSERVE: ["open_evidence"],
            ActionMode.RESEARCH: ["create_plan"],
            ActionMode.REVIEW_PLAN: ["review_policy"],
            ActionMode.NO_ACTION: [],
        }[action_mode]

    return {
        "run_id": run_id,
        "user_id": user_id,
        "user_question": _require_text(obj, "user_question", "模型回答缺少 user_question"),
        "summary": summary,
        "reasoning_outline": _str_list(obj.get("reasoning_outline", [])),
        "confidence_description": confidence,
        "evidence_refs": known_evidence_only,
        "supporting_refs": kept_supporting,
        "contradicting_refs": kept_contradicting,
        "missing_information": missing_bits,
        "freshness_warning": _str_list(obj.get("freshness_warning", [])),
        "limitations": _str_list(obj.get("limitations", [])),
        "action_mode": action_mode,
        "supported_actions": supported_actions,
        "model_provider": obj.get("model_provider"),
        "model_name": obj.get("model_name"),
        "prompt_policy_version": obj.get("prompt_policy_version", "1.0"),
        "tool_calls": _str_list(obj.get("tool_calls", [])),
        "plugin_runs": _str_list(obj.get("plugin_runs", [])),
    }


def _dedupe(values: list[UUID]) -> list[UUID]:
    seen: set[str] = set()
    out: list[UUID] = []
    for uid in values:
        key = str(uid)
        if key not in seen:
            seen.add(key)
            out.append(uid)
    return out


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _require_text(obj: dict[str, Any], key: str, message: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResponseGateError(message)
    return value.strip()


def assemble_agent_response(payload: dict[str, Any], *, response_id: UUID) -> AgentResponse:
    """用闸门规整后的 payload 构造 AgentResponse（真正的 pydantic 强校验在此发生）。"""
    return AgentResponse(
        response_id=response_id,
        run_id=payload["run_id"],
        user_id=payload["user_id"],
        user_question=payload["user_question"],
        summary=payload["summary"],
        reasoning_outline=payload["reasoning_outline"],
        confidence_description=payload["confidence_description"],
        evidence_refs=payload["evidence_refs"],
        supporting_refs=payload["supporting_refs"],
        contradicting_refs=payload["contradicting_refs"],
        missing_information=payload["missing_information"],
        freshness_warning=payload["freshness_warning"],
        limitations=payload["limitations"],
        action_mode=payload["action_mode"],
        supported_actions=payload.get("supported_actions", []),
        model_provider=payload["model_provider"],
        model_name=payload["model_name"],
        prompt_policy_version=payload["prompt_policy_version"],
        tool_calls=payload["tool_calls"],
        plugin_runs=payload["plugin_runs"],
    )
