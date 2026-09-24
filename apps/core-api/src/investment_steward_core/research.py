from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investment_steward_core import jev_client
from investment_steward_core.domain import (
    ActionMode,
    AgentResponse,
    Evidence,
    EvidenceRelation,
    InvestorProfile,
    JevRouteDecision,
    ResearchRun,
    RunStatus,
)
from investment_steward_core.evidence_policy import as_utc, is_current_evidence
from investment_steward_core.prompting import RESEARCH_PROMPT_VERSION
from investment_steward_core.response_gate import (
    ResponseGateError,
    assemble_agent_response,
    build_gate_payload,
)
from investment_steward_core.storage import Database

_LOCAL_TOKENIZE = str.split


def _tokens(text: str) -> set[str]:
    """中英文混合的轻量 token 化：保留空格词，并为中文生成 2~5-gram。"""
    normalized = re.sub(r"\s+", "", text or "").lower()
    if not normalized:
        return set()
    tokens = {part for part in re.findall(r"[a-z0-9_]+", normalized) if len(part) >= 2}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    for size in (2, 3, 4, 5):
        tokens.update(cjk[index : index + size] for index in range(len(cjk) - size + 1))
    # 也保留完整的短标识（如沪深300ETF 中的数字/字母组合）。
    tokens.update(part for part in re.findall(r"[\u3400-\u9fff]{1,}[a-z0-9]+|[a-z0-9]+[\u3400-\u9fff]{1,}", normalized))
    return tokens


def _question_tokens(question: str) -> set[str]:
    """问题关键词：去掉空白后切成最小 token 集，用于证据匹配、不入库。"""
    return _tokens(question)


def _evidence_tokens(evidence: Evidence) -> set[str]:
    tokens: set[str] = set()
    for subject in evidence.subject_refs:
        tokens.update(_tokens(subject))
    tokens.update(_tokens(evidence.summary))
    if evidence.source_name:
        tokens.update(_tokens(evidence.source_name))
    return tokens


def select_relevant_evidence(question: str, evidence: list[Evidence]) -> list[Evidence]:
    """按问题 token 与证据主体/摘要的命中交集筛选相关证据；无命中则视为无关。"""
    if not question.strip():
        return []
    q = _question_tokens(question)
    relevant: list[Evidence] = []
    for item in evidence:
        if not is_current_evidence(item):
            continue
        overlap = q & _evidence_tokens(item)
        if overlap:
            relevant.append(item)
    return relevant


def _advance(db: Database, run: ResearchRun, target: RunStatus) -> ResearchRun:
    return db.transition_research_run(run.run_id, run.user_id, target)


