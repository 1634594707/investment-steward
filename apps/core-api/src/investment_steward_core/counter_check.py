"""轻量反方检查（v27，2026-09-12 方案 §3.2；v28 二次方案 §2.1 补来源与日期；
v32 按 2026-09-13 方案 §四.2 升级为**按章节/claim 覆盖**）。

定位：**只做一次短 JSON 调用**，不重写正文、不新增结论，只回答四件事——
1. 最强支持证据是什么；
2. 最强反证是什么（必须能在报告或证据里指出来源，不许泛指「市场有风险」）；
3. 该结论成立需要哪些必要条件（缺一条即不成立）；
4. 报告里最容易被误读的那一句话，以及它为什么会被误读。

为什么不是「再生成一篇」：协同流水线已有 check→draft→risk→revise 四角色（成本高、
用户手动推进）；普通「快速研报」需要的是一条低成本的自我反驳，因此采用单次短调用，
输出固定四字段。失败一律如实标注，**不影响报告本身的交付**。

v28（二次方案 §2.1「首屏结论卡」）：首屏的「最大支持 / 最大反证」要求**带来源和日期**，
因此输出 schema 增 `strongest_support_source` / `strongest_support_date` /
`strongest_counter_source` / `strongest_counter_date`，且来源编号走**真实性闸门**
（不属于本次真实引用的编号一律清空并如实标注，防「虚构来源支撑反证」）。

v32（2026-09-13 方案 §四.2）：输入从「正文前 2500 字」改为**全章节覆盖**——
每个小节独立截断送审（超长如实标注），估值块、局限声明与全部 claims 并列送入；
长报告的后半段（估值/情景/风险）不再因总长截断而逃过反方检查。

设计铁律（与全站一致）：纯函数、无 IO；只返回提示词与归一化结果，是否调用由调用方决定。
"""

from __future__ import annotations

import re
from typing import Any

# 复核提示词版本：改动提示词或输出 schema 必须递增（与 report 提示词同一约定）。
COUNTER_CHECK_PROMPT_VERSION = "1.2"

# 单个小节的送审截断长度（§四.2：按章节送审后不再需要全文一刀切；截断处如实标注）。
SECTION_EXCERPT_CHARS = 1400

_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$")

_SYSTEM = (
    "你是研究结论的「反方审查员」。给你一份已经写好的个股研究报告与它引用的来源清单，"
    "你的职责**不是**重写报告，而是用最短的篇幅指出这份结论的软肋。\n"
    "必须做到：\n"
    "1. 只输出 JSON，不要任何解释性前后缀；\n"
    "2. 只依据给定报告与来源说话，禁止编造报告里没有的数字、事件或来源编号；\n"
    "3. 反证要**具体且可追溯**：指出是哪条判断、依赖哪条来源、为什么它不足以支撑结论；"
    "禁止「市场有风险」「需谨慎」这类空话；\n"
    "4. 只回答下面 schema 里的字段，不新增字段、不写分段标题、不下买卖建议、不预测涨跌；\n"
    "5. 最强支持与最强反证都要标出**来源编号与日期**：来源编号只能取报告已引用的那几个"
    "（如 S1/S4），报告里没有对应来源就留空字符串，**禁止编造编号**；日期取该来源中出现的日期，"
    "报告未给出就留空，不要用今天或猜测的日期。\n"
    "输出 JSON schema："
    '{"strongest_support": "报告中最有力的那条支持证据（40 字内）",'
    '"strongest_support_source": "该证据的来源编号（如 S4，只能取自报告已引用来源；没有就空串）",'
    '"strongest_support_date": "该证据的日期 YYYY-MM-DD（报告未给就空串）",'
    '"strongest_counter": "最强反证（60 字内：说明它反驳了哪条判断、依据是什么）",'
    '"strongest_counter_source": "反证的来源编号（同上规则）",'
    '"strongest_counter_date": "反证依据的日期 YYYY-MM-DD（报告未给就空串）",'
    '"necessary_conditions": ["最多 3 条，每条 30 字内：结论成立所必需、但当前证据并未确认的条件"],'
    '"most_misread_sentence": {"quote": "报告中最易被误读的一句原话（照抄，不超过 50 字）",'
    ' "why": "为什么会被误读（40 字内）"},'
    '"verdict_robustness": "robust|mixed|fragile"（结论对反证的耐受度）}'
)


