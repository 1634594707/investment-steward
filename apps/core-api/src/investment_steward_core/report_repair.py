"""研报定向修复（G1，研报产出与交付链路路线图 2026-09-27）。

**要解决的问题**：闸门「发现得准」，但**修不了**——摘要过短、一句话结论缺失只写进
「质量校验痕迹」，报告照发；界面上没有任何一个「补写摘要 / 补写结论 / 重新生成本节」
的入口，用户只能重新发起一次完整生成（又一次模型调用 + 一次等待），
而系统已经精确知道缺什么。

**本模块的定位**：把「按失败类型定向修复」抽成**唯一一处**实现，供两条路径共用：

1. **生成端点**（`POST /evidence/stock-research-report` 的 draft 阶段）：
   初稿不完整时自动修复一轮（`body.with_auto_repair`，默认开）；
2. **修复端点**（`POST /ai-research/reports/{report_id}/repair`）：
   用户在「质量校验痕迹」里点动作按钮时执行。

两处共用同一套提示词与同一套验收条件，避免「自动修复」与「手动修复」产出不一致——
这是本模块存在的唯一理由，任何一处单独改提示词都会让两条路径分叉。

**铁律（沿自 v31 §八.4）**：只依据给定来源与既有正文；不新增数字/事件/判断；
不为过检补标题、重复内容或编造事实；证据不足就如实写「证据不足」。
"""

from __future__ import annotations

from typing import Any

from . import model_client, report_quality

# 修复路径的系统提示词（三种定向修复各一句，措辞随动作收窄，不给模型越界的余地）。
SYSTEM_SUMMARY_REWRITE = "你是严谨的研究编辑：只做摘要压缩改写，绝不新增信息，绝不改动事实口径。"
SYSTEM_SUMMARY_GENERATE = "你是严谨的研究编辑：只做摘要撰写，绝不新增信息，绝不改动事实口径。"
SYSTEM_CONCLUSION = "你是严谨的研究编辑：只补写一句话结论，绝不新增信息，绝不改动事实口径。"
SYSTEM_SECTION = "你是严谨的研究编辑：只补写指定的缺失小节，绝不新增证据里不存在的信息。"


def summary_rewrite_prompt(*, title: str, summary: str, reason: str) -> tuple[str, str]:
    """摘要「落在目标区间之外」→ 改写提示词（四句话结构）。

    D04（>165）与 G1（<150）共用同一条提示词，只在 `_band_note` 上区分方向：
    过长与过短是同一缺陷的两个方向（缺「结论/证据/反证/验证动作」四要素之一），
    修复动作相同。
    """
    chars = report_quality.report_char_count(summary)
    band_note = (
        f"超出目标区间 {report_quality.SUMMARY_TARGET_MIN}-{report_quality.SUMMARY_TARGET_MAX} 字"
        if reason == "summary_over_target"
        else f"低于目标区间 {report_quality.SUMMARY_TARGET_MIN}-{report_quality.SUMMARY_TARGET_MAX} 字"
    )
    user = (
        f"研报标题：{str(title or '').strip()}\n"
        f"原执行摘要（{chars} 字，{band_note}）：\n"
        f"{summary}\n\n"
        "请把这份执行摘要改写为四句话结构、150-165 字："
        "①方向性结论 ②最强的基本面或估值依据 ③最重要的技术面或资金面依据 ④最大的风险/反证。"
        "只输出改写后的摘要纯文本（不要 JSON、不要前缀、不要解释）；"
        "只允许使用原摘要与标题中已出现的信息，不得新增任何数字、事件或判断。"
    )
    return SYSTEM_SUMMARY_REWRITE, user


def summary_generate_prompt(*, title: str, report: str) -> tuple[str, str]:
    """摘要**为空** → 从正文补写（与「改写已有摘要」是不同的动作，提示词也不同）。"""
    user = (
        f"研报标题：{str(title or '').strip()}\n"
        f"研报正文：\n{str(report or '')[:4000]}\n\n"
        "请为这份研报补写执行摘要，四句话结构、150-165 字："
        "①方向性结论 ②最强的基本面或估值依据 ③最重要的技术面或资金面依据 ④最大的风险/反证。"
        "只输出摘要纯文本（不要 JSON、不要前缀、不要解释）；"
        "只允许使用正文中已出现的信息，不得新增任何数字、事件或判断。"
    )
    return SYSTEM_SUMMARY_GENERATE, user