def _synthesize(
    db: Database,
    run: ResearchRun,
    question: str,
    supporting: list[Evidence],
    contradicting: list[Evidence],
    unknown: list[Evidence],
    *,
    profile: InvestorProfile | None = None,
) -> AgentResponse:
    """确定性证据综合：只陈述证据可支撑的内容，缺失部分明确写入 missing_information，不编造结论。"""
    supporting_refs = [item.evidence_id for item in supporting]
    contradicting_refs = [item.evidence_id for item in contradicting]
    unknown_refs = [item.evidence_id for item in unknown]
    all_refs = supporting_refs + contradicting_refs + unknown_refs

    reasoning: list[str] = []
    if supporting:
        reasoning.append(f"在本地证据账本中找到 {len(supporting)} 条与该问题相关的支持证据。")
    if contradicting:
        reasoning.append(f"同时存在 {len(contradicting)} 条相反证据，不能忽略矛盾。")
    if unknown:
        reasoning.append(f"{len(unknown)} 条证据关系未知，暂不归入支持或相反。")
    if not all_refs:
        reasoning.append("本地证据账本中没有与该问题直接匹配的条目。")

    if not all_refs:
        summary = "当前本地证据账本中没有与该问题直接相关的证据，无法形成有据结论。请补充数据供应商或先入库相关证据后再作答。"
        action_mode = ActionMode.RESEARCH
        supported_actions = ["create_plan"]
        confidence = "无证据可评估，结论不成立（证据缺失）。"
        missing = ["与本问题直接相关的证据条目缺失（行情、公告或财报）。", "证据占比为零，未调用外部模型，避免编造。"]
    elif contradicting:
        summary = (
            f"证据存在矛盾：{len(supporting)} 条支持 vs {len(contradicting)} 条相反。"
            "在矛盾澄清前，不应对该问题做单边判断。"
        )
        action_mode = ActionMode.RESEARCH
        supported_actions = ["create_plan"]
        confidence = f"支持与相反证据并存（{len(supporting)} v {len(contradicting)}），判断置信度低，需进一步研究。"
        missing = ["能够区分支持与相反证据的确凿新证据。"]

    else:
        summary = f"本地证据账本中有 {len(supporting)} 条支持证据、无相反证据，可给出倾向性判断。"
        action_mode = ActionMode.OBSERVE
        supported_actions = ["open_evidence"]
        confidence = f"支持证据 {len(supporting)} 条、相反证据 0 条，判断置信度为中等（样本有限）。"
        missing = ["更长观测窗口的后续证据，以持续验证原判断。"]

    fresh_warnings = [
        f"{item.source_name} 证据采集于 {item.collected_at.date().isoformat()}，若已过期请复核。"
        for item in supporting + contradicting + unknown
        if item.valid_until is not None and as_utc(item.valid_until) < datetime.now(UTC)
    ]

    profile_notes = [
        f"已按投资者画像提供参考视角（投资目标：{profile.investment_goal}）"
        if profile and profile.investment_goal
        else "未建立投资者画像，未提供个性化视角。",
    ]
    if profile and profile.horizon_years is not None:
        profile_notes.append(f"画像中投资期限为 {profile.horizon_years:g} 年，结论未据此给出买卖建议。")

    response = AgentResponse(
        response_id=uuid4(),
        run_id=run.run_id,
        user_id=run.user_id,
        user_question=question,
        summary=summary,
        reasoning_outline=reasoning,
        confidence_description=confidence,
        evidence_refs=all_refs,
        supporting_refs=supporting_refs,
        contradicting_refs=contradicting_refs,
        missing_information=missing,
        freshness_warning=fresh_warnings,
        limitations=[
            "回答由本地确定性规则引擎基于当前证据账本合成，未调用外部模型，不含买卖建议。",
            "相关性由问题与证据主题的 token 重叠判定，可能遗漏语义相关但用词不同的证据。",
        ]
        + profile_notes,
        action_mode=action_mode,
        supported_actions=supported_actions,
        model_provider="local-deterministic",
        model_name="steward-evidence-synthesizer",
        prompt_policy_version=RESEARCH_PROMPT_VERSION,
        plugin_runs=[f"evidence-ledger@{run.user_id}"],
        tool_calls=["read_evidence_ledger"],
    )
    return response