def _split_sections(body: str) -> list[tuple[str, str]]:
    """按 markdown 标题把正文切成（节名, 节文本）序列；无任何标题时整篇作为单节。

    §四.2：反方检查按章节送审——每个小节独立截断（SECTION_EXCERPT_CHARS），
    长报告后半段的估值/情景/风险章节不再因总长截断而被跳过。
    """
    sections: list[tuple[str, str]] = []
    current_name = "（正文开头）"
    current_lines: list[str] = []
    for line in (body or "").splitlines():
        heading = _HEADING.match(line.strip())
        if heading is not None:
            if any(line_.strip() for line_ in current_lines):
                sections.append((current_name, "\n".join(current_lines)))
            current_name = heading.group(1).replace("*", "").strip().strip("：:").strip() or "（未命名小节）"
            current_lines = []
        else:
            current_lines.append(line)
    if any(line_.strip() for line_ in current_lines):
        sections.append((current_name, "\n".join(current_lines)))
    return sections or [("（正文）", body or "")]


def _sections_text(body: str) -> str:
    """全章节送审文本：每节独立截断，超长在节尾如实标注（不静默截断）。"""
    blocks: list[str] = []
    for name, text in _split_sections(body):
        stripped = text.strip()
        if not stripped:
            continue
        if len(stripped) > SECTION_EXCERPT_CHARS:
            stripped = stripped[:SECTION_EXCERPT_CHARS] + f"（本节超长已截断，仅取前 {SECTION_EXCERPT_CHARS} 字）"
        blocks.append(f"【{name}】\n{stripped}")
    return "\n\n".join(blocks) if blocks else "（无正文）"


def _claims_text(claims: Any) -> str:
    """把已校验的 claims 结论压缩成一行，让检查器知道「哪些判断撑住了、哪些被降级了」。"""
    if not isinstance(claims, list) or not claims:
        return "（报告未输出结构化核心判断清单）"
    lines: list[str] = []
    for item in claims[:8]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        note = f"｜{status}" if status else ""
        note += f"（{item.get('note')}）" if item.get("note") else ""
        lines.append(f"- [{item.get('claim_id')}] {item.get('text')}｜引 {item.get('sources')}{note}")
    return "\n".join(lines) if lines else "（报告未输出结构化核心判断清单）"


def counter_check_messages(
    *,
    symbol: str,
    title: str,
    executive_summary: str,
    report: str,
    citations: list[str],
    claims: Any = None,
    valuation: Any = None,
    limitations: Any = None,
) -> tuple[str, str]:
    """构造反方检查的 (system, user) 消息对。

    v32（§四.2）：正文按**全章节**送审（每节独立截断）；估值结构化块与局限声明并列送入，
    确保长报告后半段（估值/情景/风险）一定在检查视野内。
    """
    valuation_block = "（无）"
    if isinstance(valuation, dict):
        parts = [
            f"{key}={str(item).strip()}"
            for key, item in valuation.items()
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        if parts:
            valuation_block = "；".join(parts)
    limits = [str(item).strip() for item in limitations if str(item).strip()] if isinstance(limitations, list) else []
    user = (
        f"【标的】{symbol}\n"
        f"【标题】{title}\n"
        f"【执行摘要】{executive_summary}\n"
        f"【报告引用的来源】{'、'.join(citations) if citations else '（无）'}\n"
        f"【已校验的核心判断】\n{_claims_text(claims)}\n"
        f"【估值结论（结构化）】{valuation_block}\n"
        f"【局限声明】{'；'.join(limits) if limits else '（无）'}\n"
        f"【报告正文（按小节送审，全章节覆盖）】\n{_sections_text(report)}\n\n"
        "请按要求只输出 JSON：给出最强支持证据、最强反证、结论成立的必要条件、最易被误读的一句话。"
        "反证必须覆盖全部小节——尤其要检查靠后小节里的估值、情景与风险论述，不只看开头。"
    )
    return _SYSTEM, user


_VALID_ROBUSTNESS = ("robust", "mixed", "fragile")
_SOURCE_ID = re.compile(r"^S\d+$")
_ISO_DATE = re.compile(r"(?:19|20)\d{2}-\d{2}-\d{2}")


def _as_str_list(value: Any, limit: int, *, max_len: int = 60) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:max_len])
    return out[:limit]


