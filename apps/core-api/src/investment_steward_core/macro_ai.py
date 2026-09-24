"""宏观雷达 AI 层（M5.2 / M5.5 提议 / M6 摘要）：纯函数，模型无关，可单测。

红线（方案 §3.7 / ADR-0006）：
- AI 不修改规则层数值——上下文只读，输出不回写打分；
- 无引用不发布——生成文本必须内联引用真实指标（label 级），校验器硬性拦截；
- AI 提议受护栏约束（D-12）：单维变化 ≤10pp、每维 ≥10%、30 天最多 2 次；用户手动调整不受限。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from investment_steward_core.domain import MacroSnapshot

# —— D-12 护栏参数（只约束 AI，不约束用户）——
AI_MAX_DELTA_PP = 0.10  # 单维变化上限（小数口径 10pp）
AI_MIN_WEIGHT = 0.10  # 单维下限 10%
AI_PROPOSAL_WINDOW_DAYS = 30
AI_PROPOSAL_MAX_PER_WINDOW = 2

ANALYSIS_SYS_PROMPT = (
    "你是投资管家的宏观分析副驾。输入是规则引擎产出的指标读数与定位打分（确定性、可复算）。"
    "你的任务是生成结构化分析，硬性要求："
    "①不得修改、重算或质疑规则层数值（分数与状态带以输入为准）；"
    "②每个论断必须内联引用具体指标，格式如（CPI 2026-07, 3.0%）——只能引用输入中真实存在的指标与数值；"
    "③全文第一行必须是「【模型方向】」加四档之一（扩张／放缓／承压／衰退风险），"
    "这是你基于引用指标的独立判断，允许与规则状态带不同（分歧即信息，不做仲裁）；"
    "④第二行起输出四段，用且仅用以下小节标题："
    "【趋势叙事】【情景推演】【关注清单】【分析局限】；"
    "【情景推演】给基准/风险/反转三情景，各一句触发条件，不做概率承诺；"
    "⑤不构成投资建议，不推荐具体标的。全文中文。"
)

PROPOSAL_SYS_PROMPT = (
    "你是投资管家宏观雷达的权重提议副驾。输入是当前四维权重与用户的调整意图（可能引用发言人表态）。"
    "请输出一个 JSON 对象（不要输出其他文字、不要 markdown 围栏），字段："
    '{"weights": {"growth": 0-1小数, "employment": 0-1小数, "inflation": 0-1小数, "monetary": 0-1小数},'
    ' "rationale": "调整依据，必须引用用户意图中的原句", "dim_changed": "主要变动维度英文键",'
    ' "direction": "上调|下调"}。'
    "约束：四维合计必须恰为 1.0；单维变化尽量不超过 0.10（10pp）；每维不低于 0.10。"
)

INSIGHTS_SYS_PROMPT = (
    "你是投资管家的研读图书馆档案副驾。输入是用户书单（书名/作者/进度/标签）。"
    "请生成认知档案，输出四段，用且仅用以下小节标题："
    "【当前关注主题】【知识结构】【荐读方向】【与研究与投资的连接】。"
    "硬性要求：必须具体提到书单中真实存在的书名（至少 3 处）作为依据；"
    "不虚构书单外的书；荐读方向只描述主题方向，不编造具体书名。全文中文。"
)


def build_analysis_context(snapshot: MacroSnapshot) -> str:
    """快照 → 只读上下文文本（ok 指标 + 规则定位）。pending 项只报状态不给数值。"""
    lines: list[str] = [f"经济体：{snapshot.region}（{snapshot.label}）"]
    ok_rows = [
        f"- {item.label}（{item.indicator}）：最新 {item.latest}{(' ' + item.unit) if item.unit else ''}"
        f"，观测期 {item.obs_date or '未知'}，数据版本 {item.dataset_version or '未知'}"
        for item in snapshot.indicators
        if item.status == "ok" and item.latest is not None
    ]
    lines.append("指标读数（仅可引用这些）：")
    lines.extend(ok_rows or ["- （无可用指标读数）"])
    pending = [item.label for item in snapshot.indicators if item.status != "ok"]
    if pending:
        lines.append(f"未接入/降级（不可引用其数值）：{('、'.join(pending))}")
    pos = snapshot.positioning
    if pos is not None:
        lines.append(
            f"规则基线定位：综合 {pos.composite} 分 → 状态带【{pos.band}】（权重版本 {pos.weight_version}）"
        )
        for dim in pos.dims:
            if dim.score is not None:
                fired = "；".join(dim.rules_fired) if dim.rules_fired else "无规则命中"
                lines.append(f"- {dim.dim} 维 {dim.score} 分（{fired}）")
        if pos.limitations:
            lines.append("口径注意：" + "；".join(pos.limitations))
    return "\n".join(lines)


def verify_citations(text: str, snapshot: MacroSnapshot, *, min_citations: int = 2) -> list[str]:
    """引用校验器（M5.2）：分析文本必须引用 ≥min_citations 个真实 ok 指标 label，无引用不发布。"""
    ok_labels = [
        item.label
        for item in snapshot.indicators
        if item.status == "ok" and item.latest is not None
    ]
    cited = [label for label in ok_labels if label and label in text]
    return cited if len(cited) >= min_citations else []


ANALYSIS_DIRECTIONS = ("扩张", "放缓", "承压", "衰退风险")


def parse_analysis_direction(text: str) -> str | None:
    """解析模型分析首行的结构化方向：缺失或非法一律 None，不编造。

    同时接受「【AI 方向】」（历史缓存）与「【模型方向】」（现行提示词）两种首行格式。
    """
    match = re.search(r"【(?:AI|模型)\s*方向】\s*(扩张|放缓|承压|衰退风险)", text)
    return match.group(1) if match else None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出提取 JSON 对象：容忍 ```json 围栏与前后杂文。失败返回 None。"""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    candidate = fenced.group(1) if fenced else stripped
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def check_proposal_guardrails(
    proposal_weights: dict[str, Any],
    current_weights: dict[str, float],
) -> tuple[dict[str, float] | None, str]:
    """AI 提议护栏（D-12）：四维齐全/合计=1.0/单维变化 ≤10pp/每维 ≥10%。违规返回 (None, 原因)。"""
    expected = set(current_weights)
    if set(proposal_weights) != expected:
        return None, f"提议权重必须且只能包含四维：{sorted(expected)}"
    for dim, value in proposal_weights.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None, f"权重 {dim} 不是数值"
    weights = {dim: float(value) for dim, value in proposal_weights.items()}
    if abs(sum(weights.values()) - 1.0) > 0.005:
        return None, f"提议四维合计必须为 1.0（当前 {round(sum(weights.values()), 4)}）"
    for dim, value in weights.items():
        if value < AI_MIN_WEIGHT:
            return None, f"提议违反护栏：{dim} 权重 {value} 低于下限 {AI_MIN_WEIGHT}（D-12）"
        delta = abs(value - current_weights[dim])
        if delta > AI_MAX_DELTA_PP + 1e-9:
            return None, (
                f"提议违反护栏：{dim} 变化 {round(delta, 4)} 超过单维上限 {AI_MAX_DELTA_PP}（10pp，D-12）——"
                "可分多次提议或由用户在面板手动调整（手动不受护栏限制）"
            )
    return weights, ""