def run_research(
    db: Database,
    run: ResearchRun,
    *,
    model_client: object | None = None,
    action_router: JevShardReviewer | None = None,
) -> AgentResponse:
    """研究链路编排：planning → collecting_evidence → analyzing → composing → completed。

    双引擎：若提供 `model_client`（含激活方案与凭据解析能力）且可用，则先尝试模型路径经
    回答闸门（response_gate）；任何不可用 / 校验失败 / 引用校验失败 → 回退本地确定性引擎
    （`_synthesize`），并在 AgentResponse 上如实标注真实 provider 与降级原因（不伪装）。
    `model_client` 留空则纯本地引擎（既有契约）。失败路径将运行标记为 failed。

    **JV07**：`action_router` 为行动模式预判回调（由调用方注入，**本模块不出网**）。它只决定
    「要不要跳过完整研究链路」，**不改写** `action_mode`；留空则整层不跑（既有契约）。
    """
    question = run.user_question
    evidence = db.list_evidence(run.user_id)
    profile = db.get_investor_profile(run.user_id)

    plan = _advance(db, run, RunStatus.PLANNING)
    collecting = _advance(db, plan, RunStatus.COLLECTING_EVIDENCE)

    try:
        relevant = select_relevant_evidence(question, evidence)
        supporting = [item for item in relevant if item.relation == EvidenceRelation.SUPPORTING]
        contradicting = [item for item in relevant if item.relation == EvidenceRelation.CONTRADICTING]
        unknown = [item for item in relevant if item.relation == EvidenceRelation.UNKNOWN]

        analyzing = _advance(db, collecting, RunStatus.ANALYZING)
        composing = _advance(db, analyzing, RunStatus.COMPOSING)

        response = _answer(
            db, composing, question, supporting, contradicting, unknown,
            model_client=model_client, profile=profile, action_router=action_router,
        )
        db.upsert_agent_response(response)

        done = db.transition_research_run(run.run_id, run.user_id, RunStatus.COMPLETED)
        linked = done.model_copy(update={"response_id": response.response_id, "updated_at": datetime.now(UTC)})
        db.upsert_research_run(linked)
        return response
    except Exception:
        try:
            _advance(db, collecting, RunStatus.FAILED)
        except ValueError:
            pass
        raise


def _answer(
    db: Database,
    run: ResearchRun,
    question: str,
    supporting: list[Evidence],
    contradicting: list[Evidence],
    unknown: list[Evidence],
    *,
    model_client: object | None,
    profile: InvestorProfile | None = None,
    action_router: JevShardReviewer | None = None,
) -> AgentResponse:
    """引擎选择：模型优先（存在且可用），否则本地确定性。失败降级集中在 `_try_model`。

    **JV07 路由**：只在「本来会走模型路径」时预判才有意义——`model_client` 为 None（模型出网
    关闭）时本来就走本地引擎，预判既省不下钱又要多花一次 Jev 调用。
    """
    route = None
    if action_router is not None and model_client is not None:
        route = jev_action_route(
            reviewer=action_router,
            question=question,
            supporting_count=len(supporting),
            contradicting_count=len(contradicting),
            unknown_count=len(unknown),
            has_profile=profile is not None,
            stale_count=sum(
                1
                for item in [*supporting, *contradicting, *unknown]
                if item.valid_until is not None and as_utc(item.valid_until) < datetime.now(UTC)
            ),
        )
    bypassed = bool(route and route.get("bypassed_model"))

    if model_client is not None and not bypassed:
        try:
            return _attach_jev_route(
                _try_model(db, run, question, supporting, contradicting, unknown, profile=profile),
                route,
            )
        except (ResponseGateError,) + (_model_client_exceptions()):
            # 模型不可用 / 输出无法通过闸门 / 引用校验失败 → 静默回退本地，如实标注。
            local = _synthesize(db, run, question, supporting, contradicting, unknown, profile=profile)
            return _attach_jev_route(
                local.model_copy(
                    update={
                        "model_provider": "local-deterministic",
                        "model_name": "steward-evidence-synthesizer",
                        "limitations": local.limitations
                        + [
                            "本次尝试调用外部模型但未通过可用性或回答闸门校验，已回退本地确定性引擎并如实标注，未伪装成模型产出。",
                        ],
                    }
                ),
                route,
            )
    return _attach_jev_route(
        _synthesize(db, run, question, supporting, contradicting, unknown, profile=profile),
        route,
    )


