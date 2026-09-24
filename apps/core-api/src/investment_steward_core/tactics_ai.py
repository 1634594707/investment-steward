"""战法 AI 技术面复核（v26，2026-09-11 用户需求「可以选择借助 AI 分析评分」）。

定位（用户拍板，勿漂移）：
- **规则引擎算「图形证据的强度」**（形态权重 × 新鲜度 + 共振 − 对冲 + 量价/位置，全部可复算）；
- **AI 复核「形态质量与可疑点」**——这是规则算不出的部分：突破真假、量价配合是否可疑、
  位置风险、规则分可能高估/低估的原因、关键支撑压力位、需要盯的点。
- 因此提示词**明令禁止**复述证据、复述规则分、写研报体正文、给买卖建议、预测涨跌幅。
  用户已有「研报生成」入口，AI 复核的增量价值只在「质检」这一层。
- 手动触发（用户点按钮才调用），不参与任何自动评分；结果留痕供长期研究。

本模块纯函数、无 IO：提示词构造 + 输出解析/归一化。取数与模型调用由调用方（api/app.py）负责。

JV03（Jev 决策模型接入路线图 2026-09-21）把复核拆成**两层**（用户拍板「不需要 Jev 单独做」）：
- **判定层**（本文件 `jev_*` 系列）：可靠度分 / 认同度 / 假突破风险——三题、类型化答案、
  可复算、**不存在 JSON 解析失败**。Jev 优先，不可用时回落 chat 同名字段。
- **叙述层**（`review_messages` + `normalize_review`）：summary / 站得住的地方 / 可疑点 /
  接下来盯什么 / 规则分评价 / 关键价位。**Jev 只回「是/否、选哪个、打几分」，生成不了文本**，
  这部分只能由 chat 出。合并规则见 `review_view`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from investment_steward_core import jev_client

# 复核提示词版本：改动提示词或输出 schema 必须递增（与 report 提示词同一约定）。
REVIEW_PROMPT_VERSION = "1.0"

_REVIEW_SYSTEM = (
    "你是量化交易系统的「形态质量复核员」。系统里的规则引擎已经用确定性公式给出了战法质量分"
    "（形态权重 × 新鲜度 + 同族合并 + 跨族共振 − 多空对冲 + 量能/位置确认），"
    "分数与逐条命中明细都会一并给你。\n"
    "你的职责**只有一个**：复核这些信号本身的可靠性——规则引擎用固定公式算不出来的部分。\n"
    "必须做到：\n"
    "1. 只输出 JSON，不要任何解释性前后缀；\n"
    "2. 只依据给定证据说话，数字必须能在证据里找到，不得编造价格、成交量、指标或消息；\n"
    "3. 重点回答：这些信号是不是「看起来像但不可靠」（假突破/量价背离/形态未完成）；\n"
    "   规则分可能高估还是低估，为什么；位置（高位追涨/低位启动）带来的风险差异；\n"
    "   从 K 线直接看出来、但规则分项没覆盖的可疑点。\n"
    "禁止做到（越界即视为无效回答）：不要复述证据内容或规则分项数值；不要写研报正文、"
    "不要分段标题与长篇论述；不要给买入/卖出/加仓/减仓建议；不要预测涨幅、目标价或时间；"
    "不要提及你无法从证据中确认的公司消息或基本面。\n"
    "输出 JSON schema："
    '{"ai_score": 0-100 的整数（你对「当前技术面可靠度」的独立评分，与规则分口径不同：'
    "规则分衡量证据强度，你衡量证据可信度），"
    '"verdict": "bullish"|"neutral"|"bearish"（技术面倾向），'
    '"agreement": "agree"|"partial"|"disagree"（是否认同规则分的排序含义），'
    '"summary": "一句话结论（40 字内）",'
    '"strengths": ["最多 3 条，每条 30 字内"],'
    '"concerns": ["最多 3 条，每条 40 字内：具体的可疑点，不要空泛"],'
    '"fake_breakout_risk": "low"|"medium"|"high",'
    '"key_levels": {"support": 数字, "resistance": 数字}（由给定 K 线推出的关键价位，元；'
    "推不出就给 null），"
    '"watch_points": ["最多 3 条，每条 30 字内：接下来需要盯的信号"],'
    '"rule_score_comment": "60 字内：规则分给高了还是给低了、为什么"}'
)


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _bars_summary(bars: list[dict[str, Any]], tail: int = 12) -> str:
    """近期 K 线摘要（日期 + OHLC + 量），供模型判断形态质量。"""
    recent = bars[-tail:] if bars else []
    lines: list[str] = []
    for bar in recent:
        lines.append(
            f"{str(bar.get('timestamp', ''))[:10]} "
            f"O{_fmt_num(bar.get('open'))} H{_fmt_num(bar.get('high'))} "
            f"L{_fmt_num(bar.get('low'))} C{_fmt_num(bar.get('close'))} "
            f"V{_fmt_num(bar.get('volume'), 0)}"
        )
    return "\n".join(lines) if lines else "（无 K 线数据）"


def _hit_detail_text(rule_score: dict[str, Any]) -> str:
    """命中明细逐条列出（含未计分的同族合并项，让模型知道哪些信号被合并了）。"""
    detail = rule_score.get("hit_detail")
    if not isinstance(detail, list) or not detail:
        return "（窗口内无战法命中）"
    lines: list[str] = []
    for item in detail:
        if not isinstance(item, dict):
            continue
        note = "" if item.get("counted", True) else "（同族合并，不计分）"
        # v27：带上触发价与失效条件，让复核能落到「具体哪个价位被打破」而不是泛泛说形态可疑。
        price = item.get("trigger_price")
        price_note = f"｜触发价 {price}" if price is not None else ""
        invalidation = str(item.get("invalidation_rule") or "").strip()
        invalidation_note = f"｜失效条件 {invalidation}" if invalidation else ""
        lines.append(
            f"- {item.get('tactic_name')}｜{item.get('direction')}｜族={item.get('family')}｜"
            f"{item.get('date')}（{item.get('age_bars')} 根前，新鲜度 {item.get('freshness')}）｜"
            f"权重 {item.get('weight')}｜贡献 {item.get('contribution')}{price_note}{invalidation_note}{note}"
        )
    return "\n".join(lines) if lines else "（窗口内无战法命中）"


def _score_parts_text(rule_score: dict[str, Any]) -> str:
    parts = rule_score.get("score_parts")
    if not isinstance(parts, dict):
        return "（无分项）"
    return "；".join(f"{key}={value}" for key, value in parts.items())


def review_messages(
    *,
    symbol: str,
    name: str,
    rule_score: dict[str, Any],
    indicators: dict[str, Any],
    bars: list[dict[str, Any]],
    sector: dict[str, Any] | None = None,
    question: str = "",
) -> tuple[str, str]:
    """构造复核消息（system, user）。证据包 = 规则分全分项 + 命中明细 + 指标 + 近期 K 线。"""
    sector_text = ""
    if sector:
        sector_text = (
            f"\n【所属板块】{sector.get('name') or sector.get('code')}"
            f"（当日 {_fmt_num(sector.get('change_pct'))}%，成分 {sector.get('member_count')} 只"
            f"{('，领涨 ' + str(sector.get('leader_name'))) if sector.get('leader_name') else ''}）"
        )
    user = (
        f"【标的】{symbol} {name}\n"
        f"【命中窗口】最近 {rule_score.get('signal_window_bars')} 根 K 线"
        f"（自 {rule_score.get('signal_window_from')}）\n"
        f"【规则质量分】{rule_score.get('tactic_score')}（公式版本 {rule_score.get('score_formula_version')}，"
        f"权重版本 {rule_score.get('weight_version')}）｜方向 {rule_score.get('direction_bias')}\n"
        f"【规则分项】{_score_parts_text(rule_score)}\n"
        f"【量能/位置】量比 {rule_score.get('volume_ratio')}（回看 {rule_score.get('volume_lookback_bars')} 根）；"
        f"区间位置 {rule_score.get('position')}（近 {rule_score.get('position_window_bars')} 根的 high-low 区间，0=最低 1=最高）；"
        f"被同族合并的族：{rule_score.get('merged_families') or '无'}\n"
        f"【命中明细】\n{_hit_detail_text(rule_score)}\n"
        f"【指标现值】{'；'.join(f'{k}={v}' for k, v in list(indicators.items())[:20])}\n"
        f"{sector_text}\n"
        f"【近期 K 线（日）】\n{_bars_summary(bars)}\n"
        f"【共 {len(bars)} 根日 K 线（回看 250 根上限）】\n"
        f"{('【用户关注点】' + question + chr(10)) if question else ''}"
        "请按要求只输出 JSON：复核这些信号的质量与可疑点，说明规则分是高估还是低估。"
    )
    return _REVIEW_SYSTEM, user


_VALID_VERDICTS = ("bullish", "neutral", "bearish")
_VALID_AGREEMENTS = ("agree", "partial", "disagree")
_VALID_RISKS = ("low", "medium", "high")


def _as_float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _as_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    return items[:limit]


def normalize_review(parsed: Any) -> dict[str, Any] | None:
    """解析结果归一化：非法枚举回退中性值、列表截断、分数 clamp。

    返回 None 表示这不是一份可用的复核（缺 summary 或完全无字段），调用方按失败处理。
    """
    if not isinstance(parsed, dict):
        return None
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    agreement = str(parsed.get("agreement") or "").strip().lower()
    risk = str(parsed.get("fake_breakout_risk") or "").strip().lower()
    ai_score: float | None = None
    try:
        ai_score = max(0.0, min(100.0, float(parsed.get("ai_score"))))
    except (TypeError, ValueError):
        ai_score = None
    levels = parsed.get("key_levels")
    support = resistance = None
    if isinstance(levels, dict):
        support = _as_float_or_none(levels.get("support"))
        resistance = _as_float_or_none(levels.get("resistance"))
    return {
        "ai_score": ai_score,
        "verdict": verdict if verdict in _VALID_VERDICTS else "neutral",
        "agreement": agreement if agreement in _VALID_AGREEMENTS else "partial",
        "summary": summary[:200],
        "strengths": _as_str_list(parsed.get("strengths"), 3),
        "concerns": _as_str_list(parsed.get("concerns"), 3),
        "fake_breakout_risk": risk if risk in _VALID_RISKS else "medium",
        "key_levels": {"support": support, "resistance": resistance},
        "watch_points": _as_str_list(parsed.get("watch_points"), 3),
        "rule_score_comment": str(parsed.get("rule_score_comment") or "").strip()[:300],
    }


# ————————————————————————————————————————————————————————————————
# JV03（Jev 决策模型接入路线图 2026-09-21）：判定层
# ————————————————————————————————————————————————————————————————

# Jev 复核题型/state 版本：改动题目、等级或 state 字段必须递增（《契约》§B-8 版本化铁律）。
JEV_REVIEW_SCHEMA_VERSION = "1.0"

JEV_QUESTION_RELIABILITY = "reliability"
JEV_QUESTION_AGREEMENT = "agreement"
JEV_QUESTION_FAKE_BREAKOUT = "fake_breakout"

# 可靠度 rubric：5 级**描述性**等级（《契约》§E1 用户拍板：单题 rubric，不做多题复合）。
#
# 官方实测结论（见《契约》C2）：`score` 的 criteria 必须**描述情境而非程度**——
# 只给数字等级（如 `["0","1","2"]`）会让概率在相邻级间摊平、confidence 掉到 0.33；
# 描述性等级才会给出单峰高置信答案。所以这里每级都写「什么情况下选它」，不写「几分」。
#
# 归一化：`score` 是等级轴上的概率加权均值（0…4），展示分 = score/(5-1)×100。
JEV_RELIABILITY_LEVELS: tuple[str, ...] = (
    "很可能是假象：形态未完成或已明显走坏，这批信号不该采信",
    "偏弱：形态能看出来，但量能或位置不配合，可靠性低于规则分暗示",
    "基本可信：形态与量价位置大体一致，与规则分的判断相当",
    "可靠：形态清晰、量价位置互相印证，规则分没有高估",
    "很可靠：多重独立证据共振且位置有利，规则分可能还低估了",
)

# 认同度选项（choice 的 criteria 是「选项 → 描述」映射）。
JEV_AGREEMENT_OPTIONS: dict[str, str] = {
    "agree": "认同：规则分恰当地反映了这批信号的可靠性，没有系统性高估或低估",
    "partial": "部分认同：大方向对，但存在规则分没覆盖到的疑点",
    "disagree": "不认同：规则分系统性高估或低估了这批信号的可靠性",
}

# 假突破风险由 `noul` 三路划分得来（《契约》C1：noul **无 confidence**，只能按值分档）。
# 高值 = 「存在风险」（官方建议措辞让高值代表「是」，见 `jev_client.noul_question` 注释）。
_JEV_BREAKOUT_RISK = {"yes": "high", "no": "low", "uncertain": "medium"}


def jev_state(
    *,
    symbol: str,
    name: str,
    rule_score: dict[str, Any],
    indicators: dict[str, Any],
    bars: list[dict[str, Any]],
    sector: dict[str, Any] | None = None,
    question: str = "",
) -> str:
    """Jev 复核的 state（**数据部分**；指令部分在 `jev_questions` 的 `instructions` 里）。

    白名单（《契约》§F）：规则分各分项 + 命中明细 + 指标现值 + 近期 K 线摘要 + 板块上下文。
    明确排除：账户/持仓明细、密钥、用户备注原文。

    与 `review_messages` 的 user 文本**同源**（复用同一批 `_bars_summary`/`_hit_detail_text`/
    `_score_parts_text`），保证「切到 Jev」不会悄悄改变喂给模型的证据范围——否则规则分与
    AI 分的分歧就不再可比。

    ⚠️ state 会出网到 TypeSafe（美国托管、默认非零留存，见设置页「数据去向」），
    因此这里的字段范围就是**实际出网的字段范围**，不得随手加字段。
    """
    sector_text = ""
    if sector:
        sector_text = (
            f"\n【所属板块】{sector.get('name') or sector.get('code')}"
            f"（当日 {_fmt_num(sector.get('change_pct'))}%，成分 {sector.get('member_count')} 只"
            f"{('，领涨 ' + str(sector.get('leader_name'))) if sector.get('leader_name') else ''}）"
        )
    return (
        f"【标的】{symbol} {name}\n"
        f"【命中窗口】最近 {rule_score.get('signal_window_bars')} 根 K 线"
        f"（自 {rule_score.get('signal_window_from')}）\n"
        f"【规则质量分】{rule_score.get('tactic_score')}（公式版本 {rule_score.get('score_formula_version')}，"
        f"权重版本 {rule_score.get('weight_version')}）｜方向 {rule_score.get('direction_bias')}\n"
        f"【规则分项】{_score_parts_text(rule_score)}\n"
        f"【量能/位置】量比 {rule_score.get('volume_ratio')}（回看 {rule_score.get('volume_lookback_bars')} 根）；"
        f"区间位置 {rule_score.get('position')}（近 {rule_score.get('position_window_bars')} 根的 high-low 区间，0=最低 1=最高）；"
        f"被同族合并的族：{rule_score.get('merged_families') or '无'}\n"
        f"【命中明细】\n{_hit_detail_text(rule_score)}\n"
        f"【指标现值】{'；'.join(f'{k}={v}' for k, v in list(indicators.items())[:20])}\n"
        f"{sector_text}\n"
        f"【近期 K 线（日）】\n{_bars_summary(bars)}\n"
        f"【共 {len(bars)} 根日 K 线（回看 250 根上限）】"
        f"{('【用户关注点】' + question) if question else ''}"
    )


def jev_questions() -> dict[str, dict[str, Any]]:
    """JV03 三题：可靠度（score）／认同度（choice）／假突破风险（noul）。

    只问规则引擎算不出的三件事，且与 `normalize_review` 的三个结构化字段一一对应
    （`ai_score` / `agreement` / `fake_breakout_risk`）——这样 Jev 判定层与 chat 判定层
    可以**同构替换**，前端与落库结构都不用改形状。

    三题一次请求并行评估（官方 fan-out 模式：加题几乎不加时延，但**加题会增加输入 token**，
    输入计费——所以「够用就好」，不为了凑数加题）。
    """
    return {
        JEV_QUESTION_RELIABILITY: jev_client.score_question(
            "依据给定的规则分项、命中明细、指标现值与近期 K 线，独立判断这批技术面信号的"
            "**可靠度**。注意口径区别：规则分衡量的是证据**强度**（形态权重×新鲜度等），"
            "这里要判断的是证据**可信度**——形态是不是真的成立、量价与位置是否互相印证。"
            "请在下列等级中选最贴合现状的一个：\n"
            + "\n".join(f"{index}）{level}" for index, level in enumerate(JEV_RELIABILITY_LEVELS)),
            list(JEV_RELIABILITY_LEVELS),
        ),
        JEV_QUESTION_AGREEMENT: jev_client.choice_question(
            "规则引擎用固定公式算出的质量分，是否恰当地反映了这批信号的真实可靠性？"
            "（即规则分有没有系统性高估或低估）",
            JEV_AGREEMENT_OPTIONS,
        ),
        JEV_QUESTION_FAKE_BREAKOUT: jev_client.noul_question(
            "这批信号里是否存在「看起来像但不可靠」的风险——假突破、量价背离，或形态尚未完成？"
            "存在则判定值应接近 1。",
            true_means="存在明确的假突破／量价背离／形态未完成风险",
            false_means="形态与量价位置互相印证，未发现这类风险",
        ),
    }


def jev_judgement(answers: Mapping[str, Any]) -> dict[str, Any]:
    """把 Jev 三题应答归一化成判定层字段（与 `normalize_review` 的三个字段同构）。

    `answers` 是 `{question_id: JevAnswer}`。**任何一题缺失或异常都回退中性值、不抛错**——
    判定层是软校验，单题不可用不该让整次复核失败（那正是今天 502 的成因，见 JV03 验收）。
    缺题时的中性值：可靠度 `None`（前端显示「—」而不是编一个分）、认同度 `partial`、
    假突破风险 `medium`。

    同时带出**可复算的原始值**（`ai_score_raw` / `ai_score_levels` / `noul_value` /
    `noul_verdict` / `confidence`）——原始值与归一化分都要留痕，否则事后无法复核
    「这个 60 分是按几级 rubric 算出来的」（对齐 `TACTIC_WEIGHT_VERSION` 的留痕惯例）。
    """
    reliability = answers.get(JEV_QUESTION_RELIABILITY)
    agreement = answers.get(JEV_QUESTION_AGREEMENT)
    breakout = answers.get(JEV_QUESTION_FAKE_BREAKOUT)

    levels = len(JEV_RELIABILITY_LEVELS)
    raw_score = getattr(reliability, "score", None)
    ai_score: float | None = None
    if raw_score is not None:
        # 归一化：score 在等级轴 0…levels-1 上，换算成 0–100 与规则分同量纲（《契约》C2）。
        normalized = max(0.0, min(1.0, float(raw_score) / (levels - 1)))
        ai_score = round(normalized * 100, 1)

    picked = getattr(agreement, "choice", None)
    agreement_value = str(picked) if picked in _VALID_AGREEMENTS else "partial"

    noul_value = getattr(breakout, "noul", None)
    verdict = breakout.noul_verdict() if breakout is not None else None
    risk = _JEV_BREAKOUT_RISK.get(verdict or "", "medium")

    confidence = getattr(agreement, "confidence", None)

    return {
        "ai_score": ai_score,
        "ai_score_raw": float(raw_score) if raw_score is not None else None,
        "ai_score_levels": levels,
        "agreement": agreement_value,
        "fake_breakout_risk": risk,
        "confidence": float(confidence) if confidence is not None else None,
        "noul_value": float(noul_value) if noul_value is not None else None,
        "noul_verdict": verdict,
    }


# 判定层字段（Jev 优先）与叙述层字段（只能来自 chat）的分界。
_JUDGEMENT_FIELDS = ("ai_score", "agreement", "fake_breakout_risk")

# 合并后的 `payload.review` 默认形状：与 `normalize_review` 的输出键完全一致，
# 这样前端 `AiReviewContent` 不用改，旧记录也能原样读出。
_REVIEW_VIEW_DEFAULTS: dict[str, Any] = {
    "ai_score": None,
    "verdict": "neutral",
    "agreement": "partial",
    "summary": "",
    "strengths": [],
    "concerns": [],
    "fake_breakout_risk": "medium",
    "key_levels": {"support": None, "resistance": None},
    "watch_points": [],
    "rule_score_comment": "",
}


def review_view(
    *, judgement: dict[str, Any] | None, narrative: dict[str, Any] | None
) -> dict[str, Any] | None:
    """把「Jev 判定层」与「chat 叙述层」合成前端 `payload.review` 的同一形状。

    合并规则（顺序很重要）：
    1. 先用叙述层铺满所有字段（`normalize_review` 的输出天然是完整形状）；
    2. 再用判定层**覆盖**三个判定字段（`ai_score` / `agreement` / `fake_breakout_risk`）——
       Jev 是专职判定、可复算、无 JSON 解析风险，优先级高于 chat 顺带给出的同名字段；
    3. 两层都拿不到 → 返回 `None`，调用方如实标注「本轮未复核」，**不再抛 502**。

    `verdict`（bullish/neutral/bearish）留在叙述层：它是对技术面倾向的一句话概括，
    Jev 的三题里没有这一题（用户拍板「不需要 Jev 单独做」，不为它单独加题）。
    """
    if judgement is None and narrative is None:
        return None
    merged = dict(_REVIEW_VIEW_DEFAULTS)
    if narrative:
        for key in merged:
            if key in narrative:
                merged[key] = narrative[key]
    if judgement:
        for key in _JUDGEMENT_FIELDS:
            merged[key] = judgement.get(key)
    return merged


# ---------------------------------------------------------------------------
# JV05（P1）：全市场扫描批量去误报
# ---------------------------------------------------------------------------
#
# 场景：`/tactics/scan-market` 粗筛出候选后、进结果列表前，逐票问 Jev「这个形态是否真实成立」。
#
# 定位与铁律（与 JV03/JV04 一致，勿漂移）：
# - **软校验**：只标注、只分组，**绝不删除候选**。判为「疑似误报」的票仍然出现在结果里，
#   只是被标出来、折叠进「待核验」——用户随时能展开看。删掉一行就是改写规则引擎的输出。
# - **可整层关闭**：Jev 未启用或总闸关闭时，本层一个字节都不产生，扫描链路与接入前逐字节一致。
# - **本模块纯函数**：只做「构造 state / 构造题面 / 合并应答 / 统计」，出网由调用方负责。
#
# 与 JV03 的关键差别：JV03 是一票一次（用户手动点），JV05 是**一批几十票一次**——因此
# 分片与预算成了主角（见 `jev_scan_shards`）。这也是 R1「只按输入 token 计费」的直接后果：
# 批量场景的成本全在 state 上，票越多越要压每票的字数。
# ---------------------------------------------------------------------------

JEV_SCAN_SCHEMA_VERSION = "1.0"

#: 逐票题 id：`c{index}_tactic_valid`。index 是**跨分片的全局编号**，与 state 里的「候选 N」
#: 一致——全局编号让合并时不必再管「这条应答属于哪个分片」，少一层出错空间。
JEV_SCAN_QUESTION_SUFFIX = "tactic_valid"

#: 三选项。用描述性选项而不是「是/否」，因为「存疑」与「不成立」是两种不同处置：
#: 前者是「证据不足」、后者是「更像是误报」，两者的复核动作不一样，不该混成一档。
JEV_SCAN_OPTIONS: dict[str, str] = {
    "valid": "成立：命中明细里的信号在 K 线走势上确实成立，不是数据错位或口径误判",
    "doubtful": "存疑：有信号，但证据不足以确认形态成立（例如量能未配合、价位未确认）",
    "invalid": "不成立：命中更像误报——例如信号由复权、停牌、涨跌停等非形态因素造成",
}
JEV_SCAN_LABELS: dict[str, str] = {
    "valid": "形态成立",
    "doubtful": "形态存疑",
    "invalid": "疑似误报",
}

#: 分片上限：路线图验收口径是「批量 50 票单请求完成」。
JEV_SCAN_MAX_PER_SHARD = 50
#: 单次扫描最多复核多少票。超出部分**如实计入 `skipped`**（不是静默不标）。
#: 200 票 ≈ 4 个分片 ≈ 4 次请求；再往上收益递减而时延线性增长（扫描本身已是分钟级任务）。
JEV_SCAN_MAX_CANDIDATES = 200
#: state 预算：官方「state + 最长单题 ≤ 32k」，留足余量取 20k。
JEV_SCAN_STATE_MAX_CHARS = 20_000
#: 单票块上限。单票超过它就按「降级阶梯」逐级削减（见 `jev_scan_item_text`），
#: 而不是截一半——半截证据会得出错误的「不成立」。
JEV_SCAN_ITEM_MAX_CHARS = 2_400
#: 尾部 K 线根数（与 JV03 `_bars_summary` 默认值同源，保证两处「近期 K 线」是同一口径）。
JEV_SCAN_BARS_TAIL = 12
#: 指标现值最多带几项（scan 行的 `indicators` 可能有二三十项，全带会挤掉 K 线摘要）。
JEV_SCAN_INDICATOR_LIMIT = 12

#: 「低置信 → 待核验」的地板线。
#:
#: ⚠️ **未标定**：R6 只标定了 `noul` 三路阈值与 `partial` 降级开关，本场景（choice 的
#: confidence 分档）**没有中文样本**，故沿用官方英文范例的 0.5 地板线。之所以敢这样用，
#: 是因为它**只影响展示分组**（折叠），不影响任何判定、排序或降级——标定缺失的代价可控。
#: 一旦拿到本场景的中文样本，应连同 JV06/JV07 的阈值一起重标（《契约》§A1 的纪律）。
JEV_SCAN_CONFIDENCE_FLOOR = 0.5

#: 逐票复核状态。`None`（未标注）与 `pending_verification` 是两件事：
#: 前者是「这一票根本没送出去评」（超限/分片失败），后者是「评了但没通过」。
JEV_SCAN_STATE_REVIEWED = "reviewed"
JEV_SCAN_STATE_PENDING = "pending_verification"


def jev_scan_bars_tail(bars: Sequence[Mapping[str, Any]] | None) -> str:
    """取「进 state 的尾部 K 线摘要」——**唯一入口**，确保与 JV03 同口径。

    单独开一个公开函数而不是让调用方直接用 `_bars_summary`：扫描引擎（`api/app.py`）
    在拉完 K 线的那一刻顺手算出这段文本，若两处各写一遍「取最近几根」，迟早会漂成
    两个口径，而 Jev 的形态判断完全依赖这段证据的一致性。根数由 `JEV_SCAN_BARS_TAIL`
    钉死（与 JV03 复用同一默认值）。
    """
    return _bars_summary(list(bars or []), JEV_SCAN_BARS_TAIL)


def jev_scan_item_text(
    row: Mapping[str, Any],
    *,
    bars_tail: str = "",
    max_chars: int = JEV_SCAN_ITEM_MAX_CHARS,
) -> tuple[str, str | None]:
    """单票进 state 的文本块（《契约》§F：命中明细 + 尾部 K 线摘要）。

    返回 `(文本, 降级说明)`；降级说明非 None 表示为了塞进预算削掉了什么，须如实透传。

    **降级阶梯**（而不是截断）——顺序按「对判定的重要性」从低到高削：
      ① 完整（命中明细 + 指标现值 + 尾部 K 线摘要）；
      ② 超预算 → 去掉尾部 K 线摘要（形态判断仍能从命中明细的触发价/失效条件看出来）；
      ③ 再超 → 去掉指标现值；
      ④ 还超 → 返回空串，调用方把这一票计入 `skipped`（`over_item_budget`），**不硬塞半截**。

    为什么不直接 `text[:max_chars]`：命中明细里的「触发价 / 失效条件」常在块尾，截断会把
    最关键的判据切掉，而 Jev 拿到残缺证据会自信地判「不成立」——这比不判更坏。
    """
    symbol = str(row.get("symbol") or "")
    name = str(row.get("name") or "")
    hit_text = _hit_detail_text(row)
    indicator_items = list((row.get("indicators") or {}).items())[:JEV_SCAN_INDICATOR_LIMIT]
    indicators_text = "；".join(f"{key}={value}" for key, value in indicator_items) or "（无）"

    def _build(*, with_bars: bool, with_indicators: bool) -> str:
        lines = [
            f"【标的】{symbol} {name}",
            (
                f"【形态方向】{row.get('direction_bias') or '—'}"
                f"｜规则质量分 {_fmt_num(row.get('tactic_score'))}"
                f"（公式版本 {row.get('score_formula_version') or '—'}）"
            ),
            f"【规则分项】{_score_parts_text(row)}",
            f"【量能/位置】量比 {_fmt_num(row.get('volume_ratio'))}；区间位置 {_fmt_num(row.get('position'))}",
        ]
        if not bool(row.get("eligible", True)):
            reasons = row.get("gating_reasons")
            lines.append(f"【未过硬门】{'；'.join(str(item) for item in reasons) if reasons else '（未给原因）'}")
        lines.append(f"【命中明细】\n{hit_text}")
        if with_indicators:
            lines.append(f"【指标现值】{indicators_text}")
        if with_bars:
            lines.append(f"【近期 K 线（日）】\n{bars_tail or '（无 K 线数据）'}")
        return "\n".join(lines)

    full = _build(with_bars=True, with_indicators=True)
    if len(full) <= max_chars:
        return full, None
    without_bars = _build(with_bars=False, with_indicators=True)
    if len(without_bars) <= max_chars:
        return without_bars, "为塞进预算已去掉尾部 K 线摘要"
    without_indicators = _build(with_bars=False, with_indicators=False)
    if len(without_indicators) <= max_chars:
        return without_indicators, "为塞进预算已去掉尾部 K 线摘要与指标现值"
    return "", "单票命中明细超预算，整票未送评"


def jev_scan_questions(evaluated: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """为一个分片里的每票生成一道 `choice` 题（一票一题，官方 fan-out）。"""
    questions: dict[str, dict[str, Any]] = {}
    for item in evaluated:
        questions[f"c{item['index']}_{JEV_SCAN_QUESTION_SUFFIX}"] = jev_client.choice_question(
            "该票的形态是否真实成立？判断依据是「命中明细里的信号」与「近期 K 线走势」是否自洽："
            "信号描述的价格行为确实发生过、且不是复权/停牌/涨跌停等非形态因素造成的假象。"
            "只看给出的证据，不要引入外部消息或基本面。",
            JEV_SCAN_OPTIONS,
        )
    return questions


def jev_scan_shards(
    items: Sequence[Mapping[str, Any]],
    *,
    max_per_shard: int = JEV_SCAN_MAX_PER_SHARD,
    max_chars: int = JEV_SCAN_STATE_MAX_CHARS,
    item_max_chars: int = JEV_SCAN_ITEM_MAX_CHARS,
) -> dict[str, Any]:
    """把候选切成若干分片，每片一份 state + 题面。

    `items` 每项：`{"symbol", "name", "row", "bars_tail"}`（`row` 是扫描结果行，
    `bars_tail` 由调用方在取数时顺带算出——**不重新拉 K 线**，否则等于把取数成本翻倍）。

    返回 `{"shards": [...], "skipped": [...]}`，其中

      shard    = `{"state", "questions", "evaluated": [{"index", "symbol", "name"}]}`
      skipped  = `{"symbol", "reason", "note"}`，reason ∈
                 `over_candidate_limit`（超出单轮复核上限）/
                 `over_item_budget`（单票超预算）/ `empty_state`（无可用文本）

    **分片策略（路线图验收要求写明）**：双重上限——每片 ≤ `max_per_shard` 票（验收口径 50）
    **且** ≤ `max_chars` 字 state。票数先到就切，字数先到也切；一票放不进当前片就开新片
    （而不是把它挤掉），只有「单票独占一片仍超预算」才计入 skipped。这样既不会因为个别
    长票把整批挤成很多小片，也不会把任何一票静默丢掉。

    `index` 是**跨分片全局编号**（1 起），题号据此生成 → 合并应答时无需知道分片边界。
    """
    shards: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    blocks: list[tuple[Mapping[str, Any], int, str]] = []

    for position, item in enumerate(items):
        symbol = str(item.get("symbol") or "")
        if position >= JEV_SCAN_MAX_CANDIDATES:
            skipped.append({
                "symbol": symbol,
                "reason": "over_candidate_limit",
                "note": f"超出单轮复核上限 {JEV_SCAN_MAX_CANDIDATES} 票，本轮未送评",
            })
            continue
        text, degraded = jev_scan_item_text(item.get("row") or {}, bars_tail=str(item.get("bars_tail") or ""), max_chars=item_max_chars)
        if not text:
            skipped.append({"symbol": symbol, "reason": "over_item_budget", "note": degraded or "超预算"})
            continue
        blocks.append((item, len(blocks) + 1, text))

    current: list[tuple[Mapping[str, Any], int, str]] = []
    used = 0

    def _flush() -> None:
        nonlocal current, used
        if not current:
            return
        evaluated = [
            {"index": index, "symbol": str(item.get("symbol") or ""), "name": str(item.get("name") or "")}
            for item, index, _text in current
        ]
        shards.append({
            "state": "\n\n".join(f"【候选 {index}】\n{text}" for _item, index, text in current),
            "questions": jev_scan_questions(evaluated),
            "evaluated": evaluated,
        })
        current = []
        used = 0

    for item, index, text in blocks:
        block_len = len(text) + 24  # 「【候选 N】」前缀与分隔
        if current and (len(current) >= max_per_shard or used + block_len > max_chars):
            _flush()
        current.append((item, index, text))
        used += block_len
        if len(current) >= max_per_shard:
            _flush()
    _flush()

    if not shards:
        return {"shards": [], "skipped": skipped}
    return {"shards": shards, "skipped": skipped}


def jev_scan_annotations(shard_results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """把各分片的应答合并成逐票标注：`{symbol: {...}}`。

    `shard_results` 每项：`{"evaluated": [...], "answers": {question_id: JevAnswer}, "error": str | None}`。

    合并规则（**宁可标「未取得」也不默认通过**）：
      - 分片带 `error`（出网失败）→ 该片全部标 `review_state=None` + 失败原因，**不猜**；
      - 题号缺答 → 同上（模型没回这题不等于形态成立）；
      - `valid` 且 `confidence ≥ floor` → `reviewed`；
      - 其余（`doubtful` / `invalid` / 低置信 / 无 confidence）→ `pending_verification`。

    「低置信」单列一条 note 而不是并进 `doubtful`：前者是「模型自己也没把握」，
    后者是「模型明确认为存疑」——用户看到的复核动作不一样。
    """
    annotations: dict[str, dict[str, Any]] = {}
    for result in shard_results:
        error = result.get("error")
        answers = result.get("answers") or {}
        for item in result.get("evaluated") or []:
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            if error:
                annotations[symbol] = {
                    "verdict": None,
                    "label": "未取得判定",
                    "confidence": None,
                    "review_state": None,
                    "note": f"本票所在分片未取得应答（{error}），未标注——不等于形态成立",
                }
                continue
            answer = answers.get(f"c{item['index']}_{JEV_SCAN_QUESTION_SUFFIX}")
            picked = getattr(answer, "choice", None) if answer is not None else None
            if picked not in JEV_SCAN_OPTIONS:
                annotations[symbol] = {
                    "verdict": None,
                    "label": "未取得判定",
                    "confidence": None,
                    "review_state": None,
                    "note": "本票未取得应答（题号缺答或选项未定义），未标注——不等于形态成立",
                }
                continue
            confidence = getattr(answer, "confidence", None)
            confidence_value = float(confidence) if isinstance(confidence, (int, float)) else None
            if picked == "valid" and confidence_value is not None and confidence_value >= JEV_SCAN_CONFIDENCE_FLOOR:
                state = JEV_SCAN_STATE_REVIEWED
                note = f"语义复核通过（置信度 {confidence_value:.2f}）"
            elif picked == "valid":
                state = JEV_SCAN_STATE_PENDING
                note = (
                    f"语义判定成立，但置信度 {confidence_value:.2f} 低于地板 "
                    f"{JEV_SCAN_CONFIDENCE_FLOOR:.2f}，进待核验"
                    if confidence_value is not None
                    else "语义判定成立但未返回置信度，无法确认，进待核验"
                )
            else:
                state = JEV_SCAN_STATE_PENDING
                note = f"语义判定「{JEV_SCAN_LABELS[picked]}」，进待核验"
            annotations[symbol] = {
                "verdict": picked,
                "label": JEV_SCAN_LABELS[picked],
                "confidence": confidence_value,
                "review_state": state,
                "note": note,
            }
    return annotations


def jev_scan_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """展示层统计行（纯函数，与标注写入方式解耦——测试可直接喂构造行）。"""
    reviewed = pending = unannotated = 0
    verdicts = {"valid": 0, "doubtful": 0, "invalid": 0}
    for row in rows:
        state = row.get("jev_review_state")
        if state == JEV_SCAN_STATE_REVIEWED:
            reviewed += 1
        elif state == JEV_SCAN_STATE_PENDING:
            pending += 1
        else:
            unannotated += 1
        verdict = row.get("jev_verdict")
        if verdict in verdicts:
            verdicts[verdict] += 1
    return {
        "total": len(rows),
        "reviewed": reviewed,
        "pending": pending,
        "unannotated": unannotated,
        "verdicts": verdicts,
    }
