"""AI 协同流水线（v23）：多角色围绕同一证据包依次串行协作。

用户拍板约束（2026-09-08）：**每个角色不同时进行，必须依次执行** —— 一次 /collab-next
只推进一个角色，角色间通过「上一角色的结构化产出」衔接；数据核对失败不进入初稿，
风险审查失败不进入修订（可对失败阶段重试，重试也是串行的一次调用）。

角色链（默认流水线）：
    check（数据核对员）→ draft（首席初稿）→ risk（风险审查员）→ revise（首席修订）

设计铁律：
- 证据同源：四个角色共享同一次构建的证据包（K 线/新闻/公告/财务 + 失败标注）；
- 失败诚实：沿用 source_errors 口径，角色失败如实标注，不假装成功、不跳过；
- 过程可审计：每个角色的产出、耗时、错误随运行状态返回并持久化最终报告；
- 只做研究文本，不触发任何交易动作。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from investment_steward_core import jev_client, macro_ai, model_client, report_quality
from investment_steward_core.prompting import STOCK_REPORT_RULES_TEXT, STOCK_REPORT_SCHEMA_TEXT

# 角色严格串行：阶段顺序即执行顺序，禁止并行。
STAGES: tuple[str, ...] = ("check", "draft", "risk", "revise")
STAGE_LABELS: dict[str, str] = {
    "check": "数据核对",
    "draft": "首席初稿",
    "risk": "风险审查",
    "revise": "首席修订",
}

_RUN_TTL_SECONDS = 6 * 3600
_MAX_RUNS = 20
_REPORT_BODY_MAX = 6000  # 送入下游角色的报告正文截断（综合/审查输入防爆量，截断处如实标注）

_runs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


class CollabBusyError(Exception):
    """同一运行的上一个阶段仍在执行（协同严格串行，拒绝并发推进）。"""


class CollabRunNotFoundError(Exception):
    """运行不存在或已过期（内存态，重启后失效）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _prune_locked() -> None:
    """超量/过期清理（调用方须已持锁）：协同运行是轻量内存态，最多保留 _MAX_RUNS 份。"""
    now_ts = datetime.now(UTC).timestamp()
    expired = [
        run_id
        for run_id, run in _runs.items()
        if now_ts - datetime.fromisoformat(run["created_at"]).timestamp() > _RUN_TTL_SECONDS
    ]
    for run_id in expired:
        _runs.pop(run_id, None)
    while len(_runs) > _MAX_RUNS:
        oldest = min(_runs, key=lambda rid: _runs[rid]["created_at"])
        _runs.pop(oldest, None)


def create_run(
    symbol: str,
    question: str,
    tech_summary: str,
    sources: dict[str, str],
    source_errors: dict[str, str],
    profile_id: str | None,
    model: str,
    stage_profiles: dict[str, str] | None = None,
    stage_models: dict[str, str] | None = None,
    evidence_meta: dict[str, Any] | None = None,
    report_mode: str | None = None,
) -> dict[str, Any]:
    """创建协同运行：证据包此刻定稿，四角色共享同一快照（证据同源）。

    v23 用户拍板：**不同角色可用不同模型**（stage_profiles: 角色名→方案 id），
    未指定的角色回落到 profile_id（「使用中」方案）；角色执行顺序不变（严格串行）。

    v28：`evidence_meta`（`source_asof` / `peer_count`）随运行快照留存，
    供「来源数据是否过期」判定与首屏结论卡的估值标签共用（研报↔雷达同一口径）。

    v31（2026-09-13 方案 §2.2）：`report_mode` 随运行创建时定稿并全程传递
    （请求 → 质量闸门 → 落库 → 前端展示共用同一值）；未知模式由调用方先经
    `report_mode_budget` 校验（422），本函数不再静默回落。
    """
    normalized_mode, _mode_label, _lo, _hi = report_quality.report_mode_budget(report_mode)
    run: dict[str, Any] = {
        "run_id": str(uuid4()),
        "symbol": symbol,
        "question": question,
        "profile_id": profile_id,
        "model": model,
        "stage_profiles": dict(stage_profiles) if stage_profiles else {},
        "stage_models": dict(stage_models) if stage_models else {},
        "report_mode": normalized_mode,
        "created_at": _now(),
        "evidence": {
            "tech_summary": tech_summary,
            "sources": dict(sources),
            "source_errors": dict(source_errors),
        },
        "evidence_meta": dict(evidence_meta) if evidence_meta else {},
        "source_keys": sorted(sources.keys()),
        "source_errors": dict(source_errors),
        "next_index": 0,
        "stages": [
            {"stage": stage, "label": STAGE_LABELS[stage], "status": "pending", "result": None, "error": None, "latency_ms": None}
            for stage in STAGES
        ],
    }
    with _lock:
        _prune_locked()
        _runs[run["run_id"]] = run
    return run