def _attach_jev_route(response: AgentResponse, route: Mapping[str, Any] | None) -> AgentResponse:
    """把 JV07 路由结论挂到回答上（**只附加，不改写 `action_mode`**）。

    `route` 为 None（本轮没跑预判）时**原样返回**，一个字段都不动——与接入前逐字节一致。
    """
    if route is None:
        return response
    fields = set(JevRouteDecision.model_fields)
    decision = JevRouteDecision(**{key: value for key, value in route.items() if key in fields})
    return response.model_copy(
        update={
            "jev_route": decision,
            "limitations": [*response.limitations, _route_limitation(route)],
        }
    )


def _model_client_exceptions() -> tuple[type[Exception], ...]:
    try:
        from investment_steward_core.model_client import ModelUnavailable

        return (ModelUnavailable,)
    except ImportError:  # pragma: no cover - 防御性
        return ()


def _try_model(
    db: Database,
    run: ResearchRun,
    question: str,
    supporting: list[Evidence],
    contradicting: list[Evidence],
    unknown: list[Evidence],
    *,
    profile: InvestorProfile | None = None,
) -> AgentResponse:
    """尝试模型路径：激活方案 → 组装上下文 → 调用 → 闸门校验 → 结构化入账。

    任一环节失败即抛异常（由 `_answer` 统一降级到本地引擎）。引用真实性由
    `response_gate` 强制校验，虚构引用不可能进入返回的 AgentResponse。
    """
    from investment_steward_core import model_client as mc
    from investment_steward_core.prompting import RESEARCH_PROMPT_VERSION, research_system_prompt

    profiles = db.list_model_profiles()
    profile_obj = mc.active_model_profile(profiles)
    if profile_obj is None:
        from investment_steward_core.model_client import ModelUnavailable

        raise ModelUnavailable("未配置激活的模型方案")

    context = _context_for_prompt(question, supporting, contradicting, unknown, profile=profile)
    reply = mc.call_active_model(
        profile_obj,
        _CredentialDependency(db),
        mc.format_messages(research_system_prompt(), context),
     purpose="追问",)

    payload = build_gate_payload(
        reply.content,
        supporting + contradicting + unknown,
        run_id=run.run_id,
        user_id=run.user_id,
    )
    payload["model_provider"] = profile_obj.name
    payload["model_name"] = reply.model
    payload["prompt_policy_version"] = RESEARCH_PROMPT_VERSION
    # 真实工具调用记录（阶段 C3）：证据账本读取 + 模型调用；不再保留占位符。
    payload["plugin_runs"] = [
        f"evidence-ledger@{run.user_id}",
        f"model:{profile_obj.name}",
    ]
    payload["tool_calls"] = ["read_evidence_ledger", f"call_model:{profile_obj.name}"]
    return assemble_agent_response(payload, response_id=uuid4())


class _CredentialDependency:
    """把 Database 的凭据读取适配成 model_client 期望的 `.get(key_id)` 形态。"""

    def __init__(self, db: Database):
        self._db = db

    def get(self, key_id: str) -> str | None:
        return self._db.get_credential(key_id)


def _context_for_prompt(
    question: str,
    supporting: list[Evidence],
    contradicting: list[Evidence],
    unknown: list[Evidence],
    *,
    profile: InvestorProfile | None = None,
) -> str:
    lines = [f"研究问题：{question}", "", "可用证据账本："]
    if not (supporting or contradicting or unknown):
        lines.append("（空）")
    for group, items in (
        ("支持", supporting),
        ("相反", contradicting),
        ("未知", unknown),
    ):
        for ev in items:
            lines.append(f"- [{group}] {ev.evidence_id} · {ev.source_name}: {ev.summary}")

    lines.append("")
    lines.append("投资者画像（仅作参考视角，不作为买卖依据）：")
    if profile is None:
        lines.append("（未建立投资者画像）")
    else:
        if profile.investment_goal:
            lines.append(f"- 投资目标：{profile.investment_goal}")
        if profile.horizon_years is not None:
            lines.append(f"- 投资期限：{profile.horizon_years:g} 年")
        if profile.knowledge_self_assessment:
            lines.append(f"- 投资者自评：{profile.knowledge_self_assessment}")
        if profile.markets_and_assets:
            lines.append(f"- 关注市场/资产：{', '.join(profile.markets_and_assets)}")
    return "\n".join(lines)


