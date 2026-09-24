/**
 * B3（frontend-optimization-roadmap-2026-09-12）：研报导出与 Markdown/HTML 构建，自 ResearchWorkbenchPage 原样搬出。
 * 纯前端导出链路（不经 Core）：复制全文 / Markdown / HTML（可打印成 PDF）/ Word / PNG / 批量 ZIP。
 */
import { useState, useEffect } from "react";
import type { StockReport, CompareSynthesis, DirectionReport, CompareEntry, AnalysisTurn, ReportCockpit, SourceCatalogEntry } from "./researchTypes";
import {
  formatPrice,
  scenarioProbabilityDisplay,
  scenarioProbabilityWording,
  valuationEvidenceDisplay,
  priceRefCells,
  referenceBoundLabel,
  retrievalStatusView,
  sourceCatalogUrlText,
  watchpointDateView,
  watchpointReview,
  coreSupportBreakdown,
  CLAIM_STATUS_LABEL,
  JEV_SKIP_LABEL,
  ROBUSTNESS_META,
  poolCodeText,
  coreSupportState,
  deepMetricCell,
} from "./format";
import { REPORT_MODE_LABEL, reportDepthLabel, keyFactHighlights, type ReportHighlight } from "./panels";
import {
  EFFECT_META,
  CONCLUSION_CHANGE_META,
  followupStatusAxes,
  followupClaimGroups,
} from "./followup";

/* —— v22 导出（纯前端，不经 Core）：复制全文 / Markdown / HTML（可打印成 PDF）/ Word —— */