def get_run(run_id: str) -> dict[str, Any] | None:
    with _lock:
        return _runs.get(run_id)


def is_done(run: dict[str, Any]) -> bool:
    return run["next_index"] >= len(STAGES)


def public_view(run: dict[str, Any]) -> dict[str, Any]:
    """对外视图：不含原始证据全文（前端只关心阶段产出与失败标注）。"""
    return {
        "run_id": run["run_id"],
        "symbol": run["symbol"],
        "question": run["question"],
        "model": run["model"],
        "stage_models": dict(run.get("stage_models") or {}),
        # v31：模式随运行视图下发（请求 → 闸门 → 落库 → 前端共用同一 report_mode）。
        "report_mode": run.get("report_mode") or report_quality.DEFAULT_REPORT_MODE,
        "created_at": run["created_at"],
        "done": is_done(run),
        "next_index": run["next_index"],
        "stages": [dict(stage) for stage in run["stages"]],
        "source_keys": list(run["source_keys"]),
        "source_errors": dict(run["source_errors"]),
        # v29：证据元数据（来源取数时刻/单季序列/价格量能与估值图数据）随运行视图下发；
        # 不含证据全文，与「对外视图不带原始证据」的边界不冲突——图表由前端与单股研报同一组件渲染。
        "evidence_meta": dict(run.get("evidence_meta") or {}),
    }


def _evidence_text(evidence: dict[str, Any]) -> str:
    """把证据包拼成送模型的文本（与个股研报 S1..S5 同一口径）。"""
    sources = evidence.get("sources") or {}
    # 显式 [S#] 前缀，与个股研报端点同一口径（避免模型靠小标题猜编号导致引用错位）。
    numbered = "\n".join(f"[{key}] {value}" for key, value in sources.items())
    errors = evidence.get("source_errors") or {}
    unavailable = ("\n来源不可用（禁止引用、禁止臆测其内容）：" + "；".join(errors.values())) if errors else ""
    tech = evidence.get("tech_summary") or "K 线取数失败，技术面无数据。"
    return f"[S1] {tech}\n\n{numbered}{unavailable}"


# 与个股研报同一份报告 JSON 结构（v24 起由 prompting.STOCK_REPORT_SCHEMA_TEXT 单一维护：
# 此前本文件与 app.py 各存一份近乎相同的模板，已出现措辞漂移；修订稿/初稿共用同一份）。
_REPORT_SCHEMA_TEXT = STOCK_REPORT_SCHEMA_TEXT

_STAGE_SYSTEM: dict[str, str] = {
    "check": (
        "你是数据核对员：只核对给定证据包的完整性与口径，不预测行情、不给投资建议、"
        "不编造证据里不存在的内容。你的输出不构成投资建议。"
    ),
    "draft": (
        "你是严谨的 A 股研究助理：只依据给定来源输出，每条判断必须可溯源；"
        "证据不足时如实说证据不足；宁可给「无数据」也不编造。你的输出不构成投资建议。"
    ),
    "risk": (
        "你是风险审查员：独立对立视角，专门找初稿的漏洞、被忽略的风险与证据不足以支撑的判断；"
        "不迎合初稿结论，禁止空泛套话。你的输出不构成投资建议。"
    ),
    "revise": (
        "你是严谨的 A 股研究助理（修订稿）：在初稿基础上吸收风险审查意见，"
        "采纳或反驳都必须写明证据依据；只依据给定来源，不编造。你的输出不构成投资建议。"
    ),
}