def read_latest_response(db: Database, user_id: UUID) -> AgentResponse | None:
    runs = db.list_research_runs(user_id)
    for item in runs:
        if item.response_id is not None:
            return db.get_agent_response(item.response_id, user_id)
    return None


# ---------------------------------------------------------------------------
# JV07：行动模式预判路由（纯函数层，出网由调用方注入）
#
# **要解决的问题**：研究链路的引擎选择是**硬编码**的——`_answer` 只要 `model_client` 存在就先跑
# 模型路径（`_try_model`，一次完整 prompt + 全部证据上下文），失败才回退本地确定性引擎。
# 于是「这个提问其实不需要研究」的情况也要付一次完整模型调用的钱。
#
# **本层做什么**：在跑模型路径**之前**先问 Jev 一道 `choice`：这次提问该进哪条链路。
# 官方 **intent-routing** 原型（路线图 §3 已标 ✅ 采纳）：`confidence` **从 choice 应答读取**
# （不是塞进 options），低置信 → 维持现行完整链路。
#
# **为什么只有 `no_action` 会真正换路径**（这是本层最需要解释的一条）：
# 代码里「轻」的路径只有**一条**——本地确定性引擎 `_synthesize`；四个模式并没有各自对应的
# 轻量链路。而判为 `research` / `observe` / `review_plan` 恰恰说明**这题要紧**，此时省额度是
# 拿回答质量换钱，不划算。只有判为 `no_action`（「当前不需要研究、观察或复盘」）才意味着
# **这次完整调用本身就不该发生**。所以：
#
#   bypassed_model = (choice == "no_action") AND (confidence >= 0.5)
#
# 其余任何情况（含失败、缺答、低置信、判为 research/observe/review_plan）一律**维持完整链路**——
# 多花一次调用只是多花钱，走错链路是少给答案。
#
# **硬约束①（验收明写）**：预判**永不改写**回答里的 `action_mode`。`action_mode` 始终由
# 实际产出方（模型经 `response_gate` / 本地引擎）给出；本层只决定「跑哪条链路」，把自己的结论
# 单独放在 `jev_route` 里。两者不一致时**如实并陈**（写进 `limitations`），不互相覆盖。
#
# **硬约束②**：路由决策（含 `confidence`、所选模式、完整题目回执）随 `jev_route` 落库与审计。
#
# **零变化开关**：`reviewer` 为 None（总闸关闭 / 未启用）或 `model_client` 为 None
# （模型出网关闭，本来就走本地引擎——预判既省不下钱又要多花一次 Jev 调用）时，整层不跑。
# ---------------------------------------------------------------------------

#: 分片复核回调（与 JV05/JV06/JV08 共用 `jev_client.build_shard_reviewer` 的返回形态）。
JevShardReviewer = Callable[[Mapping[str, Any]], list[dict[str, Any]]]

JEV_ROUTE_SCHEMA_VERSION = "1.0"

#: 单题 id。本场景恒一道题（预判是**整体**判断，不是逐条打分），故不需要 `n{index}_` 前缀。
JEV_ROUTE_QUESTION_ID = "q_action_mode"

#: 四个选项直接给四个行动模式（官方 intent-routing：criteria 即路由目标），并补**描述性**说明。
#: 用描述而不是光秃秃的模式名——「review_plan」这类内部词面在模型看来没有判别力。
JEV_ROUTE_OPTIONS: dict[str, str] = {
    "research": "补研究：现有证据不足以回答、或存在矛盾需要先澄清，值得花一次完整研究",
    "observe": "观察：证据已足够形成倾向性判断，继续跟踪即可，不需要现在深挖",
    "review_plan": "复盘计划：持仓或既定方案本身可能已失效，应先复核计划而不是继续研究标的",
    "no_action": "不行动：这个问题当前不需要研究、观察或复盘（例如纯闲聊、或与投资无关）",
}