def conclusion_prompt(*, symbol: str, title: str, report: str) -> tuple[str, str]:
    """一句话结论缺失 → 定向补写（首屏结论卡的第一行，成本远低于重跑整稿）。

    要求 `direction` 必须落在 `ALLOWED_CONCLUSION_DIRECTIONS` 内（越界的返回值会被
    丢弃，见 `apply_repair`），`confidence` 同理只接受三档。
    """
    directions = "/".join(report_quality.ALLOWED_CONCLUSION_DIRECTIONS)
    levels = "/".join(report_quality.ALLOWED_CONFIDENCE)
    user = (
        f"研究对象：A 股 {symbol}。研报标题：{str(title or '').strip()}\n"
        f"研报正文：\n{str(report or '')[:4000]}\n\n"
        "这份研报缺少「一句话结论」（首屏结论卡的第一行）。请只补写这一句结论。\n"
        "请输出严格 JSON（无 markdown 围栏）：\n"
        '{"statement": "一句话结论（40-80 字：先说方向，再说最核心的多空矛盾）", '
        f'"direction": "{directions} 之一", '
        '"core_conflict": "最核心的多空矛盾（30 字以内）", '
        f'"confidence": "{levels} 之一"}}\n'
        "铁律：只依据正文中已出现的信息组织这一句，不得新增任何数字、事件或判断；"
        "正文若证据不足，就如实写成「证据不足」口径，不得编造；"
        f"direction 必须取给定值之一（{directions}），拿不准就填「多空交织」。"
    )
    return SYSTEM_CONCLUSION, user


def section_prompt(
    *,
    symbol: str,
    question: str,
    numbered: str,
    unavailable: str,
    report: str,
    section_name: str,
) -> tuple[str, str]:
    """缺一个必需小节 → 只补写这一节。"""
    user = (
        f"研究对象：A 股 {symbol}。用户关注点：{question.strip() or '综合趋势与基本面'}\n\n"
        f"{numbered}{unavailable}\n\n"
        f"现有报告缺失「{section_name}」小节，正文其余部分如下：\n{str(report or '')[:4000]}\n\n"
        "请只补写这一个缺失小节。请输出严格 JSON（无 markdown 围栏）：\n"
        f'{{"section": "{section_name}", "content": "以 ### {section_name} 开头的 markdown 正文（300-600 字；'
        '每条判断末尾挂 [S1..S5] 来源标注；证据不足时如实写「无数据」并解释原因）"}}\n'
        "铁律：只依据给定来源；禁止编造来源里不存在的数字或事件；"
        "禁止重复其他小节已有的内容；禁止为通过检查补标题、重复内容或编造事实。"
    )
    return SYSTEM_SECTION, user


def _call(profile: Any, store: Any, prompts: tuple[str, str]) -> str:
    """统一走 `model_client.call_active_model`（与生成路径同一条出网通道）。

    打桩点在 `model_client._post_json`，因此自动修复与手动修复在测试里共用同一套桩。
    """
    system_text, user_text = prompts
    reply = model_client.call_active_model(
        profile,
        store,
        model_client.format_messages(system_text, user_text),
        purpose="研报",
    )
    return (reply.content or "").strip()


def rewrite_summary(
    *, parsed: dict[str, Any], profile: Any, store: Any, reason: str
) -> tuple[bool, str, int]:
    """摘要重写（D04/G1）：返回 (是否采纳, 说明, 改写后字数)。

    采纳条件：落进目标区间；或**朝正确方向改善**且未超硬上限（改善版保留，软告警继续标注）。
    绝不静默截断，也绝不二次自动重试。
    """
    original = str(parsed.get("executive_summary", "") or "").strip()
    original_chars = report_quality.report_char_count(original)
    written = _call(
        profile,
        store,
        summary_rewrite_prompt(title=str(parsed.get("title", "")), summary=original, reason=reason),
    )
    written_chars = report_quality.report_char_count(written)
    in_target_band = (
        report_quality.SUMMARY_TARGET_MIN <= written_chars <= report_quality.SUMMARY_TARGET_MAX
    )
    improved = (
        written_chars < original_chars
        if reason == "summary_over_target"
        else written_chars > original_chars
    )
    if written and (in_target_band or (written_chars <= report_quality.MAX_SUMMARY_CHARS and improved)):
        parsed["executive_summary"] = written
        return True, "", written_chars
    return (
        False,
        "重写输出为空、未朝目标区间改善或仍超硬上限，保留原文（软告警标注，交人工复核）。",
        written_chars,
    )