def _report_text(parsed: dict[str, Any] | None) -> str:
    if not isinstance(parsed, dict):
        return "（无）"
    parts = [
        f"标题：{parsed.get('title', '')}",
        f"执行摘要：{parsed.get('executive_summary', '')}",
        f"置信度：{parsed.get('confidence', '')}",
        f"正文：{str(parsed.get('report', ''))[:_REPORT_BODY_MAX]}",
    ]
    return "\n".join(parts)


def _stage_user_prompt(run: dict[str, Any], stage: str) -> str:
    symbol = run["symbol"]
    question = run["question"] or "综合趋势与基本面"
    evidence_block = _evidence_text(run["evidence"])
    head = f"研究对象：A 股 {symbol}。用户关注点：{question}\n\n{evidence_block}"

    if stage == "check":
        return (
            f"{head}\n\n请输出严格 JSON（无 markdown 围栏）：\n"
            '{"observations": ["对每条可用来源的一句话核对结论：内容口径、截至时间、能否支撑研究"], '
            '"gaps": ["缺失或取数失败来源及对研究的具体影响；全部可用时给空数组"], '
            '"readiness": "high|medium|low"}\n'
            "铁律：只描述证据包里真实存在的内容；不得建议拉取不存在的数据源；来源标注失败的必须列入 gaps。"
        )
    if stage == "draft":
        checklist = run["stages"][0].get("result") or {}
        return (
            f"{head}\n\n数据核对员的核对结论（供参考，其判断仍须以证据为准）：\n"
            f"{_dump_json(checklist)}\n\n"
            "你是首席研究员，请基于证据输出研报初稿。请输出严格 JSON（无 markdown 围栏）：\n"
            f"{_REPORT_SCHEMA_TEXT}\n"
            + STOCK_REPORT_RULES_TEXT
        )
    if stage == "risk":
        draft = run["stages"][1].get("result") or {}
        return (
            f"{head}\n\n初稿（首席研究员）：\n{_report_text(draft)}\n\n"
            "请输出严格 JSON（无 markdown 围栏）：\n"
            '{"risks": ["3-6 条具体风险或反证：每条指出依据（哪条证据/哪个缺口），或明确指出初稿某判断证据不足"], '
            '"verdict": "support|partial|against"}\n'
            "铁律：禁止「市场有风险」式空话；每条必须可追溯到证据或证据缺口；不新造证据里不存在的事实。"
        )
    # revise
    draft = run["stages"][1].get("result") or {}
    risk = run["stages"][2].get("result") or {}
    return (
        f"{head}\n\n初稿（首席研究员）：\n{_report_text(draft)}\n\n"
        f"风险审查意见：\n{_dump_json(risk)}\n\n"
        "你是首席研究员，请输出修订稿。请输出严格 JSON（无 markdown 围栏）：\n"
        f"{_REPORT_SCHEMA_TEXT[:-1]}"
        ', "revision_notes": "不超过 3 条：采纳/反驳了风险审查的哪些点、理由（反驳必须给证据依据）"}\n'
        + STOCK_REPORT_RULES_TEXT
        + "\n6. 只针对有依据的批评修订；证据不支持审查意见时可反驳但须写明依据；不编造。"
    )


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _allowed_citations(run: dict[str, Any]) -> set[str]:
    """本次运行**真实取到**的来源 id（证据包在 create_run 时定稿，全角色共享）。

    S1 = 技术面（K 线快照），仅当 K 线取数成功时可引用；S2/S3/S4 取数失败则不在 source_keys 中，
    因此不可被引用——这是判断「假引用」的唯一依据（不猜测、不补全缺失来源）。
    """
    evidence = run.get("evidence") or {}
    return report_quality.available_citations(
        run.get("source_keys") or [], tech_available=bool(evidence.get("tech_summary"))
    )