#: 选项短标签（界面与审计用）。
JEV_ROUTE_MODE_LABELS: dict[str, str] = {
    "research": "补研究",
    "observe": "观察",
    "review_plan": "复盘计划",
    "no_action": "不行动",
}

#: 唯一会真正换路径的预判模式。理由见本节抬头。
JEV_ROUTE_BYPASS_MODE = "no_action"

#: 置信度地板线。
#:
#: ⚠️ **未标定**：R6 只标定了 `noul` 三路阈值与 `partial` 降级开关，本场景（`choice` 的
#: confidence 分档）**没有中文样本**，故沿用官方 intent-routing 原型与英文范例的 0.5。
#: 用法是保守方向（**不达标就维持完整链路**），所以标定缺失的代价是「少省几次调用」
#: 而不是「把该研究的提问打发掉」。
JEV_ROUTE_CONFIDENCE_FLOOR = 0.5

#: 路由态：`routed`（拿到预判并按它走了）/ `full`（拿到预判但维持完整链路）/ `unannotated`（没拿到）。
JEV_ROUTE_STATE_ROUTED = "routed"
JEV_ROUTE_STATE_FULL = "full"
JEV_ROUTE_STATE_UNANNOTATED = "unannotated"

#: state 预算：官方「state + 最长单题 ≤ 32k」，留足余量取 20k（与 JV04/JV05/JV06/JV08 同口径）。
JEV_ROUTE_STATE_MAX_CHARS = 20_000
#: 用户问题进 state 的截断长度（**只影响送出去的文本，不影响回答本身**）。
JEV_ROUTE_QUESTION_CHARS = 500


def jev_route_state(
    question: str,
    *,
    supporting_count: int = 0,
    contradicting_count: int = 0,
    unknown_count: int = 0,
    has_profile: bool = False,
    stale_count: int = 0,
) -> str:
    """构造 JV07 的 state：**用户问题 + 数据完备度标志**，仅此。

    **明确不带证据正文**（《契约》§F：JV07 允许「已有结论摘要 + 数据完备度标志」，排除研报正文）。
    理由不只是出网面：证据正文会被完整链路原样用上，这里再送一遍是**同一份内容付两次钱**；
    而「有几条支持 / 几条相反 / 几条未知」这些**标志**才是路由真正需要的判别信息。
    """
    cleaned = " ".join(str(question or "").split())
    if len(cleaned) > JEV_ROUTE_QUESTION_CHARS:
        cleaned = f"{cleaned[:JEV_ROUTE_QUESTION_CHARS]}…"
    return (
        "【用户提问】\n"
        f"{cleaned or '（空问题）'}\n\n"
        "【本地证据账本完备度】（只给条数，不给正文）\n"
        f"支持证据：{max(0, int(supporting_count))} 条\n"
        f"相反证据：{max(0, int(contradicting_count))} 条\n"
        f"关系未知：{max(0, int(unknown_count))} 条\n"
        f"已建立投资者画像：{'是' if has_profile else '否'}\n"
        f"已过期证据：{max(0, int(stale_count))} 条"
    )[:JEV_ROUTE_STATE_MAX_CHARS]