/** Windows 文件名非法字符清理 + 截断。 */
function sanitizeFilename(name: string): string {
  return name.replace(/[\\/:*?"<>|\r\n]+/g, "-").replace(/\s+/g, " ").slice(0, 80).trim() || "研究报告";
}

/**
 * R04（2026-09-18 排版与研报呈现一致性路线图）：单份导出与批量 ZIP **共用**同一文件名构造。
 * 格式 `研报-{代码}[-{标的名}]-{模型}-{日期}`；名称只取自选/持仓的服务端 label，
 * 取不到就只留 6 位代码（J08 口径），不解析模型写的标题、不留空。
 */
export function reportFileBase(symbol: string, label: string, model: string, date: string): string {
  const fileLabel = `${symbol}${label ? `-${label}` : ""}`;
  return `研报-${fileLabel}-${model}-${date}`;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * 轻量 markdown → HTML（与页面 MarkdownishText 同一语法子集：###、- / 1. 列表、
 * | 表格 |（带 |---| 分隔行）、**加粗**、[S#] 引用），已转义实体、零 XSS 面。
 * v31 §八.6：`highlights` 传入时，PNG/PDF/HTML 导出**保留颜色**（白底色板，对比度 ≥4.5:1），
 * 且每个彩色标记都带 title 文字标签；Markdown 文件本身仍只保留加粗与文字标签（不依赖颜色）。
 */
export function markdownToHtml(md: string, highlights: ReportHighlight[] = []): string {
  const out: string[] = [];
  const lines = md.split("\n").map((line) => line.trimEnd());
  let listTag: "ul" | "ol" | null = null;
  const sortedHighlights = [...highlights].sort((a, b) => b.text.length - a.text.length);
  /**
   * 行内组装（v31 §八.6）：先转义，再替换 **加粗** 与 [S#] 引用标签；
   * `highlights` 非空时对剩余纯文本段做关键数字高亮（每段最多 2 处，与页面 renderInline 同口径），
   * PNG/PDF/HTML 导出保留颜色（白底色板）且每个 mark 带 title 文字标签。
   */
  const inline = (text: string): string => {
    const escaped = escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(\[S\d+\])/g, '<span class="cite">$1</span>');
    if (sortedHighlights.length === 0) return escaped;
    let budget = 2;
    return escaped
      .split(/(<b>[^<]*<\/b>|<span class="cite">\[S\d+\]<\/span>)/g)
      .map((part) => {
        // 已成标签的片段不再嵌套高亮（加粗本身已是强调，避免双重编码）。
        if (part.startsWith("<b>") || part.startsWith('<span class="cite">')) return part;
        return part.replace(/[+-]?\d+(?:\.\d+)?%?/g, (token) => {
          if (budget <= 0) return token;
          const hit = sortedHighlights.find((highlight) => highlight.text === token);
          if (!hit) return token;
          budget -= 1;
          const titleAttr = hit.title ? ` title="${escapeHtml(hit.title)}"` : "";
          return `<mark class="rpt-hl rpt-hl-${hit.tone}"${titleAttr}>${token}</mark>`;
        });
      })
      .join("");
  };
  const closeList = (): void => {
    if (listTag) { out.push(`</${listTag}>`); listTag = null; }
  };
  const cells = (line: string): string[] =>
    line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const isSeparator = (line: string): boolean => {
    const text = line.trim();
    return text.includes("|") && text.includes("-") && /^[\s|:-]+$/.test(text);
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] ?? "";
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      closeList();
      out.push(`<h3>${inline(heading[1] ?? "")}</h3>`);
      continue;
    }
    if (line.trim().startsWith("|") && isSeparator(lines[i + 1] ?? "")) {
      closeList();
      const header = cells(line);
      const rows: string[][] = [];
      let j = i + 2;
      while (j < lines.length && (lines[j] ?? "").trim().startsWith("|")) {
        rows.push(cells(lines[j] ?? ""));
        j += 1;
      }
      i = j - 1;
      out.push(`<table><thead><tr>${header.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.*)$/);
    const ordered = line.match(/^\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const tag = bullet ? "ul" : "ol";
      if (listTag !== tag) { closeList(); out.push(`<${tag}>`); listTag = tag; }
      out.push(`<li>${inline((bullet ?? ordered)?.[1] ?? "")}</li>`);
      continue;
    }
    closeList();
    if (line.trim() !== "") out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("\n");
}

/** 浅色可打印 HTML 文档（HTML 导出与 Word 导出共用；Word 直接吃同一 HTML）。 */
export function buildHtmlDocument(title: string, metaLines: string[], bodyHtml: string): string {
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>${escapeHtml(title)}</title>
<style>
  body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; max-width: 860px; margin: 40px auto; padding: 0 24px; color: #1a1a1a; line-height: 1.75; }
  h1 { font-size: 22px; border-bottom: 2px solid #2e7d5b; padding-bottom: 10px; }
  h3 { font-size: 16px; margin-top: 24px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 20px; }
  .meta span { display: inline-block; margin-right: 16px; }
  ul { padding-left: 22px; margin: 6px 0; }
  ol { padding-left: 24px; margin: 6px 0; }
  li { margin: 4px 0; }
  p { margin: 8px 0; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }
  th, td { border: 1px solid #dcdfdd; padding: 6px 10px; text-align: left; vertical-align: top; }
  th { background: #f4f6f5; font-weight: 600; }
  .cite { color: #2e7d5b; font-weight: 600; }
  /* v31 §八.6 白底导出色板（暗色界面另有对应 token）：彩色标记一律加粗 + title 文字标签，
     普通文本对比度 ≥ 4.5:1；灰度打印时加粗仍可识别重点，不依赖颜色。 */
  mark.rpt-hl { background: transparent; font-weight: 700; padding: 0 1px; }
  mark.rpt-hl-blue  { color: #1d4ed8; }
  mark.rpt-hl-amber { color: #8a5a00; }
  mark.rpt-hl-coral { color: #b3261e; }
  .disclaimer { margin-top: 32px; padding: 10px 14px; background: #f4f6f5; border-left: 3px solid #2e7d5b; color: #555; font-size: 13px; }
  @media print { body { margin: 0 auto; } }
</style>
</head>
<body>
<h1>${escapeHtml(title)}</h1>
<div class="meta">${metaLines.map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</div>
${bodyHtml}
<p class="disclaimer">研究参考，不构成投资建议；关键判断请回链原始来源复核。</p>
</body>
</html>`;
}

export function buildStockReportMarkdown(report: StockReport, followUps: AnalysisTurn[] = []): string {
  const lines: string[] = [];
  /** 表格单元格内的 | 会打断表格语法，统一替换为 /。 */
  const cell = (value: string | number | null | undefined): string =>
    String(value ?? "—").replace(/\|/g, "/").replace(/\n+/g, " ").trim() || "—";
  lines.push(`# ${report.title}`, "");
  lines.push(`- 标的：${report.symbol}`);
  lines.push(`- 模型：${report.model}`);
  lines.push(`- 置信度：${report.confidence}`);
  // v31 §2.2：规格行用「标准版 · 2,153 字 · 完整」口径（模式 · 字数 · 三态质量状态）。
  const specParts = [
    // J06：档位深度不足时规格行如实呈现「深研版（未达深度 1839/3500 字）」。
    reportDepthLabel(report),
    typeof report.report_chars === "number" && reportDepthLabel(report) === REPORT_MODE_LABEL[report.report_mode ?? ""]
      ? `${report.report_chars} 字`
      : null,
    report.quality_status_label
      ?? (report.quality_status === "complete" ? "完整" : report.quality_status === "needs_review" ? "待复核" : report.quality_status === "incomplete" ? "不完整" : null),
  ].filter(Boolean);
  if (specParts.length > 0) lines.push(`- 报告规格：${specParts.join(" · ")}`);
  // J10（2026-09-18 桌面端路线图）：A 级来源为 0 必须写在头部。
  // 过去它只出现在 100 多行之后的「证据质量与结论支撑」里（样报 106 行），
  // 而等级口径自己写着「核心结论需 A/B 级及以上」——读者先读完结论才知道没有一手原文。
  const aTierSources = report.evidence_quality?.tiers?.A ?? [];
  if (report.evidence_quality && aTierSources.length === 0) {
    lines.push("- 证据资格提示：**本次无一手公告/财报原文（A 级 0 条）**，财务与估值口径均为二次分发或媒体摘要，结论按此打折看。");
  }
  // v32 §二.1：首屏驾驶舱三行结论词随导出走（离开界面后仍能一眼看到三个时间尺度的分项判断）。
  const cockpit: ReportCockpit | undefined = report.pillar_verdicts ?? report.decision_card?.cockpit;
  if (cockpit) {
    const cockpitParts = [
      cockpit.fundamental ? `基本面 ${cockpit.fundamental}` : null,
      cockpit.valuation ? `价值估值 ${cockpit.valuation}` : null,
      cockpit.technical ? `技术状态 ${cockpit.technical}` : null,
    ].filter(Boolean) as string[];
    if (cockpitParts.length > 0) {
      lines.push(`- 驾驶舱：${cockpitParts.join(" · ")}${(cockpit.horizon ?? []).length > 0 ? `（适用期限：${cockpit.horizon!.join("、")}）` : ""}`);
    }
  }
  // v31 §八.4：草稿与阻断项随导出走——离开界面后同样不得冒充完整报告。
  if (report.is_draft) lines.push(`- 状态：**草稿（生成不完整，不计入正式报告数量）**`);
  for (const blocker of report.quality_blockers ?? []) lines.push(`- 阻断项：${blocker.message}`);
  lines.push(`- 生成时间：${new Date(report.generated_at).toLocaleString()}`);
  // J07（2026-09-18 桌面端路线图）：头部去掉零信息重复行。
  // 过去「引用来源」与「本次可引用来源」两行无条件输出，模型引用满可用来源时两行逐字相同
  // （样报 002468 的 9/10 行即如此）；字数与提示词版本也各写两遍。
  const cited = report.citations ?? [];
  if (cited.length > 0) lines.push(`- 引用来源：${cited.join("、")}`);
  const unreferencedSources = (report.available_citations ?? []).filter((key) => !cited.includes(key));
  if (unreferencedSources.length > 0) lines.push(`- 未引用可得来源：${unreferencedSources.join("、")}`);
  const trace = report.generation_trace;
  const traceParts = trace ? [
    trace.prompt_version ? `提示词 ${trace.prompt_version}` : null,
    trace.stages_completed?.length ? `已完成阶段 ${trace.stages_completed.join("/")}` : null,
    trace.report_mode ? `模式 ${REPORT_MODE_LABEL[trace.report_mode] ?? trace.report_mode}` : null,
  ].filter(Boolean) : [];
  // 提示词版本只在留痕里没有、或与策略版本不一致时单独成行——不一致才是版本错位的真信号。
  if (report.prompt_policy_version && !traceParts.some((part) => String(part).includes(report.prompt_policy_version!))) {
    lines.push(`- 提示词策略版本：${report.prompt_policy_version}`);
  }
  if (traceParts.length > 0) lines.push(`- 生成留痕：${traceParts.join(" · ")}`);
  // v31 §4.2：缺失小节在导出中同样显式占位（红色在 Markdown 里退化为文字标签，不依赖颜色）。
  for (const name of report.missing_sections ?? []) {
    lines.push(`- 缺失小节：**${name} · 未生成**`);
  }
  lines.push("");
  if (report.executive_summary) {
    lines.push("## 执行摘要", report.executive_summary, "");
  }
  lines.push("## 研报正文", report.report.trim(), "");
  // v24 档 A/B/C：估值判断（市盈率一类）——模型未给估值时不写该节，不占位、不编造。
  // v32：正常化盈利/三情景估值/安全边际/四态估值状态随估值节一起导出。
  const valuation = report.valuation;
  const normalizedEarnings = valuation?.normalized_earnings;
  const valuationCasesAll = valuation?.valuation_cases ?? [];
  // J02：三列全 null 的情景行不配占一张表（样报 002468 曾把整表渲染成「—」，看着像分析过但没填）。
  const hasNumberColumn = (item: { earnings: number | null; multiple: number | null; fair_value: number | null }) =>
    item.earnings != null || item.multiple != null || item.fair_value != null;
  const valuationCases = valuationCasesAll.filter((item) => item.name && hasNumberColumn(item));
  const valuationCasesEmptiedOut = valuationCases.length === 0 && valuationCasesAll.some((item) => item.name);
  const marginOfSafety = valuation?.margin_of_safety;
  // Q05（2026-09-19 路线图）：三态口径与估值卡共用 format.valuationEvidenceDisplay——
  // 有 PE/PB 却没做正常化验证的样例，导出不能再写「无数据」。
  const valuationEvidence = valuationEvidenceDisplay(valuation);
  const maskedValuationVerdict = !valuation?.verdict?.trim()
    || valuation.verdict.trim() === "无数据"
    || valuation.verdict.trim() === "无法判断";
  if (valuation && (Object.values(valuation).some((item) => Boolean(item && String(item).trim()))
    || (normalizedEarnings && (typeof normalizedEarnings.value === "number" || normalizedEarnings.basis))
    || valuationCases.length > 0 || typeof marginOfSafety === "number")) {
    lines.push("## 估值判断", "", "| 项目 | 内容 |", "| --- | --- |");
    if (valuation.verdict || maskedValuationVerdict) {
      const verdictText = maskedValuationVerdict ? valuationEvidence.verdictDisplay : valuation.verdict;
      lines.push(`| 结论 | ${cell(verdictText)}${!maskedValuationVerdict && valuation.verdict_note ? `（${cell(valuation.verdict_note)}）` : ""} |`);
    }
    lines.push(
      `| 估值证据三态 | ${cell(valuationEvidence.label)}`
      + `${valuationEvidence.note ? `（${cell(valuationEvidence.note)}）` : ""} |`,
    );
    // J01 的原因行不能因为换了措辞就消失：结论词被归一时，读者仍要知道是怎么被改成这一词的。
    if (maskedValuationVerdict && valuation.verdict_note) {
      lines.push(
        `| 结论词归一说明 | ${cell(valuation.verdict_note)}`
        + `${valuation.verdict_raw ? `（模型原结论词：${cell(valuation.verdict_raw)}）` : ""} |`,
      );
    }
    if (valuation.valuation_status) {
      const statusLabel = valuation.valuation_status === "undervalued" ? "低估（正常化口径）"
        : valuation.valuation_status === "fair" ? "合理（正常化口径）"
        : valuation.valuation_status === "expensive" ? "偏贵（正常化口径）"
        : valuation.valuation_status === "undetermined" ? "无法判断（未完成正常化验证）"
        : valuation.valuation_status;
      lines.push(`| 估值状态 | ${cell(statusLabel)}${valuation.valuation_status_note ? `（${cell(valuation.valuation_status_note)}）` : ""} |`);
    }
    if (normalizedEarnings && (typeof normalizedEarnings.value === "number" || normalizedEarnings.basis)) {
      lines.push(
        `| 正常化盈利 | ${normalizedEarnings.value != null ? `${cell(normalizedEarnings.value)} 亿/年` : "未完成正常化验证"}`
        + `${normalizedEarnings.confidence ? `（置信 ${cell(normalizedEarnings.confidence)}）` : ""}`
        + `${normalizedEarnings.basis ? ` 口径：${cell(normalizedEarnings.basis)}` : ""} |`,
      );
    }
    if (valuation.pe_ttm) lines.push(`| PE(TTM) | ${cell(valuation.pe_ttm)} |`);
    if (valuation.pe_percentile) lines.push(`| 历史分位 | ${cell(valuation.pe_percentile)} |`);
    if (valuation.peer_position) lines.push(`| 同业位置 | ${cell(valuation.peer_position)} |`);
    if (valuation.basis) lines.push(`| 判断依据 | ${cell(valuation.basis)} |`);
    lines.push("");
    if (valuationCases.length > 0) {
      lines.push("### 三情景估值（正常化盈利 × 适用倍数 → 公允价值）", "", "| 情景 | 年化盈利（亿） | 适用倍数 | 隐含公允价值（元） |", "| --- | --- | --- | --- |");
      for (const item of valuationCases) {
        lines.push(`| ${cell(item.name_label || item.name)} | ${cell(item.earnings)} | ${cell(item.multiple)} | ${cell(item.fair_value)} |`);
      }
      lines.push("");
    } else if (valuationCasesEmptiedOut) {
      // 说明句只给读者能懂的口径理由，不把内部任务编号与工程注释写进研报。
      lines.push(
        `- 未做三情景估值：正常化盈利未经验证${normalizedEarnings?.basis ? `（${cell(normalizedEarnings.basis)}）` : ""}。`,
        "",
      );
    }
    if (typeof marginOfSafety === "number" && Number.isFinite(marginOfSafety)) {
      lines.push(`- 安全边际：${(marginOfSafety * 100).toFixed(1)}%（${marginOfSafety >= 0 ? "现价低于基准公允价值" : "现价高于基准公允价值"}）`, "");
    }
    for (const item of valuation.valuation_limitations ?? []) lines.push(`- 估值口径限制：${item}`);
    if ((valuation.valuation_limitations ?? []).length > 0) lines.push("");
  }
  // v24 结构化判断（表格语法：页面渲染、HTML/Word 导出、复制全文三处同一口径）
  const scenarios = report.scenarios ?? [];
  const support = report.levels?.support ?? [];
  const resistance = report.levels?.resistance ?? [];
  const watchpoints = report.watchpoints ?? [];
  if (scenarios.length > 0) {
    // J09（2026-09-18 桌面端路线图）：三档概率同值 = 模型根本没做概率判断（样报 002468 三个「中」）。
    // R04：同值判定收敛到 format.scenarioProbabilityDisplay，与屏显共用同一实现、两处口径一致。
    const { uniform: probabilityUniform, displayOf: probabilityCellOf } = scenarioProbabilityDisplay(scenarios);
    // Q08（2026-09-19 路线图）：列名与屏显同源——未校准的低/中/高只能叫「倾向（未统计校准）」，
    // 写成「概率」会被读者当成上涨概率（页面上「中」二字脱离口径后就是个数字幻觉）。
    const probabilityWording = scenarioProbabilityWording(undefined, scenarios);
    // v27 补「失效条件 / 有效窗口 / 概率 / 预期区间」——可证伪口径必须随导出一起走，
    // 否则离开界面后情景又退化成不可被推翻的方向声明。
    lines.push(
      "## 情景与触发条件",
      "",
      `| 情景 | 触发条件 | ${probabilityWording.column} | 预期区间 | 有效窗口 | 失效条件 | 依据来源 |`,
      "| --- | --- | --- | --- | --- | --- | --- |",
    );
    for (const item of scenarios) {
      const low = formatPrice(item.expected_range?.low ?? null);
      const high = formatPrice(item.expected_range?.high ?? null);
      const expectedRange = low === "—" && high === "—" ? "—" : `${low} ~ ${high}`;
      lines.push(
        `| ${cell(item.name)} | ${cell(item.trigger)} | ${cell(probabilityCellOf(item))}`
        + ` | ${cell(expectedRange)} | ${cell(item.horizon)} | ${cell(item.invalidates)} | ${cell(item.source)} |`,
      );
    }
    lines.push("");
    lines.push(`- 概率口径：${probabilityWording.note}`, "");
    if (probabilityUniform) {
      lines.push("- 概率口径：三档概率同值，按「未给概率」处理——不作排序或建仓依据。", "");
    }
  }
  if (support.length > 0 || resistance.length > 0) {
    lines.push("## 关键价位", "", "| 类型 | 价位 | 依据 |", "| --- | --- | --- |");
    for (const item of support) lines.push(`| 支撑 | ${cell(formatPrice(item.price))} | ${cell(item.basis)} |`);
    for (const item of resistance) lines.push(`| 压力 | ${cell(formatPrice(item.price))} | ${cell(item.basis)} |`);
    lines.push("");
  }
  if (watchpoints.length > 0) {
    // J10（2026-09-18 桌面端路线图）：「验证时点」与「到期日」合并为一列。
    // 到期日本就是从可解析的 verify_by 推出来的，能解析时两列逐字相同（样报 98/99 行），
    // 事件锚定行则一个有值一个是「—」——两列并排只让读者猜哪个才是准的。
    lines.push("## 验证点清单", "", "| 观察信号 | 到期（含来源） | 状态 | 若成立则预期 |", "| --- | --- | --- | --- |");
    for (const item of watchpoints) {
      const due = item.due_on && item.due_on !== item.verify_by ? `${item.verify_by} → ${item.due_on}` : item.verify_by;
      lines.push(
        `| ${cell(item.signal)} | ${cell(due)}`
        + ` | ${item.status === "pending_review" ? "待复盘" : cell(item.status)} | ${cell(item.expected_if_true)} |`,
      );
    }
    lines.push("");
  }
  // v27 证据质量与 claim 覆盖（系统写入，非模型产出）：导出件必须能自证「哪条结论撑得住」。
  const evidenceQuality = report.evidence_quality;
  const claims = report.claims ?? report.claim_findings?.claims ?? [];
  const claimFindings = report.claim_findings;
  if (evidenceQuality || claims.length > 0) {
    lines.push("## 证据质量与结论支撑（系统写入，非模型产出）", "");
    if (evidenceQuality) {
      lines.push(
        `- 等级口径：A=公告/财报/交易所原始数据 · B=公司正式说明/二次分发 · C=媒体/数据商摘要 · D=全市场扫描/传闻；`
        + `核心结论需 ${(evidenceQuality.core_support_qualities ?? ["B"]).join("/")} 级及以上`,
        `- 本次来源分级：${(["A", "B", "C", "D"] as const)
          .map((tier) => `${tier} 级 ${(evidenceQuality.tiers?.[tier] ?? []).join("/") || "无"}`)
          .join("；")}`,
      );
    }
    if (claimFindings) {
      // J03（桌面端升级路线图 2026-09-18）：判定阈值化——「有支撑」= 核心判断全数支撑；
      // 部分支撑带分母（N/M），1/6 与 4/6 文案必然不同；口径与屏显共用 coreSupportState。
      const { state, counts } = coreSupportState(claimFindings);
      const coreLabel =
        state === "supported"
          ? `有支撑（${counts.supported}/${counts.total}）`
          : state === "partial"
            ? `部分支撑（${counts.supported}/${counts.total}）`
            : state === "unsupported"
              ? "无支撑（不得作为可发布的核心结论）"
              : "无法判定（模型未输出 claims）";
      lines.push(`- 核心结论判定：${coreLabel}`);
      // Q09（2026-09-19 路线图）：分母必须能被追溯——「2/3」不写清数的是哪三条判断，
      // 读者无从知道漏掉的那一条是哪个结论（与屏显共用 coreSupportBreakdown 的同一集合）。
      const breakdown = coreSupportBreakdown(claimFindings);
      if (breakdown.coreIds.length > 0) {
        lines.push(
          `  - 计入统计的核心判断：${breakdown.coreIds.join("、")}`
          + `（完整支撑 ${breakdown.supportedIds.join("、") || "无"}`
          + `；未完整支撑 ${breakdown.unsupportedIds.join("、") || "无"}）`,
        );
      }
    }
    // JV04：语义支撑层——导出件离开界面后必须仍能看出「引用的内容到底支撑不支撑该判断」，
    // 以及**哪几条压根没进这一层**（没跑 ≠ 通过，两者在纸面上必须能区分）。
    const jev = report.jev ?? claimFindings?.jev;
    if (jev) {
      if (jev.available) {
        lines.push(
          `- 语义支撑层（Jev ${jev.model || "—"}）：已比对 ${jev.evaluated} 条判断**所引证据的内容**是否支撑该判断`
          + `（确定性层只校验引用真实性/字段覆盖/证据等级，这一层是内容比对）`
          + (jev.downgraded.length > 0 ? `；${jev.downgraded.length} 条据此降级` : "")
          + (jev.invented && jev.invented.length > 0
            ? `；${jev.invented.length} 条含所引证据中找不到的具体事实（疑似编造，须人工复核）`
            : ""),
        );
        if (jev.weakened && jev.weakened.length > 0) {
          lines.push(
            `  - 语义认为强度不足但**未改判定**：${jev.weakened.join("、")}`
            + "（中文样本尚未标定，暂只标注）",
          );
        }
      } else {
        lines.push(
          "- 语义支撑层**未运行**：本次只跑了确定性口径（引用真实性/字段覆盖/证据等级），"
          + "「所引内容是否真的支撑该判断」**未校验**，相关结论按未做语义核验对待。",
        );
      }
      if (jev.skipped.length > 0) {
        lines.push(
          `  - 未进语义比对 ${jev.skipped.length} 条：`
          + jev.skipped
            .map((item) => `${item.claim_id || "（无编号）"}（${JEV_SKIP_LABEL[item.reason] ?? item.reason}）`)
            .join("、")
          + "——「未校验」不等于「通过」",
        );
      }
    }
    if (claims.length > 0) {
      // 语义列只在**该层确实跑过**时出现：旧档案（无 jev）保持原来的 7 列，
      // 不让每一行都挂一句「该层未运行」——那层没跑由上面的汇总行统一说明。
      const jevRan = Boolean(jev?.available);
      lines.push(
        "",
        jevRan
          ? "| 核心判断 | 引用 | 需要字段 | 覆盖缺口 | 最高等级 | 结论 | 语义支撑（Jev） | 入核心支撑统计 |"
          : "| 核心判断 | 引用 | 需要字段 | 覆盖缺口 | 最高等级 | 结论 | 入核心支撑统计 |",
        jevRan ? "| --- | --- | --- | --- | --- | --- | --- | --- |" : "| --- | --- | --- | --- | --- | --- | --- |",
      );
      // Q09：判断表要标出哪些进入核心支撑统计，「2/3」才是可回查的分母而不是装饰。
      // 按位置取（coreFlags）：旧档案没有 claim_id 时按编号匹配会把每条都判成「未列入核心」。
      const coreFlags = coreSupportBreakdown(claimFindings).coreFlags;
      const marksCore = claimFindings && claimFindings.claims.length > 0 ? coreFlags : null;
      const coreCell = (index: number, support: string | undefined): string => {
        if (!marksCore) return "未判定（无覆盖校验）";
        if (!marksCore[index]) return "否（未列入核心）";
        return support === "full" ? "是 · 计入分子（完整支撑）" : "是 · 仅计入分母";
      };
      // 「未比对」= 该层跑了但这条没进（原因在汇总行里逐条写明），与「该层未运行」是两件事。
      const jevCell = (claim: (typeof claims)[number]): string => {
        if (!claim.jev) return "未比对";
        const invented =
          claim.jev.invented === "yes" ? " · 疑似编造" : claim.jev.invented === "uncertain" ? " · 编造存疑" : "";
        return `${claim.jev.support_label}${invented}`;
      };
      for (const claim of claims) {
        lines.push(
          `| ${cell(claim.text)} | ${cell(claim.sources.join("/"))} | ${cell(claim.requires.join("、"))}`
          + ` | ${cell(claim.missing_coverage.join("、"))} | ${cell(claim.evidence_quality)}`
          + ` | ${cell(CLAIM_STATUS_LABEL[claim.status] ?? claim.status)}`
          + (jevRan ? ` | ${cell(jevCell(claim))}` : "")
          + ` | ${cell(coreCell(claims.indexOf(claim), claim.support))} |`,
        );
      }
    }
    for (const item of claimFindings?.warnings ?? []) lines.push(`- ${item}`);
    lines.push("");
  }
  // Q09（2026-09-19 路线图）：来源目录——导出里的 S1…S5 只有编号时，离开界面就查无出处。
  // 缺链接一律如实写「（未提供链接）」，绝不按来源名猜一个看起来合理的 URL。
  pushSourceCatalog(lines, cell, report.source_catalog);
  // v27 反方检查：ok=false 时如实写入「未完成」，不用空白冒充做过。
  const counterCheck = report.counter_check;
  if (counterCheck) {
    lines.push("## 反方检查（单次短调用，只指软肋，不改写正文）", "");
    if (!counterCheck.ok) {
      lines.push(`- 本次未完成：${counterCheck.detail || "未返回可解析结果"}`);
    } else {
      const robustness = ROBUSTNESS_META[counterCheck.verdict_robustness ?? "mixed"] ?? ROBUSTNESS_META.mixed!;
      lines.push(
        `- 结论耐受度：${robustness.text}`,
        `- 最强支持证据：${counterCheck.strongest_support || "—"}`,
        `- 最强反证：${counterCheck.strongest_counter || "—"}`,
      );
      const conditions = counterCheck.necessary_conditions ?? [];
      if (conditions.length > 0) {
        lines.push("- 结论成立的必要条件（缺一即不成立）：");
        for (const item of conditions) lines.push(`  - ${item}`);
      }
      const misread = counterCheck.most_misread_sentence;
      if (misread?.quote) lines.push(`- 最容易被误读的一句：「${misread.quote}」${misread.why ? `（${misread.why}）` : ""}`);
    }
    lines.push("");
  }
  if (report.source_errors && Object.keys(report.source_errors).length > 0) {
    lines.push("## 来源取数失败说明");
    for (const [key, message] of Object.entries(report.source_errors)) lines.push(`- ${key}：${message}`);
    lines.push("");
  }
  const dropped = report.citation_dropped ?? [];
  const warnings = report.quality_warnings ?? [];
  // v39 Q16/Q17/Q18：深研拆解三块写进导出——离开界面也要能复核「拆了什么、缺什么」。
  const earnings = report.earnings_growth ?? [];
  const cashFlow = report.cash_flow_quality ?? null;
  const divergence = report.valuation_divergence ?? null;
  if (earnings.length > 0 || cashFlow || divergence) {
    lines.push("## 深研拆解（盈利增长 · 现金流质量 · 估值分化）", "");
    if (earnings.length > 0) {
      lines.push("**盈利增长拆解**（贡献比例只在可复算时给出）：", "");
      lines.push("| 因素 | 作用 | 贡献 | 依据与来源 |", "| --- | --- | --- | --- |");
      for (const row of earnings) {
        lines.push(
          `| ${cell(row.factor_label || row.factor_raw || "未指明因素")} | ${cell(row.effect)}`
          + ` | ${row.contribution_pct == null ? "未给出" : `${row.contribution_pct}%`}`
          + ` | ${cell(`${row.basis || "—"}（${row.source_note || "无来源"}）`)}${row.note ? ` —— ${cell(row.note)}` : ""} |`,
        );
      }
      lines.push("");
    }
    if (cashFlow) {
      const show = (input?: number | string | null) => (
        input === null || input === undefined || input === "" ? "未取到" : String(input)
      );
      lines.push(
        `**现金流质量**：经营现金流 ${show(cashFlow.operating_cash_flow)}｜归母净利 ${show(cashFlow.net_income)}`
        + `｜覆盖倍数 ${show(cashFlow.coverage_ratio)}｜折旧摊销 ${show(cashFlow.depreciation_amortization)}`
        + `｜营运资本变动 ${show(cashFlow.working_capital ?? cashFlow.working_capital_change)}`
        + `｜资本开支 ${show(cashFlow.capex)}｜自由现金流 ${show(cashFlow.free_cash_flow)}`,
      );
      lines.push(`- 判定：${cashFlow.verdict || "未判定"} —— ${cashFlow.note || ""}`);
      if (cashFlow.downgrade_note) lines.push(`- 降档说明：${cashFlow.downgrade_note}`);
      lines.push("");
    }
    if (divergence) {
      const review = divergence.comparable_review;
      lines.push(
        `**估值分化与可比口径**：PE ${deepMetricCell(divergence.pe_ttm)}`
        + `（${divergence.pe_percentile || "无分位"}）｜PB ${deepMetricCell(divergence.pb_mrq)}`
        + `（${divergence.pb_percentile || "无分位"}）｜ROE ${deepMetricCell(divergence.roe)}`
        + `｜净资产口径 ${divergence.equity_basis || "未说明"}`,
      );
      if (divergence.cycle_note) lines.push(`- 盈利周期位置：${divergence.cycle_note}`);
      if (review) {
        lines.push(
          `- 可比样本：${review.sample_count ?? 0} 只（${review.comparability_label || "未判定"}）—— ${review.note || ""}`,
        );
        lines.push(`- 剔除规则：${review.exclusion_rule || "未声明"}`);
      }
      lines.push(`- 敏感性口径：${divergence.sensitivity_note || "未说明"}`);
      if (divergence.low_pe_is_not_margin_of_safety) lines.push(`- ${divergence.guardrail}`);
      lines.push("");
    }
  }
  // v39 Q19：研究完成度写在字数告警之前——「长而不全」不能因为字数先出现就被读成研究已完整。
  const completeness = report.research_completeness;
  if ((completeness?.items?.length ?? 0) > 0) {
    lines.push("## 深研完成度（系统写入，非模型产出）", "");
    lines.push(
      `- 判定：${completeness!.complete ? "四项齐备" : "关键研究项缺失"}`
      + `（${completeness!.done_count ?? 0}/${completeness!.items!.length}）`,
    );
    for (const item of completeness!.items!) {
      lines.push(
        `- ${cell(item.label)}：${item.state_label || item.state}`
        + `${(item.covered?.length ?? 0) > 0 ? `（已覆盖 ${item.covered!.join("、")}）` : ""}`
        + `${(item.gaps?.length ?? 0) > 0 ? ` 缺：${item.gaps!.join("、")}` : ""}`,
      );
    }
    if (completeness!.headline) lines.push("", `- ${cell(completeness!.headline)}`);
    if (completeness!.word_count_note) lines.push(`- ${cell(completeness!.word_count_note)}`);
    if (completeness!.note) lines.push(`- ${cell(completeness!.note)}`);
    lines.push("");
  }
  // v39 Q20：反方检查触发的摘要加注——模型原话保留、加了什么标注必须可回查。
  const overreach = report.summary_overreach;
  if (overreach && (overreach.changed || overreach.reviewed === false || (overreach.warnings?.length ?? 0) > 0)) {
    lines.push("## 摘要修订留痕（反方检查触发，服务端只加注不改写事实句）", "");
    lines.push(`- ${cell(overreach.revision_note || "本次未给出修订说明")}`);
    for (const item of overreach.warnings ?? []) lines.push(`- ${cell(item)}`);
    if (overreach.reviewed === false) {
      lines.push("- 反方检查未运行：摘要过度断言检查同样未运行（不假装已修订）。");
    }
    for (const item of overreach.revision_trace ?? []) {
      lines.push(`- 标注：${cell(String(item.sentence ?? ""))}${item.denial ? ` ← 反方：${cell(String(item.denial))}` : ""}`);
    }
    if (overreach.summary_original && overreach.revised_summary
      && overreach.summary_original !== overreach.revised_summary) {
      lines.push(`- 模型原话：${cell(overreach.summary_original)}`);
    }
    lines.push("");
  }
  if (dropped.length > 0 || warnings.length > 0) {
    lines.push("## 质量校验痕迹（系统写入，非模型产出）");
    if (dropped.length > 0) lines.push(`- 已剔除虚假引用（这些来源本次取数失败）：${dropped.join("、")}`);
    for (const item of warnings) lines.push(`- ${item}`);
    lines.push("");
  }
  if (report.limitations.length > 0) {
    lines.push("## 局限与口径声明");
    for (const item of report.limitations) lines.push(`- ${item}`);
    lines.push("");
  }
  if (report.revision_notes) {
    lines.push("## 修订说明（协同流水线）", report.revision_notes, "");
  }
  // v31 §2.4：定向修复的修订历史随导出走（原因/策略/结果），离开界面后仍可解释报告为何变化。
  const history = report.revision_history ?? [];
  if (history.length > 0) {
    lines.push("## 修订历史（定向修复，每次修复升一版）", "");
    for (const entry of history) {
      lines.push(
        `- 修订 v${entry.revision}（${entry.strategy_label ?? entry.strategy}）：${entry.reason}`
        + ` → ${entry.result_label ?? entry.result ?? "—"}`,
      );
      if (entry.changes) lines.push(`  - 变更：${entry.changes}`);
    }
    lines.push("");
  }
  // v32 §三：追问分析附录随导出走（append-only，原报告不可变；补充材料保留「未独立验证」标签，
  // 颜色退化为文字标签，满足「导出后关键信息仍可识别」的验收口径）。
  // v39 Q02：具体顺序与措辞全部由 pushFollowUpAppendix 承担，与屏显共用同一批判定函数。
  pushFollowUps(lines, followUps);
  lines.push("> 研究参考，不构成投资建议；关键判断请回链原始来源复核。");
  return lines.join("\n");
}

/**
 * Q09：来源目录节（研报与追问附录共用一份实现）。
 * 空目录不占版面——旧记录没有 source_catalog 时不写空表冒充做过来源梳理。
 */
function pushSourceCatalog(
  lines: string[],
  cell: (value: string | number | null | undefined) => string,
  rows: SourceCatalogEntry[] | undefined,
  heading = "## 来源目录（可回查：编号 / 名称 / 链接 / 数据日期 / 获取时间 / 口径限制）",
): void {
  if ((rows?.length ?? 0) === 0) return;
  lines.push(heading, "", "| 编号 | 名称 | 可用链接 | 数据日期 | 获取时间 | 等级 | 引用状态 | 被哪些判断使用 | 口径限制 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |");
  for (const row of rows!) {
    lines.push(
      `| ${cell(row.source_id)}`
      + ` | ${cell(row.name || "（未提供名称）")}`
      + ` | ${cell(sourceCatalogUrlText(row))}`
      + ` | ${cell(row.data_date || "未标注")}`
      + ` | ${cell(row.retrieved_at || "未标注")}`
      + ` | ${cell(row.quality || "—")}`
      + ` | ${row.cited === false ? "可得未引用" : "已引用"}`
      + ` | ${cell((row.used_by ?? []).join("、") || "—")}`
      + ` | ${cell(row.scope_note || "—")} |`,
    );
  }
  lines.push("- 链接口径：仅列出来源记录里真实存在的链接；标「（未提供链接）」表示本次没有可访问地址，不代表来源不存在。", "");
}

/**
 * Q02/Q04/Q06/Q07/Q08/Q11：单条追问附录的导出（与屏显 AnalysisTurnCard 同一顺序、同一措辞）。
 * 状态轴与判断分组都取自 followup.tsx 的共享函数（followupStatusAxes / followupClaimGroups），
 * 而不是在这里再写一遍三元表达式——上一版屏显与导出各写一套，改一处就漏一处。
 */
function pushFollowUpAppendix(lines: string[], turn: AnalysisTurn): void {
  const cell = (value: string | number | null | undefined): string =>
    String(value ?? "—").replace(/\|/g, "/").replace(/\n+/g, " ").trim() || "—";
  const axes = followupStatusAxes(turn);
  const change = CONCLUSION_CHANGE_META[turn.conclusion_change] ?? CONCLUSION_CHANGE_META.undetermined!;
  const groups = followupClaimGroups(turn);
  const retrieval = retrievalStatusView(turn.retrieval_status);
  const valuation = turn.valuation_view ? valuationEvidenceDisplay(turn.valuation_view) : null;
  const probabilityWording = scenarioProbabilityWording(turn.probability_view ?? null);

  lines.push(
    `### 附录 #${turn.turn_index} · ${change.label}`,
    "",
    `- 时间：${new Date(turn.created_at).toLocaleString()}`,
    `- 入口：${turn.mode_label || turn.mode || "未标注（旧版记录未区分解读/补充研究）"}`,
    `- 数据来源：${axes.dataBasis.label}`,
    "",
    `**问：** ${turn.question}`,
    "",
    `> ${axes.stateLine}`,
    "",
  );
  if (turn.direct_answer?.trim()) {
    lines.push(`**直接回答：** ${turn.direct_answer.trim()}`, "");
  }
  lines.push(
    `- 状态轴：修订「${axes.revision.label}」 · 可回答「${axes.answerability.label}」 · 数据「${axes.dataBasis.label}」`,
    "",
  );
  if (axes.conflict) lines.push(`> ⚠ 状态轴冲突：${axes.conflict}`, "");
  if (turn.data_as_of !== undefined) lines.push(`- 数据截至：${turn.data_as_of || "未标注"} · ${retrieval.text}`, "");
  if ((turn.key_conditions?.length ?? 0) > 0) {
    lines.push("**关键条件（直接回答成立的前提）**：");
    for (const item of turn.key_conditions!) lines.push(`- ${item}`);
    lines.push("");
  }
  if ((turn.evidence_gaps?.length ?? 0) > 0) {
    lines.push("**补证缺口（还缺什么证据）**：");
    for (const item of turn.evidence_gaps!) lines.push(`- ${item}`);
    lines.push("");
  }
  lines.push(turn.answer.trim(), "");

  // Q04：无新增证据的判断不再逐条刷屏（样报 5 行「无法判断」压住了回答本体），合并成一句共同限制。
  if (groups.expanded.length > 0) {
    lines.push("**受影响判断（只展开有新增证据路径的）**：", "", "| 编号 | 判断名称 | 影响 | 理由 |", "| --- | --- | --- | --- |");
    for (const claim of groups.expanded) {
      const meta = EFFECT_META[claim.effect] ?? EFFECT_META.cannot_judge!;
      lines.push(`| ${cell(claim.claim_id)} | ${cell(claim.claim_name || "—")} | ${meta.label} | ${cell(claim.reason)} |`);
    }
    lines.push("");
  } else if (groups.unresolved.length > 0) {
    lines.push("**受影响判断**：本轮没有任何判断获得可展开的支持或削弱证据。", "");
  }
  if (groups.unresolved.length > 0) {
    lines.push(
      `**未展开的判断（${groups.unresolved.map((item) => item.claim_id || "—").join("、")}）**：${groups.shared}`,
      "",
    );
  }
  if ((turn.dropped_claim_ids ?? []).length > 0) {
    lines.push(`- 已剔除模型自造的判断编号：${turn.dropped_claim_ids!.join("、")}（原报告不存在，不采信）`, "");
  }
  if ((turn.scenario_changes ?? []).length > 0) {
    lines.push("**情景变化**：");
    for (const item of turn.scenario_changes!) lines.push(`- ${item.name || "情景"}：${item.change}`);
    lines.push("");
  }
  if ((turn.new_watchpoints ?? []).length > 0) {
    // Q07：日期依据与假设拆分随导出走——凭空出现的 11-30 这类日期，导出里也要看得见它被降级了。
    lines.push(
      "**新增验证点**：",
      "",
      "| 观察信号 | 验证时点 | 日期类型 | 日期依据 | 若成立则预期 | 假设拆分 |",
      "| --- | --- | --- | --- | --- | --- |",
    );
    for (const item of turn.new_watchpoints!) {
      const date = watchpointDateView(item);
      lines.push(
        `| ${cell(item.signal)} | ${cell(date.text)} | ${cell(date.kind)}`
        + ` | ${cell(date.basis)}${date.demotedFrom ? `（原值 ${cell(date.demotedFrom)} 已降级）` : ""}`
        + ` | ${cell(item.expected_if_true)} | ${cell(date.assumptions.join("；") || "—")} |`,
      );
    }
    for (const item of turn.new_watchpoints!) {
      const date = watchpointDateView(item);
      if (date.note) lines.push(`- 日期口径（${item.signal || "验证点"}）：${date.note}`);
    }
    // Q07：重复验证点与「同时点相反预期」在导出里同样点名（屏显同一份判定，不多一套口径）。
    for (const item of turn.new_watchpoints!) {
      const review = watchpointReview(item);
      if (review.duplicateOf > 0) {
        lines.push(`- 重复验证点：${cell(item.signal)}与第 ${review.duplicateOf} 条为同一信号，合并看待即可`);
      }
      if (review.note) lines.push(`- 复核提示（${item.signal || "验证点"}）：${review.note}`);
    }
    lines.push("");
  }
  if ((turn.price_refs ?? []).length > 0) {
    // Q06：每个价位带截至日与适用窗口——历史快照不能被读成未来时点的实时阈值。
    lines.push("**本次引用的价位与时点**：", "", "| 价位 | 是什么位 | 数据截至 | 适用窗口 | 口径 | 说明 |", "| --- | --- | --- | --- | --- | --- |");
    for (const ref of turn.price_refs!) {
      const cells = priceRefCells(ref);
      lines.push(
        `| ${cells.level} | ${cell(cells.role)} | ${cell(cells.asOf)} | ${cell(cells.window)}`
        + ` | ${cell(cells.status)} | ${cell(cells.note || "—")} |`,
      );
    }
    lines.push("");
  }
  if (valuation) {
    lines.push(
      `**本次估值口径（三态）**：${valuation.label} · ${valuation.verdictDisplay}`,
      "",
    );
    if (valuation.note) lines.push(`- ${valuation.note}${valuation.derived ? "（该记录未带三态字段，措辞按同一规则补齐，未新增判断）" : ""}`, "");
  }
  if ((turn.probability_view?.scenarios?.length ?? 0) > 0) {
    // Q08：列名用「倾向（未统计校准）」；只凭历史高点身份进入区间的价位标成参考位，不当目标价。
    lines.push(
      `**情景倾向（列名口径：${probabilityWording.column}）**：`,
      "",
      "| 情景 | " + probabilityWording.column + " | 区间下界 | 区间上界 |",
      "| --- | --- | --- | --- |",
    );
    for (const item of turn.probability_view!.scenarios!) {
      const boundCell = (side: "low" | "high") => {
        const bound = (item.bounds ?? []).find((row) => row.side === side);
        if (!bound || bound.value == null) return "—";
        const labeled = referenceBoundLabel(bound);
        return `${labeled.isReference ? `${labeled.label} ` : ""}${cell(bound.value)}`;
      };
      lines.push(
        `| ${cell(item.name)} | ${cell(item.probability_display || item.probability_level || "未给出")}`
        + ` | ${boundCell("low")} | ${boundCell("high")} |`,
      );
    }
    lines.push("", `- 口径：${turn.probability_view!.note || probabilityWording.note}`);
    for (const item of turn.probability_view!.reference_bounds ?? []) {
      lines.push(`- 参考位提示：${cell(item.level)}（${item.scenario}）${item.note ? ` —— ${item.note}` : ""}`);
    }
    lines.push("");
  }
  for (const item of turn.supplement_evidence ?? []) {
    lines.push(`- 补充材料【${item.trust_label}】：来源 ${item.source_name || "（未填）"}${item.event_date ? ` · 事件日期 ${item.event_date}` : ""}`);
    lines.push(`  > ${item.text.replace(/\n+/g, " ").slice(0, 300)}`);
  }
  if ((turn.evidence_snapshot ?? []).length > 0) {
    lines.push("", "**本次新增数据（补充研究取数结果）**：");
    for (const item of turn.evidence_snapshot!) {
      lines.push(
        `- [${item.status === "ok" ? "成功" : "失败"}] ${item.label || item.kind}`
        + `${item.source_name ? ` · 来源 ${item.source_name}` : ""}`
        + `${item.event_date ? ` · 数据日期 ${item.event_date}` : ""}`
        + `${item.retrieved_at ? ` · 获取 ${item.retrieved_at}` : ""}`
        + ` · ${sourceCatalogUrlText({ url: item.url, url_note: item.url_note })}`
        + `${(item.supported_claims ?? []).length > 0 ? ` · 支撑 ${item.supported_claims!.join("、")}` : ""}`
        + `${item.trust_label ? ` · ${item.trust_label}` : ""}`,
      );
      if (item.error) lines.push(`  - 失败原因：${item.error}（未用推测填补）`);
    }
  }
  if (retrieval.failedItems.length > 0) {
    lines.push("", `- 补证失败项：${retrieval.failedItems.map((item) => `${item.label || item.kind}${item.error ? `（${item.error}）` : ""}`).join("；")} —— ${retrieval.text}`);
  }
  if ((turn.limitations ?? []).length > 0) {
    lines.push("");
    for (const item of turn.limitations!) lines.push(`- 口径限制：${item}`);
  }
  lines.push("");
  // Q13：催化证据链——缺的那环在导出里也要看得见，正文的断言不能盖过链条断裂的位置。
  const chain = turn.catalyst_chain;
  if ((chain?.steps?.length ?? 0) > 0) {
    lines.push("#### 催化证据链（业务量 → 价格与成本 → 利润 → 市场预期 → 价格反应）", "");
    for (const step of chain!.steps ?? []) {
      lines.push(
        `- ${cell(step.label || step.key)}：${step.status === "evidenced" ? "有证据" : "缺证据"}`
        + `${step.evidence ? `（${cell(step.evidence)}）` : ""}`
        + `${step.note ? ` —— ${cell(step.note)}` : ""}`,
      );
    }
    if (chain!.note) lines.push("", `- 链条口径：${cell(chain!.note)}`);
    if (chain!.guardrail) lines.push(`- 护栏：${cell(chain!.guardrail)}`);
    lines.push("");
  }
  if ((turn.answer_flags?.length ?? 0) > 0) {
    lines.push(`- 服务端标注（不改写正文）：${turn.answer_flags!.map((item) => cell(item)).join(" ")}`, "");
  }
  pushSourceCatalog(lines, cell, turn.source_catalog, "#### 本附录来源目录");
}

/**
 * Q02/Q04/Q03（2026-09-19 路线图）：追问分析附录导出。
 * 顺序与屏显一致（回答 → 状态 → 条件/缺口 → 正文 → 判断 → 验证点 → 价位 → 材料 → 局限），
 * 模型与提示词版本仍留痕，只是排到每条附录的最后（审计用，不是阅读入口）。
 */
function pushFollowUps(lines: string[], followUps: AnalysisTurn[]): void {
  if (followUps.length === 0) return;
  lines.push("## 追问分析附录（不可变追加记录）", "");
  for (const turn of followUps) {
    pushFollowUpAppendix(lines, turn);
    lines.push(
      `> 审计留痕：模型 ${turn.model || "—"}${turn.prompt_version ? ` · 提示词 ${turn.prompt_version}` : " · 提示词版本未记录"}`
      + ` · 锚定原报告 v${turn.base_report_version}`,
      "",
    );
  }
}

/** v23 跨报告综合结果 → Markdown（导出与复制共用）。 */
export function buildCompareSynthesisMarkdown(synthesis: CompareSynthesis): string {
  const lines: string[] = [];
  lines.push("# 跨报告综合 · 共识与分歧", "");
  lines.push(`- 综合模型：${synthesis.model}`);
  lines.push(`- 生成时间：${new Date(synthesis.generated_at).toLocaleString()}`);
  lines.push("", "## 综合结论", synthesis.summary, "");
  if (synthesis.consensus.length > 0) {
    lines.push("## 共识点");
    for (const item of synthesis.consensus) lines.push(`- ${item}`);
    lines.push("");
  }
  if (synthesis.divergences.length > 0) {
    lines.push("## 分歧点（如实陈列，未投票未调和）");
    for (const item of synthesis.divergences) lines.push(`- ${item}`);
    lines.push("");
  }
  if (synthesis.to_verify.length > 0) {
    lines.push("## 待人工核验");
    for (const item of synthesis.to_verify) lines.push(`- ${item}`);
    lines.push("");
  }
  lines.push("> 研究参考，不构成投资建议；分歧请回链各份原报告复核后自行裁决。");
  return lines.join("\n");
}

export function buildDirectionReportMarkdown(report: DirectionReport): string {
  const lines: string[] = [];
  /** 表格单元格内的 | 会打断表格语法，统一替换为 /。 */
  const cell = (value: string | number | null | undefined): string =>
    String(value ?? "—").replace(/\|/g, "/").replace(/\n+/g, " ").trim() || "—";
  lines.push(`# ${report.title || `方向研判 · ${report.topic}`}`, "");
  lines.push(`- 主题：${report.topic}`);
  lines.push(`- 模型：${report.model}`);
  // v33 C04：调用身份随导出走（方案 / 提示词版本）。
  const trace = report.generation_trace;
  if (trace) {
    const parts = [
      trace.profile_name ? `方案 ${trace.profile_name}` : null,
      trace.prompt_version ? `提示词 ${trace.prompt_version}` : null,
      trace.mode ? `模式 ${trace.mode}` : null,
    ].filter(Boolean);
    if (parts.length > 0) lines.push(`- 调用身份：${parts.join(" · ")}`);
  }
  // v33 B01：两种诚实输出状态 + 质量三态随导出留痕。
  if (report.research_mode) {
    lines.push(`- 输出状态：${report.research_mode === "evidence" ? "证据研判（基于本次证据快照）" : "知识概览（基于模型既有知识，非实时）"}`);
  }
  if (report.quality_status_label) lines.push(`- 质量状态：${report.quality_status_label}`);
  if (report.is_draft) lines.push(`- 状态：**草稿（生成不完整，不计入正式报告数量）**`);
  lines.push(`- 生成时间：${new Date(report.generated_at).toLocaleString()}`);
  lines.push("");
  if (report.executive_summary) lines.push("## 执行摘要", report.executive_summary, "");
  lines.push("## 研判正文", (report.report || report.direction_summary || "").trim(), "");
  // v33 B05：核心判断分层
  if ((report.core_judgments?.length ?? 0) > 0) {
    lines.push("## 核心判断（分层）", "", "| 编号 | 层级 | 判断 | 置信 | 依据 |", "| --- | --- | --- | --- | --- |");
    for (const item of report.core_judgments!) {
      lines.push(`| ${cell(item.id)} | ${cell(item.kind_label || item.kind)} | ${cell(item.text)} | ${cell(item.confidence)} | ${cell((item.support_refs ?? []).join("/"))} |`);
    }
    lines.push("");
  }
  // v33 A17/B12：候选表（身份核验 + 入选解释；无价格字段）
  if (report.stock_pool.length > 0) {
    lines.push("## 候选公司（可验证的研究对象）", "", "| 名称/代码 | 产业链环节 | 业务关联 | 业绩兑现路径 | 身份核验 | 主要反证 |", "| --- | --- | --- | --- | --- | --- |");
    for (const item of report.stock_pool) {
      lines.push(
        `| ${cell(item.name || item.symbol)}（${cell(poolCodeText(item))}） | ${cell(item.sector || item.value_chain_position)}`
        + ` | ${cell(item.business_link || item.reason)} | ${cell(item.profit_path)}`
        + ` | ${cell(item.identity_status_label || item.identity_status)} | ${cell(item.counter_evidence)} |`,
      );
    }
    lines.push("");
  } else {
    lines.push("## 候选公司", "", "本次没有给出候选公司（宁缺毋假）。", "");
  }
  // v33 B06：反方审查
  const check = report.counter_check;
  if (check) {
    lines.push("## 反方审查", "");
    if (!check.ok) lines.push(`- 本次未完成：${check.detail || "未返回可解析结果"}`);
    else {
      lines.push(`- 最强反方论点：${check.strongest_counter || "—"}`);
      for (const item of check.affected_conclusions ?? []) {
        const label = item.effect === "weakens" ? "削弱" : item.effect === "supports" ? "支持" : "无法判断";
        lines.push(`- ${item.id}（${label}）：${item.reason}`);
      }
      for (const item of check.pool_challenges ?? []) lines.push(`- 候选 ${item.symbol} 疑点：${item.challenge}`);
    }
    lines.push("");
  }
  // v33 B04：证据快照
  if ((report.evidence_snapshot?.length ?? 0) > 0) {
    lines.push("## 证据快照（[E#] 可回链）", "");
    for (const item of report.evidence_snapshot!) {
      lines.push(`- [${item.evidence_id}] ${item.title}（${item.source}）· ${item.content} · 抓取 ${item.retrieved_at.slice(0, 19).replace("T", " ")}`);
    }
    lines.push("");
  }
  if (report.evidence_unavailable) lines.push(`- 来源不可用：${report.evidence_unavailable}`);
  if ((report.data_gaps?.length ?? 0) > 0) {
    lines.push("## 数据缺口", "");
    for (const item of report.data_gaps!) lines.push(`- ${item}`);
    lines.push("");
  }
  if (report.next_verification) lines.push("## 下一验证动作", report.next_verification, "");
  if (report.catalysts.length > 0) {
    lines.push("## 关键催化");
    for (const item of report.catalysts) lines.push(`- ${item}`);
    lines.push("");
  }
  if (report.risks.length > 0) {
    lines.push("## 主要风险");
    for (const item of report.risks) lines.push(`- ${item}`);
    lines.push("");
  }
  if ((report.quality_warnings?.length ?? 0) > 0) {
    lines.push("## 质量告警（系统写入，非模型产出）");
    for (const item of report.quality_warnings!) lines.push(`- ${item}`);
    lines.push("");
  }
  lines.push("> 研究参考，不构成投资建议；候选代码请自行核验后再送战法雷达。");
  return lines.join("\n");
}

/** 下载文本文件（带 UTF-8 BOM，保证 Windows 记事本 / Word 中文不乱码）。 */
function downloadText(filename: string, content: string, mime: string): void {
  const blob = new Blob(["\ufeff", content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function fallbackCopy(text: string): boolean {
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/** PDF 导出：隐藏 iframe 加载打印样式文档后调起打印面板（Chromium 内可直接「另存为 PDF」），零依赖。 */
function printHtmlDocument(htmlDocument: string): void {
  document.getElementById("wb-print-frame")?.remove();
  const frame = document.createElement("iframe");
  frame.id = "wb-print-frame";
  frame.style.position = "fixed";
  frame.style.right = "0";
  frame.style.bottom = "0";
  frame.style.width = "0";
  frame.style.height = "0";
  frame.style.border = "0";
  document.body.appendChild(frame);
  frame.srcdoc = htmlDocument;
  frame.onload = () => {
    try {
      frame.contentWindow?.focus();
      frame.contentWindow?.print();
    } catch { /* 打印面板被拦截时静默，用户可用 HTML 导出兜底 */ }
  };
}

/** PNG 图片导出：html2canvas 把浅色打印版文档渲染为 2x canvas 再下载。 */
async function exportReportPng(htmlDocument: string, filenameBase: string): Promise<boolean> {
  try {
    const { default: html2canvas } = await import("html2canvas");
    const styleMatch = htmlDocument.match(/<style>([\s\S]*?)<\/style>/);
    const bodyMatch = htmlDocument.match(/<body>([\s\S]*)<\/body>/);
    const container = document.createElement("div");
    container.style.position = "fixed";
    container.style.left = "-9999px";
    container.style.top = "0";
    container.style.width = "860px";
    container.style.background = "#ffffff";
    container.style.padding = "32px 36px";
    container.style.fontFamily = '"Microsoft YaHei", "PingFang SC", sans-serif';
    container.style.lineHeight = "1.75";
    container.style.color = "#1a1a1a";
    if (styleMatch) {
      const styleEl = document.createElement("style");
      styleEl.textContent = styleMatch[1] ?? "";
      container.appendChild(styleEl);
    }
    const bodyWrap = document.createElement("div");
    if (bodyMatch) bodyWrap.innerHTML = bodyMatch[1] ?? ""; // 内容全部经 markdownToHtml/escapeHtml 转义，无注入面
    else bodyWrap.textContent = htmlDocument;
    container.appendChild(bodyWrap);
    document.body.appendChild(container);
    try {
      const canvas = await html2canvas(container, { backgroundColor: "#ffffff", scale: 2, useCORS: true });
      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob((result) => resolve(result), "image/png"));
      if (!blob) return false;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${sanitizeFilename(filenameBase)}.png`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
      return true;
    } finally {
      container.remove();
    }
  } catch {
    return false;
  }
}

/** 结果区导出工具条：多种方式共用同一份 Markdown / HTML 构建产物。 */
export function ExportBar({ filenameBase, markdown, htmlDocument }: { filenameBase: string; markdown: string; htmlDocument: string }) {
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    if (!note) return;
    const timer = window.setTimeout(() => setNote(null), 2600);
    return () => window.clearTimeout(timer);
  }, [note]);

  async function copyAll(): Promise<void> {
    try {
      await navigator.clipboard.writeText(markdown);
      setNote("已复制全文到剪贴板");
    } catch {
      setNote(fallbackCopy(markdown) ? "已复制全文到剪贴板" : "复制失败，请手动选择文本");
    }
  }

  return (
    <div className="wb-export" role="group" aria-label="导出报告">
      {/* v33 A10：导出收纳为统一菜单——所有格式都基于完整构建产物（含全部章节与追问附录），与当前激活页签无关。 */}
      <details className="wb-export-menu">
        <summary>导出（复制 / Markdown / HTML / Word / PDF / 图片）</summary>
        <div className="wb-export-menu-body">
          <button className="ghost-btn" onClick={() => void copyAll()}>复制全文</button>
          <button
            className="ghost-btn"
            onClick={() => { downloadText(`${sanitizeFilename(filenameBase)}.md`, markdown, "text/markdown"); setNote("已下载 Markdown 文件"); }}
          >Markdown</button>
          <button
            className="ghost-btn"
            onClick={() => { downloadText(`${sanitizeFilename(filenameBase)}.html`, htmlDocument, "text/html"); setNote("已下载 HTML（浏览器打开后可打印成 PDF）"); }}
          >HTML</button>
          <button
            className="ghost-btn"
            onClick={() => { downloadText(`${sanitizeFilename(filenameBase)}.doc`, htmlDocument, "application/msword"); setNote("已下载 Word 文档"); }}
          >Word</button>
          <button
            className="ghost-btn"
            onClick={() => { printHtmlDocument(htmlDocument); setNote("已打开打印面板：目标选「另存为 PDF」即可导出 PDF"); }}
          >PDF</button>
          <button
            className="ghost-btn"
            onClick={() => { setNote("正在生成图片…"); void exportReportPng(htmlDocument, filenameBase).then((ok) => setNote(ok ? "已下载 PNG 图片" : "图片生成失败，请改用 HTML 导出")); }}
          >图片</button>
          {note && <span className="wb-export-note">{note}</span>}
        </div>
      </details>
    </div>
  );
}

/** v22 批量导出：整轮批量生成的全部成功报告打包为 ZIP（每份 md+html，附 index.md 汇总索引与失败清单）。 */
export function BatchExportBar({ entries, disabled }: { entries: CompareEntry[]; disabled: boolean }) {
  const [note, setNote] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  useEffect(() => {
    if (!note) return;
    const timer = window.setTimeout(() => setNote(null), 3200);
    return () => window.clearTimeout(timer);
  }, [note]);

  async function exportZip(): Promise<void> {
    setWorking(true);
    setNote("正在打包…");
    try {
      const { default: JSZip } = await import("jszip");
      const zip = new JSZip();
      const stamp = new Date().toISOString().slice(0, 10);
      let okCount = 0;
      const failed: string[] = [];
      const indexLines: string[] = [`# 批量研报索引（${stamp}）`, ""];
      entries.forEach((entry) => {
        if (entry.pending) return;
        if (!entry.report) {
          failed.push(`${entry.symbol} ${entry.name}：${entry.error ?? "生成失败"}`);
          return;
        }
        okCount += 1;
        const report = entry.report;
        const seq = String(okCount).padStart(2, "0");
        const base = sanitizeFilename(reportFileBase(report.symbol, entry.name, report.model, report.generated_at.slice(0, 10)));
        const md = buildStockReportMarkdown(report);
        zip.file(`${seq}-${base}.md`, md);
        zip.file(
          `${seq}-${base}.html`,
          buildHtmlDocument(
            report.title,
            [report.symbol, `模型 ${report.model}`, `置信 ${report.confidence}`, new Date(report.generated_at).toLocaleString()],
            markdownToHtml(md.replace(/^# .*$/m, "").trim(), keyFactHighlights(report)),
          ),
        );
        indexLines.push(`- [${seq}] ${report.symbol} ${entry.name} · ${report.model} · 置信 ${report.confidence}（${seq}-${base}.md / .html）`);
      });
      if (failed.length > 0) {
        zip.file("失败清单.txt", `以下标的本轮生成失败：\n${failed.join("\n")}\n`);
        indexLines.push("", "## 未成功", ...failed.map((f) => `- ${f}`));
      }
      indexLines.push("", "> 研究参考，不构成投资建议；关键判断请回链原始来源复核。");
      zip.file("index.md", indexLines.join("\n"));
      const blob = await zip.generateAsync({ type: "blob" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `批量研报-${okCount}份-${stamp}.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setNote(`已打包 ${okCount} 份成功报告${failed.length > 0 ? `，${failed.length} 份失败已列入失败清单` : ""}`);
    } catch {
      setNote("打包失败，请改用单份导出");
    } finally {
      setWorking(false);
    }
  }

  const exportable = entries.filter((entry) => entry.report).length;
  return (
    <div className="wb-export" role="group" aria-label="批量导出">
      <button
        className="ghost-btn"
        disabled={disabled || working || exportable === 0}
        onClick={() => void exportZip()}
      >批量导出 ZIP（{exportable} 份）</button>
      {note && <span className="wb-export-note">{note}</span>}
    </div>
  );
}

/** v23 协同流水线各阶段结果的结构（宽松读取，缺字段如实降级显示）。 */
interface CollabCheckResult { observations?: string[]; gaps?: string[]; readiness?: string }