def execute_next(core: Any, run: dict[str, Any], profile: Any, credential_store: Any) -> dict[str, Any]:
    """执行下一个角色（严格串行：一次只推进一个）。

    - 当前阶段失败（模型调用/解析）→ 标记 failed，**不进入下一阶段**；再次调用本函数会重试该阶段；
    - 同一阶段并发调用 → CollabBusyError（串行约束）；
    - 返回执行完（done 或 failed）的那一个阶段 dict。
    """
    with _lock:
        index = run["next_index"]
        if index >= len(STAGES):
            raise CollabRunNotFoundError("协同运行已完成，没有待执行角色")
        stage = run["stages"][index]
        if stage["status"] == "running":
            raise CollabBusyError("上一角色仍在执行中（协同流水线严格串行，请等待其完成）")
        stage["status"] = "running"

    stage_name = stage["stage"]
    try:
        reply = model_client.call_active_model(
            profile,
            credential_store,
            model_client.format_messages(_STAGE_SYSTEM[stage_name], _stage_user_prompt(run, stage_name)),
         purpose="协同",)
    except model_client.ModelUnavailable as exc:
        stage["status"] = "failed"
        stage["error"] = f"模型调用失败：{exc}"
        return stage
    except Exception as exc:  # noqa: BLE001 - 单角色失败如实标注，不拖垮运行状态
        stage["status"] = "failed"
        stage["error"] = f"角色执行异常：{exc}"
        return stage

    parsed = macro_ai.extract_json_object(reply.content)
    if not _stage_result_valid(stage_name, parsed):
        stage["status"] = "failed"
        stage["error"] = f"{STAGE_LABELS[stage_name]}输出无法解析为约定 JSON（无引用不发布，ADR-0006）。原文片段：{reply.content[:200]}"
        return stage

    # v24 质量闸门：报告类阶段（初稿/修订）的引用必须属于本次真实取到的来源。
    # 虚构引用一律剔除；剔除后为空 → 按「无引用不发布」判该阶段失败（不发布、不落库）。
    if stage_name in ("draft", "revise"):
        allowed = _allowed_citations(run)
        # v28：来源数据日期由创建运行时定格（同一证据包），当天日期按站点既有口径取本机时区。
        meta = run.get("evidence_meta") or {}
        evidence = run.get("evidence") or {}
        quality = report_quality.validate_report(
            parsed,
            allowed=allowed,
            source_asof=meta.get("source_asof") or {},
            today=datetime.now().astimezone().date(),
            # v31：模式随运行传递（创建时已归一化），闸门口径与落库/前端展示一致。
            mode=run.get("report_mode") or report_quality.DEFAULT_REPORT_MODE,
            # JV04：证据同源在此兑现——`sources` 是创建运行时定稿的证据包原文，
            # 四角色共享同一份，语义比对用的正是模型当初看到的那段内容。
            source_texts=evidence.get("sources") or {},
            jev_judge=jev_client.build_judge(core, credential_store, purpose="jev:claim-support"),
        )
        if not quality["citations"]:
            stage["status"] = "failed"
            stage["error"] = (
                f"{STAGE_LABELS[stage_name]}引用的来源 id 均不属于本次真实取到的来源"
                f"（剔除：{'、'.join(quality['dropped_citations'])}；可引用：{'、'.join(quality['available_citations'])}），"
                "按「无引用不发布」不发布（防虚构引用）。"
            )
            return stage
        parsed = {**parsed, "citations": quality["citations"]}
        stage["citation_dropped"] = quality["dropped_citations"]
        stage["quality_warnings"] = quality["warnings"]
        stage["report_chars"] = quality["report_chars"]
        stage["summary_chars"] = quality["summary_chars"]
        stage["report_sections"] = quality["sections"]
        # v31 质量恢复：三态状态 + 阻断项 + 缺失小节随阶段留痕（落库与前端展示共用同一口径）。
        stage["quality_status"] = quality["quality_status"]
        stage["quality_status_label"] = quality["quality_status_label"]
        stage["quality_blockers"] = quality["quality_blockers"]
        stage["missing_sections"] = quality["missing_sections"]
        stage["section_states"] = quality["section_states"]
        # v27：claim→source 覆盖结论与证据分级随阶段留痕（落库与前端展示共用同一口径）。
        stage["claim_findings"] = quality["claim_findings"]
        # JV04：语义支撑层回执（未启用/不可用时为 available=False，如实标注而非静默）。
        stage["jev"] = quality.get("jev")
        stage["evidence_quality"] = quality["evidence_quality"]
        # v28：一句话结论 + 摘要降级提示随阶段留痕（首屏结论卡与摘要旁注的数据来源）。
        stage["conclusion"] = quality["conclusion"]
        stage["summary_downgrade_notes"] = quality["summary_downgrade_notes"]
        # 兼容旧断言字段名：parsed 里的 claims 保留原始条目，便于前端逐条对照正文。
        parsed = {**parsed, "claims": quality["claims"]}

    stage["status"] = "done"
    stage["result"] = parsed
    stage["latency_ms"] = reply.latency_ms
    with _lock:
        run["next_index"] = index + 1
    return stage