def jev_route_question() -> dict[str, dict[str, Any]]:
    """JV07 的题面：一道 `choice`，四个行动模式作选项（含描述）。

    `confidence` **不从这里塞进 options**——它由应答侧给出（官方 intent-routing 原型）。
    """
    return {
        JEV_ROUTE_QUESTION_ID: jev_client.choice_question(
            "判断这次提问**接下来该走哪条链路**——即「值不值得为它跑一次完整研究（一次完整"
            " prompt + 全部证据上下文）」。只依据上面的提问与证据完备度标志判断：\n"
            "① 若提问与投资无关（闲聊、纯功能询问）或明显不需要研究/观察/复盘 → 选 no_action；\n"
            "② 若现有证据互相矛盾或明显不足，需要先补证据 → 选 research；\n"
            "③ 若证据已够形成倾向性判断，只需继续跟踪 → 选 observe；\n"
            "④ 若更像是「持仓或既定方案本身可能失效」，应先复核计划 → 选 review_plan。\n"
            "只做路由判断，不要回答这个问题本身，也不要给出投资建议。",
            JEV_ROUTE_OPTIONS,
        )
    }


def jev_route_shards(
    question: str,
    *,
    supporting_count: int = 0,
    contradicting_count: int = 0,
    unknown_count: int = 0,
    has_profile: bool = False,
    stale_count: int = 0,
) -> dict[str, Any]:
    """包成 `build_shard_reviewer` 认的分片包（**恒为单片**）。

    为什么仍走分片形态而不是直接 `(state, questions)`：退避序列、可重试状态码判定、失败隔离
    这三件事与「评的是什么」无关，各写一遍迟早漂成两套口径。单片包让 JV07 直接复用
    `jev_client.build_shard_reviewer`（`purpose="jev:action-routing"`）。
    """
    state = jev_route_state(
        question,
        supporting_count=supporting_count,
        contradicting_count=contradicting_count,
        unknown_count=unknown_count,
        has_profile=has_profile,
        stale_count=stale_count,
    )
    questions = jev_route_question()
    return {"shards": [{"state": state, "questions": questions, "evaluated": [{"index": 1}]}]}


def jev_route_decision(answer: Any) -> dict[str, Any]:
    """从一道 `choice` 应答读出路由决策。

    **保守方向**：任何拿不到 / 不认识 / 置信度不达标的情况都返回 `bypassed_model=False`
    ——维持完整链路。多花一次调用只是多花钱，走错链路是少给答案。
    """
    raw_choice = getattr(answer, "choice", None)
    raw_confidence = getattr(answer, "confidence", None)
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None
    probabilities_raw = getattr(answer, "probabilities", None) or {}
    probabilities = {
        str(key): round(float(value), 4)
        for key, value in dict(probabilities_raw).items()
        if isinstance(value, (int, float))
    }
    base: dict[str, Any] = {
        "question_id": JEV_ROUTE_QUESTION_ID,
        "options": list(JEV_ROUTE_OPTIONS),
        "confidence": confidence,
        "probabilities": probabilities,
        "confidence_floor": JEV_ROUTE_CONFIDENCE_FLOOR,
        "bypass_mode": JEV_ROUTE_BYPASS_MODE,
        "schema_version": JEV_ROUTE_SCHEMA_VERSION,
    }
    if not isinstance(raw_choice, str) or raw_choice not in JEV_ROUTE_OPTIONS:
        return {
            **base,
            "mode": None,
            "mode_label": "未取得预判",
            "bypassed_model": False,
            "route_state": JEV_ROUTE_STATE_UNANNOTATED,
            "note": "未取得有效预判（题号缺答或选项不认识），维持完整研究链路",
        }
    bypassed = (
        raw_choice == JEV_ROUTE_BYPASS_MODE
        and confidence is not None
        and confidence >= JEV_ROUTE_CONFIDENCE_FLOOR
    )
    if bypassed:
        note = (
            f"预判为「{JEV_ROUTE_MODE_LABELS[raw_choice]}」且置信度 {confidence:.2f}"
            f" ≥ {JEV_ROUTE_CONFIDENCE_FLOOR:.2f}，跳过完整研究链路，改由本地确定性引擎作答"
        )
    elif raw_choice == JEV_ROUTE_BYPASS_MODE:
        reason = (
            "置信度缺失" if confidence is None
            else f"置信度 {confidence:.2f} < {JEV_ROUTE_CONFIDENCE_FLOOR:.2f}"
        )
        note = f"预判为「{JEV_ROUTE_MODE_LABELS[raw_choice]}」但{reason}，维持完整研究链路"
    else:
        note = (
            f"预判为「{JEV_ROUTE_MODE_LABELS[raw_choice]}」，说明这题值得认真回答，"
            "维持完整研究链路（不为省额度牺牲回答质量）"
        )
    return {
        **base,
        "mode": raw_choice,
        "mode_label": JEV_ROUTE_MODE_LABELS[raw_choice],
        "bypassed_model": bypassed,
        "route_state": JEV_ROUTE_STATE_ROUTED if bypassed else JEV_ROUTE_STATE_FULL,
        "note": note,
    }