def apply_repair(
    *,
    strategy: str,
    parsed: dict[str, Any],
    profile: Any,
    store: Any,
    symbol: str,
    question: str = "",
    numbered: str = "",
    unavailable: str = "",
    allowed_citations: set[str],
    missing_sections: list[str],
    full_rerun_prompts: tuple[str, str] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """按 `strategy` 执行**一次**定向修复；返回 `(parsed, repaired, detail)`。

    - `summary_only`：摘要为空 → 从正文补写；摘要落在目标区间外 → 按方向改写。
    - `conclusion_only`：补写一句话结论（越界 direction/confidence 丢弃，不臆造）。
    - `section_only`：只重新生成 `missing_sections[0]` 并合并回正文，引用清单同步扩充。
    - `full_rerun`：整稿重跑，**必须**由调用方给出 `full_rerun_prompts`（需要当时的
      完整证据包，只有生成路径拿得到）。

    任何异常都由调用方捕获后如实留痕（`detail` 里写明），本函数不吞异常、不伪造成功。
    """
    if strategy == report_quality.REPAIR_STRATEGY_FULL_RERUN:
        if not full_rerun_prompts:
            return parsed, False, "整稿重跑需要当时的完整证据包，本入口无法执行（请在标的页重新生成）。"
        from . import macro_ai  # 局部导入：仅整稿重跑需要解析完整 JSON

        rerun_raw = _call(profile, store, full_rerun_prompts)
        rerun_parsed = macro_ai.extract_json_object(rerun_raw)
        if (
            isinstance(rerun_parsed, dict)
            and isinstance(rerun_parsed.get("report"), str)
            and rerun_parsed.get("report", "").strip()
            and rerun_parsed.get("citations")
        ):
            return rerun_parsed, True, "重跑完整初稿（同证据包、同模式）。"
        return parsed, False, "重跑输出无法解析为带引用的报告 JSON，保留原稿。"

    if strategy == report_quality.REPAIR_STRATEGY_SUMMARY:
        original = str(parsed.get("executive_summary", "") or "").strip()
        if not original:
            written = _call(
                profile,
                store,
                summary_generate_prompt(title=str(parsed.get("title", "")), report=str(parsed.get("report", ""))),
            )
            if written and report_quality.report_char_count(written) <= report_quality.MAX_SUMMARY_CHARS:
                parsed["executive_summary"] = written
                return parsed, True, "只生成缺失的执行摘要（正文未改写）。"
            return parsed, False, "摘要生成输出为空或超上限，保留原状。"
        reason = report_quality.summary_rewrite_reason(original) or "summary_below_target"
        accepted, detail, _chars = rewrite_summary(parsed=parsed, profile=profile, store=store, reason=reason)
        if accepted:
            return parsed, True, "只重写执行摘要（正文未改写）。"
        return parsed, False, detail

    if strategy == report_quality.REPAIR_STRATEGY_CONCLUSION:
        from . import macro_ai  # 局部导入：仅结论补写需要解析 JSON

        existing = report_quality.normalize_conclusion(parsed.get("conclusion"))
        raw = _call(
            profile,
            store,
            conclusion_prompt(
                symbol=symbol,
                title=str(parsed.get("title", "")),
                report=str(parsed.get("report", "")),
            ),
        )
        new_conclusion = report_quality.normalize_conclusion(macro_ai.extract_json_object(raw))
        if not new_conclusion["statement"]:
            return parsed, False, "结论补写输出为空或无法解析，保留原状。"
        # 方向越界 → 丢弃方向（保留原值或留空，交闸门如实告警），不编造。
        if (
            new_conclusion["direction"]
            and new_conclusion["direction"] not in report_quality.ALLOWED_CONCLUSION_DIRECTIONS
        ):
            new_conclusion["direction"] = existing["direction"] or ""
        # 置信度只接受三档，越界留空（不臆造强度）。
        if new_conclusion["confidence"] not in report_quality.ALLOWED_CONFIDENCE:
            new_conclusion["confidence"] = ""
        parsed["conclusion"] = new_conclusion
        return parsed, True, "只补写缺失的一句话结论（正文未改写）。"

    if strategy == report_quality.REPAIR_STRATEGY_SECTION:
        from . import macro_ai  # 局部导入：仅小节补写需要解析 JSON

        if not missing_sections:
            return parsed, False, "未给出要补写的小节名，保留原稿。"
        section_name = missing_sections[0]
        raw = _call(
            profile,
            store,
            section_prompt(
                symbol=symbol,
                question=question,
                numbered=numbered,
                unavailable=unavailable,
                report=str(parsed.get("report", "")),
                section_name=section_name,
            ),
        )
        section_parsed = macro_ai.extract_json_object(raw)
        content = str(section_parsed.get("content", "")).strip() if isinstance(section_parsed, dict) else ""
        if not content:
            return parsed, False, "小节补写输出为空或无法解析，保留原稿。"
        merged_report = report_quality.merge_section(str(parsed.get("report", "")), section_name, content)
        # 合并后引用清单 = 原清单 ∪ 新小节里真实可用的引用（避免自产「清单不一致」告警）。
        merged_refs = report_quality.citation_consistency(
            {
                "report": merged_report,
                "executive_summary": parsed.get("executive_summary", ""),
                "valuation": parsed.get("valuation"),
            },
            allowed=allowed_citations,
        )["text_refs"]
        declared_before = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
        parsed["citations"] = sorted(
            {str(item).strip() for item in declared_before if str(item).strip()}
            | {ref for ref in merged_refs if ref in allowed_citations}
        )
        parsed["report"] = merged_report
        return parsed, True, f"只重新生成缺失小节「{section_name}」。"

    return parsed, False, f"未知修复策略「{strategy}」，保留原稿。"