def _stage_result_valid(stage_name: str, parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    if stage_name == "check":
        return isinstance(parsed.get("observations"), list) or isinstance(parsed.get("gaps"), list)
    if stage_name in ("draft", "revise"):
        return isinstance(parsed.get("report"), str) and bool(parsed.get("citations"))
    if stage_name == "risk":
        return isinstance(parsed.get("risks"), list)
    return False


# ---------------------------------------------------------------------------
# v23 阶段 0：多模型对比结果的跨报告综合（共识 / 分歧 / 待核验）。
# 不投票、不平均：分歧如实陈列，由用户自行裁决。
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "你是研究综合员：综合多份针对不同标的或来自不同模型的研究报告，"
    "提炼共识与分歧；分歧必须如实陈列，禁止强行调和或投票裁决。"
    "只基于给定报告内容，不新造事实。你的输出不构成投资建议。"
)


def synthesis_messages(
    items: list[dict[str, Any]],
    question: str,
) -> tuple[str, str]:
    """构建跨报告综合的 (system, user) 消息对。items 为已完成报告的摘要列表。"""
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        body = str(item.get("report", ""))[:_REPORT_BODY_MAX]
        blocks.append(
            f"【报告 {index}】标的 {item.get('symbol', '未知')} · 模型 {item.get('model', '未知')}"
            f" · 置信 {item.get('confidence', '未知')}\n"
            f"执行摘要：{item.get('executive_summary', '')}\n"
            f"正文（超长截断）：\n{body}"
        )
    user = (
        (f"用户关注点：{question}\n\n" if question else "")
        + "\n\n".join(blocks)
        + "\n\n请输出严格 JSON（无 markdown 围栏）：\n"
        '{"summary": "综合结论（150 字以内：整体方向与最重要的提示）", '
        '"consensus": ["多份报告一致认同的判断（每条注明哪些报告一致）"], '
        '"divergences": ["分歧点：写明分歧主题与各方立场（如「A 模型认为…；B 模型认为…」）"], '
        '"to_verify": ["需要用户人工核验的事项（数据缺口、冲突证据、时效风险）"]}\n'
        "铁律：只综合给定报告的内容；报告间结论冲突时进 divergences，不进 consensus；不投票、不平均。"
    )
    return _SYNTHESIS_SYSTEM, user


def parse_synthesis(text: str) -> dict[str, Any] | None:
    parsed = macro_ai.extract_json_object(text)
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("summary"), str) or not parsed["summary"].strip():
        return None
    return {
        "summary": parsed["summary"].strip(),
        "consensus": [str(item) for item in parsed.get("consensus", []) if str(item).strip()],
        "divergences": [str(item) for item in parsed.get("divergences", []) if str(item).strip()],
        "to_verify": [str(item) for item in parsed.get("to_verify", []) if str(item).strip()],
    }