def jev_action_route(
    *,
    reviewer: JevShardReviewer | None,
    question: str,
    supporting_count: int = 0,
    contradicting_count: int = 0,
    unknown_count: int = 0,
    has_profile: bool = False,
    stale_count: int = 0,
) -> dict[str, Any] | None:
    """跑一次行动模式预判，返回回执块；`reviewer` 为 None 时返回 None（整层不跑）。

    **从不抛异常**：复核回调抛错时按 `unannotated` 如实标注并**维持完整链路**——
    路由是一个附加的省钱层，它挂掉绝不能让提问答不上来。
    """
    if reviewer is None:
        return None
    bundle = jev_route_shards(
        question,
        supporting_count=supporting_count,
        contradicting_count=contradicting_count,
        unknown_count=unknown_count,
        has_profile=has_profile,
        stale_count=stale_count,
    )
    results: list[dict[str, Any]] = []
    failure = ""
    try:
        results = list(reviewer(bundle))
    except Exception as exc:  # noqa: BLE001 - 附加层绝不能拖垮研究链路
        failure = f"{type(exc).__name__}: {exc}"[:300]
    first = results[0] if results else {}
    error = first.get("error")
    answers = first.get("answers") or {}
    decision = jev_route_decision(answers.get(JEV_ROUTE_QUESTION_ID))
    models = [str(entry.get("model")) for entry in results if entry.get("model")]
    decision["model"] = "/".join(sorted(set(models)))
    decision["available"] = True
    if error or failure:
        detail = error or failure
        decision.update({
            "mode": None,
            "mode_label": "未取得预判",
            "bypassed_model": False,
            "route_state": JEV_ROUTE_STATE_UNANNOTATED,
            "note": f"预判未取得应答（{detail}），维持完整研究链路",
        })
    return decision


def _route_limitation(route: Mapping[str, Any]) -> str:
    """把路由结论写进 `limitations` 的人话说明（**如实并陈，不覆盖 `action_mode`**）。

    预判与实际产出的 `action_mode` 不一致是**正常且可能**的：预判说的是「该不该花这次调用」，
    而 `action_mode` 是作答方按证据账本给出的结论。两者不互相改写，只并陈说明。
    """
    if route.get("bypassed_model"):
        return (
            f"JV07 路由：预判为「{route.get('mode_label')}」（置信度 "
            f"{route.get('confidence') if route.get('confidence') is not None else '—'}），"
            "本次未调用完整研究链路，回答由本地确定性引擎按证据账本给出。"
            "下面的行动模式是**作答方**的结论，未被引擎预判改写；若你需要完整研究，可重新提问。"
        )
    if route.get("route_state") == JEV_ROUTE_STATE_FULL:
        return (
            f"JV07 路由：预判为「{route.get('mode_label')}」（置信度 "
            f"{route.get('confidence') if route.get('confidence') is not None else '—'}），"
            "维持完整研究链路；下面的行动模式由**作答方**给出，未被引擎预判改写。"
        )
    return (
        "JV07 路由：本轮未取得有效预判，维持完整研究链路；下面的行动模式由**作答方**给出。"
    )