def _source_id(value: Any, allowed: set[str] | None) -> str:
    """来源编号归一 + **真实性闸门**：不属于本次真实引用的编号一律丢弃（返回空串）。"""
    text = str(value or "").strip().strip("[]").upper()
    if not _SOURCE_ID.match(text):
        return ""
    if allowed is not None and text not in allowed:
        return ""
    return text


def _date_text(value: Any) -> str:
    """日期归一：只接受 ISO 日期（YYYY-MM-DD）；模型给的是别的写法/今天/相对词一律留空。"""
    match = _ISO_DATE.search(str(value or ""))
    return match.group(0) if match else ""


def normalize_counter_check(parsed: Any, *, allowed: set[str] | None = None) -> dict[str, Any] | None:
    """归一化反方检查输出。

    `strongest_counter` 缺失即视为无效（反方检查的全部价值就在这条）→ 返回 None，
    调用方按「本次未能完成反方检查」如实标注，而不是编一条反证出来。

    `allowed`（可选）= 本次真实引用的来源 id 集合；给出时对支持/反证的来源编号做**真实性闸门**，
    不属于该集合的编号清空并在 `dropped_sources` 里如实记录（同研报 citation 闸门口径）。
    """
    if not isinstance(parsed, dict):
        return None
    strongest_counter = str(parsed.get("strongest_counter") or "").strip()
    if not strongest_counter:
        return None
    sentence = parsed.get("most_misread_sentence")
    quote = why = ""
    if isinstance(sentence, dict):
        quote = str(sentence.get("quote") or "").strip()[:120]
        why = str(sentence.get("why") or "").strip()[:120]
    robustness = str(parsed.get("verdict_robustness") or "").strip().lower()

    dropped: list[str] = []
    resolved: dict[str, str] = {}
    for field, raw in (
        ("strongest_support_source", parsed.get("strongest_support_source")),
        ("strongest_counter_source", parsed.get("strongest_counter_source")),
    ):
        text = str(raw or "").strip().strip("[]").upper()
        value = _source_id(raw, allowed)
        if text and not value:
            dropped.append(text)
        resolved[field] = value

    return {
        "strongest_support": str(parsed.get("strongest_support") or "").strip()[:160],
        "strongest_support_source": resolved["strongest_support_source"],
        "strongest_support_date": _date_text(parsed.get("strongest_support_date")),
        "strongest_counter": strongest_counter[:240],
        "strongest_counter_source": resolved["strongest_counter_source"],
        "strongest_counter_date": _date_text(parsed.get("strongest_counter_date")),
        "necessary_conditions": _as_str_list(parsed.get("necessary_conditions"), 3, max_len=80),
        "most_misread_sentence": {"quote": quote, "why": why},
        "verdict_robustness": robustness if robustness in _VALID_ROBUSTNESS else "mixed",
        "dropped_sources": dropped,
        "prompt_version": COUNTER_CHECK_PROMPT_VERSION,
    }