def count_recent_ai_proposals(versions: list[dict[str, Any]], now: datetime | None = None) -> int:
    """统计最近 30 天内 source='ai' 的权重版本数（30 天最多主动提议 2 次）。"""
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=AI_PROPOSAL_WINDOW_DAYS)
    count = 0
    for row in versions:
        if row.get("source") != "ai":
            continue
        try:
            created = datetime.fromisoformat(str(row.get("created_at", "")))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created >= cutoff:
            count += 1
    return count


def build_insights_context(books: list[Any]) -> str:
    """书单 → 认知档案上下文（真实字段：title/author/progress/status/notes）。"""
    status_label = {"reading": "在读", "finished": "读完", "wishlist": "想读"}
    lines = [f"书单共 {len(books)} 本："]
    for book in books:
        progress = f"{round(float(book.progress) * 100)}%"
        state = status_label.get(getattr(book, "status", ""), "未知")
        notes = getattr(book, "notes", None) or []
        note_text = ("；笔记：" + " / ".join(notes[:3])) if notes else ""
        lines.append(f"- 《{book.title}》{book.author}（{state}，进度 {progress}{note_text}）")
    return "\n".join(lines)


def verify_insight_citations(text: str, books: list[Any], *, min_citations: int = 3) -> list[str]:
    """档案引用校验：必须提到 ≥min_citations 个真实书名，不虚构。"""
    titles = [book.title for book in books if getattr(book, "title", None)]
    cited = [title for title in titles if title and title in text]
    return cited if len(cited) >= min_citations else []


# ---- 发言人信号解读（§3.5 段二，D-13）：方向标注 + 关注点迁移，引用原文句子 ----

SPEAKER_SYS_PROMPT = """你是投资研究助手中的宏观沟通解读模块。你只会收到一段央行/官方发言人原文（excerpt）与基本信息。
任务：
1. 方向标注：鹰派（偏紧缩/担忧通胀）、鸽派（偏宽松/担忧就业与增长）、中性。
2. 关注点迁移：发言人相比典型立场更强调什么（如从通胀转向就业下行风险）。
3. 解读含义：这对「就业/通胀/增长/货币四维中哪类信号更重要」意味着什么——只描述含义，不输出任何买卖建议、仓位建议。

硬性规则：
- rationale 必须逐字引用 excerpt 中的原句（至少一句）作为依据，禁止转述冒充引用；
- 不引用原文就不许下方向结论；
- 不编造 excerpt 中不存在的信息；
- 只输出一个 JSON 对象：{"direction": "鹰派|鸽派|中性", "rationale": "…（含原文引用句）…", "focus_shift": "…（一句话）…"}，不要输出其他内容。"""


def _split_sentences(text: str) -> list[str]:
    """按中英文句读切分原文，取长度 ≥6 的片段（过短片段匹配意义弱）。"""
    import re

    parts = re.split(r"[。！？；;.!?\n\r]+", text)
    return [part.strip() for part in parts if len(part.strip()) >= 6]


def verify_speaker_citations(rationale: str, excerpt: str, *, min_citations: int = 1) -> list[str]:
    """发言人解读引用校验：rationale 必须包含 ≥min_citations 句 excerpt 原文（子串匹配）。

    编造/转述不匹配原句 → 返回空列表（无引用不回填方向，ADR-0006）。
    """
    sentences = _split_sentences(excerpt)
    cited = [sentence for sentence in sentences if sentence in rationale]
    return cited if len(cited) >= min_citations else []
