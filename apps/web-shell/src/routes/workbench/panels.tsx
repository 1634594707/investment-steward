/**
 * B3（frontend-optimization-roadmap-2026-09-12）：研报分析面板簇，自 ResearchWorkbenchPage 原样搬出。
 * 质量面板 / 证据面板 / 反方检查 / 决策卡 / 结论判断 / 图表 / Markdown 渲染。
 */
import { Fragment, memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, createChart } from "lightweight-charts";
import type { StockReport, QuarterlyPoint, PriceChartPack, ValuationChartPack, ReviewQueue, DirectionReport, ReportScenario, ReportLevel, ReportWatchpoint, ReportValuation, ClaimFinding, ReportConclusion, CategorizedWarning, ReportDecisionCard, QualityBlocker, SectionState, GenerationTrace, ReportCockpit, SourceCatalogEntry } from "./researchTypes";
import { formatPrice, formatProbability, scenarioProbabilityDisplay, scenarioProbabilityWording, valuationEvidenceDisplay, deepMetricCell, sourceCatalogUrlText, coreSupportBreakdown, CLAIM_STATUS_LABEL, JEV_SKIP_LABEL, ROBUSTNESS_META, coreSupportState, watchpointDateView, watchpointTriageView, watchpointPriorityView } from "./format";

/* —— v31 质量恢复（2026-09-13 方案 §2.2/§4.1/§4.2）：三态质量状态 + 首屏状态行 + 正文目录锚点 —— */

/** 研报模式 → 中文标签（与后端 REPORT_MODES 同源；未知模式原样展示，不猜测）。 */
export const REPORT_MODE_LABEL: Record<string, string> = {
  quick: "快速版",
  standard: "标准版",
  deep: "深研版",
};

/** J06（桌面端升级路线图 2026-09-18）：档位深度标签——正文低于该模式下限时如实呈现为
 * 「深研版（未达深度 1839/3500 字）」，不再只挂一个泛化的「待复核」让用户自猜；达标时原样返回。 */
export function reportDepthLabel(report: {
  report_mode?: string;
  report_chars?: number;
  report_mode_budget?: { min?: number };
}): string | null {
  const label = REPORT_MODE_LABEL[report.report_mode ?? ""];
  if (!label) return null;
  const min = report.report_mode_budget?.min;
  if (typeof report.report_chars === "number" && typeof min === "number" && report.report_chars < min) {
    return `${label}（未达深度 ${report.report_chars}/${min} 字）`;
  }
  return label;
}

/** 三态质量状态 → 展示色调（完整=mint / 待复核=琥珀 / 不完整=珊瑚红；每种颜色都伴随文字标签）。 */
export const QUALITY_STATUS_TONE: Record<string, string> = {
  complete: "mint",
  needs_review: "warn",
  incomplete: "coral",
};

/* —— v32 价值投资升级（2026-09-13 方案 §二）：首屏驾驶舱三行结论词 + 正常化估值状态 —— */

/** 驾驶舱三行结论词 → 展示元数据（颜色只表达证据/状态：正向=青绿、风险=珊瑚红、待确认=琥珀、不可判=灰蓝，不代表买卖方向）。 */
export const COCKPIT_ROW_META: Record<string, { label: string; tones: Record<string, string>; hint: string }> = {
  fundamental: {
    label: "基本面",
    tones: { 偏强: "mint", 中性: "gray", 偏弱: "coral", 无法判断: "warn" },
    hint: "企业经营质量方向（改善/稳定/恶化）——长期价值层结论",
  },
  valuation: {
    label: "价值估值",
    tones: { 低估: "mint", 合理: "blue", 偏贵: "coral", 无法判断: "warn" },
    hint: "按正常化盈利口径的贵贱结论；低 PE 分位本身不等于低估",
  },
  technical: {
    label: "技术状态",
    tones: { 确认: "mint", 待确认: "warn", 转弱: "coral", 无信号: "gray" },
    hint: "短期趋势确认状态——不改变长期价值结论",
  },
};

/** 四态估值状态 → 展示元数据（与后端 VALUATION_STATUS_LABELS 同源）。 */
export const VALUATION_STATUS_META: Record<string, { label: string; tone: string; hint: string }> = {
  undervalued: { label: "低估", tone: "mint", hint: "正常化盈利验证后的低估结论（低 PE 分位本身不等于低估）" },
  fair: { label: "合理", tone: "blue", hint: "正常化盈利验证后价格与价值大体相当" },
  expensive: { label: "偏贵", tone: "coral", hint: "正常化盈利验证后价格高于价值" },
  undetermined: { label: "无法判断", tone: "warn", hint: "未完成正常化盈利验证或证据不足——不据此判断贵贱" },
};

/**
 * v32 §二.1 首屏驾驶舱：三行分项判断（基本面/价值估值/技术状态）+ 适用期限。
 * 数据来自模型输出的 pillar_verdicts（服务端已归一）；旧行缺失时该行不渲染（不编造结论），
 * 无任何结论时整体不渲染，首屏退回「三行支撑计数」（DecisionCardPanel 的 pillar_rows）。
 */
export function ReportCockpitRows({ cockpit }: { cockpit?: ReportCockpit }) {
  if (!cockpit) return null;
  const rows = (["fundamental", "valuation", "technical"] as const)
    .map((key) => {
      const meta = COCKPIT_ROW_META[key]!;
      const verdict = (cockpit[key] ?? "").trim();
      if (!verdict) return null;
      const tone = meta.tones[verdict] ?? "gray";
      return { key, label: meta.label, verdict, tone, hint: meta.hint };
    })
    .filter((row): row is NonNullable<typeof row> => row !== null);
  const horizon = cockpit.horizon ?? [];
  if (rows.length === 0) return null;
  return (
    <div className="wb-cockpit" role="list" aria-label="首屏驾驶舱：基本面 / 价值估值 / 技术状态三行分项判断">
      {rows.map((row) => (
        <div key={row.key} className="wb-cockpit-row" role="listitem">
          <b>{row.label}</b>
          <span className={`soft-tag ${row.tone}`} title={row.hint}>{row.verdict}</span>
        </div>
      ))}
      {horizon.length > 0 && (
        <div className="wb-cockpit-row is-horizon" role="listitem">
          <b>适用期限</b>
          {horizon.map((item) => (
            <span key={item} className="soft-tag gray" title="该组结论适用的期限；三个时间尺度分开，禁止用一个标签覆盖全部期限">
              {item}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** v31 §4.1 首屏固定行：标的 · 报告模式 · 正文字数 · 质量状态（「标准版 · 2,153 字 · 完整」口径）。 */
export function ReportFirstScreen({ report }: { report: StockReport }) {
  const depthLabel = reportDepthLabel(report);
  const depthDeficit = depthLabel !== null && depthLabel !== REPORT_MODE_LABEL[report.report_mode ?? ""];
  const status = report.quality_status;
  const statusLabel = report.quality_status_label
    ?? (status === "complete" ? "完整" : status === "needs_review" ? "待复核" : status === "incomplete" ? "不完整" : null);
  const blockers = report.quality_blockers ?? [];
  const missing = report.missing_sections ?? [];
  const revision = report.report_revision;
  const repair = report.repair;
  // v32：驾驶舱三行结论词（顶层字段优先，退回结论卡内嵌；都缺失时不渲染）。
  const cockpit = report.pillar_verdicts ?? report.decision_card?.cockpit;
  // D03（前端设计与架构优化任务路线图 2026-09-19）：限制与验证点必须与结论同屏。
  const limitations = report.limitations ?? [];
  const watchpoints = report.watchpoints ?? [];
  return (
    <div className="wb-firstscreen">
      <div className="wb-result-meta">
        {depthLabel && (
          <span
            className={`soft-tag ${depthDeficit ? "warn" : "blue"}`}
            title={depthDeficit ? "正文低于该模式的字数下限（J06：档位深度如实呈现）；篇幅不足可能论证深度不够" : "本篇按该模式的字数预算做软告警（快速 1500-2500 / 标准 2000-3500 / 深研 3500-6000）"}
          >
            {depthLabel}
          </span>
        )}
        {typeof report.report_chars === "number" && !depthDeficit && <span className="soft-tag gray">正文 {report.report_chars} 字</span>}
        {statusLabel && (
          <span
            className={`soft-tag ${QUALITY_STATUS_TONE[status ?? ""] ?? "gray"}`}
            title="complete=无阻断项；needs_review=仅软告警（证据/时效/摘要长度等）；incomplete=有阻断项（缺章节/无正文/无引用），不得当作已完成研报使用"
          >
            {statusLabel}
          </span>
        )}
        {typeof revision === "number" && revision > 1 && (
          <span
            className="soft-tag gray"
            title={report.revision_history?.map((entry) => `修订 v${entry.revision}（${entry.strategy_label ?? entry.strategy}）：${entry.reason} → ${entry.result_label ?? entry.result ?? "—"}`).join("\n") || "经定向修复后的修订稿"}
          >
            修订 v{revision}
          </span>
        )}
        {repair?.attempted && repair.repaired === false && (
          <span className="soft-tag warn" title={repair.detail || "定向修复未成功，保留原稿（草稿）"}>
            修复未成功 · 保留原稿
          </span>
        )}
        {report.is_draft && (
          <span className="soft-tag warn" title="不完整报告仍保存为可回看的草稿（含缺失项与原因），不计入正式报告数量">
            草稿 · 不计入正式报告
          </span>
        )}
        {(() => {
          /* v33 A05：数据截止日 = 来源快照里最新的一天（证据时效一眼可见）。 */
          const asofs = Object.values(report.source_asof ?? {}).filter((value) => typeof value === "string" && value.length >= 10);
          const latest = asofs.length > 0 ? asofs.slice().sort()[asofs.length - 1]!.slice(0, 10) : null;
          return latest ? <span className="soft-tag gray" title="各来源数据的最新日期（详见证据页签）">数据截至 {latest}</span> : null;
        })()}
      </div>
      <ReportCockpitRows cockpit={cockpit} />
      {/* D03：首屏给出判断边界——「局限与口径声明」「验证点」与结论同屏（命名与正文页签、导出标题一致）；
          原文缺哪一项就写缺哪一项，不从别处补写。 */}
      <div className="wb-firstscreen-guard" data-testid="report-firstscreen-guard">
        <div className="wb-guard-row">
          <span className="answer-block-title">局限与口径声明</span>
          {limitations.length > 0 ? (
            <ul className="answer-list">{limitations.map((item) => <li key={item}>{item}</li>)}</ul>
          ) : (
            <p className="answer-list wb-guard-missing">原报告未给出局限声明。</p>
          )}
        </div>
        <div className="wb-guard-row">
          <span className="answer-block-title">验证点</span>
          {watchpoints.length > 0 ? (
            <ul className="answer-list">
              {watchpoints.slice(0, 3).map((item, i) => (
                <li key={`fsw-${i}`}>{item.signal || "—"} · 验证时点 {item.due_on || item.verify_by || "未给出"}</li>
              ))}
              {watchpoints.length > 3 && <li>另有 {watchpoints.length - 3} 项，见「情景与失效条件」。</li>}
            </ul>
          ) : (
            <p className="answer-list wb-guard-missing">原报告未给出验证点。</p>
          )}
        </div>
      </div>
      {status === "incomplete" && (
        <div className="wb-incomplete" role="alert">
          <b>生成不完整</b>——本报告缺少必需内容，不得当作已完成研报使用；原始模型输出与缺失项已如实保留。
          {missing.length > 0 && (
            <p className="wb-incomplete-missing">
              缺失小节：{missing.map((name) => <span key={name} className="soft-tag coral">{name} · 未生成</span>)}
            </p>
          )}
          {blockers.length > 0 && (
            <ul className="answer-list">
              {blockers.map((blocker, i) => (
                <li key={`${blocker.code}-${i}`}>{blocker.message}</li>
              ))}
            </ul>
          )}
          <p className="report-meta">可切换标准模式重新生成完整报告，或在下方逐项核对缺失原因。</p>
        </div>
      )}
    </div>
  );
}

/** 四大必需小节（与后端 REQUIRED_SECTIONS 同源）。 */
export const REQUIRED_SECTION_NAMES: readonly string[] = ["技术面", "消息面", "基本面", "综合判断"];

/**
 * v31 §4.2 正文目录：四大必需小节锚点常驻；缺失小节显示红色「未生成」占位，
 * 不让页面看起来像正常报告。旧记录无 section_states 时按 report_sections 尽力推断，
 * 两者都缺则只渲染锚点、不断言缺失（不编造状态）。
 */
export function ReportBodyToc({ report }: { report: StockReport }) {
  const states = report.section_states ?? [];
  const sections = report.report_sections ?? null;
  const itemOf = (name: string): { present: boolean | null; anchor: boolean } => {
    if (states.length > 0) {
      const state = states.find((item: SectionState) => item.section === name);
      return { present: state ? state.present : false, anchor: Boolean(state?.present) };
    }
    if (sections !== null) {
      const present = sections.some((title) => title.includes(name));
      return { present, anchor: present };
    }
    // 旧记录：无小节状态数据 → 渲染锚点但不标注缺失（缺失与否无从判定，不编造）。
    return { present: null, anchor: true };
  };
  const known = states.length > 0 || sections !== null;
  const scrollTo = (name: string): void => {
    document.getElementById(`wb-sec-${name}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  return (
    <nav className="wb-body-toc" aria-label="正文小节目录">
      <span className="wb-toc-label">目录</span>
      {REQUIRED_SECTION_NAMES.map((name) => {
        const { present } = itemOf(name);
        if (known && present === false) {
          return (
            <span key={name} className="wb-toc-item is-missing" title="该小节未生成（或仅有标题没有实质内容）——生成不完整，不作正常报告使用">
              {name} · 未生成
            </span>
          );
        }
        return (
          <a
            key={name}
            className="wb-toc-item"
            href={`#wb-sec-${name}`}
            onClick={(event) => { event.preventDefault(); scrollTo(name); }}
          >
            {name}
          </a>
        );
      })}
    </nav>
  );
}

/* —— v31 P1 正文重点标注（§八.5）：按 claim 重要度与已核验数值定位，禁止关键词自动染色 —— */

/** 正文高亮：tone 来自信息状态（蓝=关键结论 / 琥珀=待核验 / 珊瑚红=无来源支撑），不是涨跌方向。 */
export interface ReportHighlight {
  text: string;
  tone: "blue" | "amber" | "coral";
  /** 悬停/读屏的文字标签（每种颜色必须同时有文字表达，§3.2）。 */
  title?: string;
}

/** 从一段文本里提取数字 token（带 % 与正负号；同比/环比口径由正文语境表达，不在高亮里猜测）。 */
const NUMBER_TOKEN = /[+-]?\d+(?:\.\d+)?%?/g;

/**
 * 关键数字高亮来源（§八.5：按 claim 重要度和已核验数值定位）：
 * - 一句话结论里的数字 → 蓝（重要结论）；
 * - importance=high 且支撑 full 的判断里的数字 → 蓝；
 * - importance=high 且支撑 partial 的判断里的数字 → 琥珀（待核验）；
 * - importance=high 且支撑 none 的判断里的数字 → 珊瑚红（无来源支撑）。
 * 每段正文最多高亮 1～2 处（renderInline 内限制），总量封顶 8 个，正文黑白层次保持主导。
 */
export function keyFactHighlights(report: StockReport | null): ReportHighlight[] {
  if (!report) return [];
  const out: ReportHighlight[] = [];
  const seen = new Set<string>();
  const pushFrom = (text: string, tone: ReportHighlight["tone"], title: string): void => {
    for (const match of text.match(NUMBER_TOKEN) ?? []) {
      // 过滤噪声：单字符数字（如「3 条」的 3）不进高亮，避免满页染色。
      if (match.replace(/[+%.-]/g, "").length < 2) continue;
      if (seen.has(match)) continue;
      seen.add(match);
      out.push({ text: match, tone, title });
      if (out.length >= 8) return;
    }
  };
  const conclusion = report.conclusion?.statement ?? "";
  if (conclusion) pushFrom(conclusion, "blue", "一句话结论中的关键数字（重要结论，蓝色强调）");
  const claims = report.claim_findings?.claims ?? report.claims ?? [];
  for (const claim of claims) {
    if (claim.importance !== "high") continue;
    if (claim.support === "partial") pushFrom(claim.text, "amber", "核心判断（部分支持 · 待核验）中的关键数字");
    else if (claim.support === "none") pushFrom(claim.text, "coral", "核心判断（无来源支撑）中的关键数字");
    else if (claim.support === "full") pushFrom(claim.text, "blue", "核心判断（完整支持）中的关键数字");
    if (out.length >= 8) break;
  }
  return out;
}

/**
 * v39 Q19：深研完成度四项（盈利拆解 / 现金流与资本开支 / 估值解释 / 情景与反证）。
 * 完成度由服务端确定性推导，**字数只作辅助提示**：长而缺关键论证的报告不算研究完整，
 * 短而四项齐备的报告也不因篇幅被判无效——所以这里把「缺哪一项、缺什么口径」摊开给人看。
 */
export function ResearchCompletenessCard({
  completeness,
}: {
  completeness?: NonNullable<StockReport["research_completeness"]> | null;
}) {
  const items = completeness?.items ?? [];
  if (items.length === 0) return null;
  const toneOf = (state: string) => (state === "done" ? "mint" : state === "partial" ? "warn" : "coral");
  return (
    <div className="wb-completeness">
      <span className="wb-counter-label">
        深研完成度 {completeness?.done_count ?? 0}/{items.length}
        {completeness?.complete ? " · 四项齐备" : " · 关键研究项缺失"}
      </span>
      <ul className="answer-list">
        {items.map((item, i) => (
          <li key={`${item.key}-${i}`}>
            <span className={`soft-tag ${toneOf(item.state)}`} title={item.note || undefined}>
              {item.state_label || item.state}
            </span>
            <b> {item.label}</b>
            {(item.covered?.length ?? 0) > 0 && <span className="report-meta"> · 已覆盖 {item.covered!.join("、")}</span>}
            {(item.gaps?.length ?? 0) > 0 && <span className="report-meta amber"> · 缺 {item.gaps!.join("、")}</span>}
            {item.note && <p className="report-meta">{item.note}</p>}
          </li>
        ))}
      </ul>
      {completeness?.headline && <p className="report-meta">{completeness.headline}</p>}
      {/* 字数永远排在完成度之后，并带「只作辅助提示」的说明——不能反过来用字数代替论证。 */}
      {completeness?.word_count_note && (
        <p className="report-meta" title="字数只作辅助提示，不参与完成度判定（Q19）">{completeness.word_count_note}</p>
      )}
      {completeness?.note && <p className="report-meta">{completeness.note}</p>}
    </div>
  );
}

/**
 * v39 Q16/Q17/Q18：深研三块拆解。
 * 三条红线跟着数据走：贡献比例**算不出就不显示**（不显示 0%，也不显示估的数）；
 * 只有覆盖倍数时结论一律是「不足以判断盈利质量」；低 PE 分位永远不等于安全边际。
 */
export function DeepAnalysisBlocks({ report }: { report: StockReport }) {
  const earnings = report.earnings_growth ?? [];
  const cash = report.cash_flow_quality ?? null;
  const divergence = report.valuation_divergence ?? null;
  if (earnings.length === 0 && !cash && !divergence) return null;
  // 快照里的原值是全精度浮点（PE 11.69555428），屏显按两位小数收；与导出共用同一份实现。
  const value = deepMetricCell;
  const review = divergence?.comparable_review ?? null;
  return (
    <div className="wb-deep-analysis">
      {earnings.length > 0 && (
        <>
          <span className="wb-counter-label">盈利增长拆解（贡献比例只在能从原始数据复算时才给）</span>
          <table className="wb-judgement-table">
            <thead>
              <tr><th>因素</th><th>作用</th><th>贡献</th><th>依据与来源</th></tr>
            </thead>
            <tbody>
              {earnings.map((row, i) => (
                <tr key={`eg-${row.factor || i}`}>
                  <td>{row.factor_label || row.factor_raw || "未指明因素"}</td>
                  <td>{row.effect || "—"}</td>
                  <td>
                    {row.contribution_pct === null || row.contribution_pct === undefined
                      ? <span className="soft-tag warn" title="服务端不代填比例">未给出</span>
                      : `${row.contribution_pct}%`}
                  </td>
                  <td>
                    {row.basis || "—"}
                    <span className="report-meta">（{row.source_note || "无来源"}）</span>
                    {row.note && <p className="report-meta amber">{row.note}</p>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {cash && (
        <>
          <span className="wb-counter-label">现金流质量（覆盖倍数本身不等于盈利质量）</span>
          <p className="report-meta">
            经营现金流 {value(cash.operating_cash_flow)} · 归母净利 {value(cash.net_income)}
            · 覆盖倍数 {value(cash.coverage_ratio)} · 折旧摊销 {value(cash.depreciation_amortization)}
            · 营运资本变动 {value(cash.working_capital ?? cash.working_capital_change)}
            · 资本开支 {value(cash.capex)} · 自由现金流 {value(cash.free_cash_flow)}
          </p>
          <p className="report-meta">
            <span className={`soft-tag ${cash.strength === "insufficient" ? "warn" : "mint"}`} title={cash.note || undefined}>
              {cash.verdict || "未判定"}
            </span>
            {cash.note && <span> {cash.note}</span>}
          </p>
          {/* 正文已经下了「盈利质量较好」这类结论而口径不足时，服务端给降档说明（不改写正文）。 */}
          {cash.downgrade_note && <p className="report-meta amber" role="note">{cash.downgrade_note}</p>}
        </>
      )}
      {divergence && (
        <>
          <span className="wb-counter-label">估值分化与可比口径</span>
          <p className="report-meta">
            PE {value(divergence.pe_ttm)}（{value(divergence.pe_percentile)}）· PB {value(divergence.pb_mrq)}
            （{value(divergence.pb_percentile)}）· ROE {value(divergence.roe)}
            · 净资产口径 {value(divergence.equity_basis)}
          </p>
          {divergence.cycle_note && <p className="report-meta">盈利周期位置：{divergence.cycle_note}</p>}
          {review && (
            <p className="report-meta">
              <span
                className={`soft-tag ${review.comparability === "ok" ? "mint" : "warn"}`}
                title={review.note || undefined}
              >
                可比样本 {review.sample_count ?? 0}（{review.comparability_label || "未判定"}）
              </span>
              {" "}{review.note}
              {" "}剔除规则：{review.exclusion_rule}
            </p>
          )}
          <p className="report-meta amber">
            {divergence.sensitivity_note}
            {divergence.low_pe_is_not_margin_of_safety && " 低 PE 分位 ≠ 安全边际。"}
          </p>
          {divergence.guardrail && <p className="report-meta">{divergence.guardrail}</p>}
        </>
      )}
    </div>
  );
}

export function ReportQualityPanel({ report }: { report: StockReport }) {
  const dropped = report.citation_dropped ?? [];
  const warnings = report.quality_warnings ?? [];
  const available = report.available_citations ?? [];
  const completeness = report.research_completeness ?? null;
  const tagged
    = dropped.length > 0
    || warnings.length > 0
    || available.length > 0
    || typeof report.report_chars === "number"
    || (completeness?.items?.length ?? 0) > 0
    || (report.earnings_growth?.length ?? 0) > 0
    || Boolean(report.cash_flow_quality)
    || Boolean(report.valuation_divergence);
  if (!tagged) return null;
  return (
    <div className="wb-quality">
      <DeepAnalysisBlocks report={report} />
      <ResearchCompletenessCard completeness={completeness} />
      <div className="wb-quality-head">
        {report.citations.length > 0
          ? <span className="soft-tag">引用来源 {report.citations.join("/")}</span>
          : <span className="soft-tag gray">无真实引用</span>}
        {available.length > 0 && (
          <span className="soft-tag gray" title="本次真实取到的来源 id：S1 技术面恒在，S2/S3/S4 取数失败则不在其中，其编号不允许被引用">
            可引用 {available.join("/")}
          </span>
        )}
        {dropped.length > 0 && (
          <span className="soft-tag coral" title="模型引用了本次未取到的来源 id，已按「无引用不发布」剔除">
            已剔除虚假引用 {dropped.length}（{dropped.join("、")}）
          </span>
        )}
        {typeof report.report_chars === "number" && <span className="soft-tag gray">正文 {report.report_chars} 字</span>}
        {typeof report.summary_chars === "number" && (
          <span
            className={`soft-tag ${(report.summary_rewrite?.needed && !report.summary_rewrite?.rewritten) ? "warn" : "gray"}`}
            title="执行摘要字数（闸门口径）；超 180 字硬上限会触发一次「仅重写摘要」的模型调用，仍超限则保留原文交人工复核"
          >
            摘要 {report.summary_chars} 字
          </span>
        )}
        {report.summary_rewrite?.needed && (
          <span
            className={`soft-tag ${report.summary_rewrite.rewritten ? "mint" : "warn"}`}
            title={report.summary_rewrite.rewritten
              ? "原摘要超限，已由一次独立的「仅重写摘要」调用替换；正文未被改写"
              : report.summary_rewrite.detail || "原摘要超限且重写未成功，保留原文交人工复核（不静默截断）"}
          >
            {report.summary_rewrite.rewritten
              ? `摘要已重写（原 ${report.summary_rewrite.original_chars} → 现 ${report.summary_rewrite.rewritten_chars} 字）`
              : "摘要超限 · 保留原文待复核"}
          </span>
        )}
        {report.prompt_policy_version && <span className="soft-tag gray">提示词策略 {report.prompt_policy_version}</span>}
      </div>
      {(() => {
        /* v30 四级分组（四次方案 §2.2）：blocking 置顶常开、证据/计算展开、形式折叠；
           旧记录无 categorized_warnings 时回退平铺，不编造分组。 */
        const grouped = report.categorized_warnings;
        if (!grouped || grouped.length === 0) {
          if (warnings.length === 0) return null;
          return (
            <details className="wb-quality-warn" open>
              <summary>质量告警 {warnings.length} 条（不阻断交付，供人工复核）</summary>
              <ul className="answer-list">{warnings.map((item, i) => <li key={i}>{item}</li>)}</ul>
            </details>
          );
        }
        const order = ["blocking", "evidence", "computation", "format"] as const;
        const toneOf: Record<string, string> = { blocking: "coral", evidence: "warn", computation: "blue", format: "gray" };
        const groups = order
          .map((category) => ({ category, items: grouped.filter((item) => item.category === category) }))
          .filter((group) => group.items.length > 0);
        return (
          <div className="wb-warn-groups">
            {groups.map((group) => (
              <details
                key={group.category}
                className={`wb-quality-warn wb-warn-group cat-${group.category}`}
                open={group.category === "blocking" || group.category === "evidence" || group.category === "computation"}
              >
                <summary>
                  <span className={`soft-tag ${toneOf[group.category] ?? "gray"}`}>{group.items[0]?.category_label ?? group.category}</span>
                  {group.items.length} 条
                  {group.category === "blocking" && "（需人工确认后再采信）"}
                </summary>
                <ul className="answer-list">
                  {group.items.map((item, i) => (
                    <li key={i}>
                      {item.message}
                      {(item.impact || item.action) && (
                        <small className="wb-warn-meta">
                          {item.impact && <>影响：{item.impact}</>}
                          {item.action && <> · 建议动作：{item.action}</>}
                        </small>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        );
      })()}
    </div>
  );
}

/* ---------- v27「可信交付」：证据分级 / claim 覆盖 / 反方检查 ---------- */

/** 证据等级口径（与后端 SOURCE_PROFILES 同源，见 report_quality.py）。 */
export const QUALITY_TIER_LABEL: Record<string, string> = {
  A: "A 级 · 公告/财报/交易所原始数据",
  B: "B 级 · 公司正式说明/二次分发",
  C: "C 级 · 媒体/数据商摘要",
  D: "D 级 · 全市场扫描/传闻",
};

export const CLAIM_STATUS_TONE: Record<string, string> = {
  supported: "mint",
  downgraded: "warn",
  no_source: "coral",
};

/* —— JV04 语义支撑层：Jev 逐 claim 判定「所引证据的内容是否真的支撑该判断」—— */

/** 语义支撑四选项 → 色板。「无关」与「不支撑」都是强信号（证据相悖 vs 拿错证据），同为 coral。 */
export const JEV_SUPPORT_TONE: Record<string, string> = {
  support: "mint",
  partial: "warn",
  unsupport: "coral",
  irrelevant: "coral",
};

/** 编造风险三路（noul）→ 色板；uncertain 只标注，不二值化。 */
export const JEV_INVENTED_TONE: Record<string, string> = {
  yes: "coral",
  uncertain: "warn",
  no: "gray",
};

export const JEV_INVENTED_LABEL: Record<string, string> = {
  yes: "疑似编造事实",
  uncertain: "编造存疑",
  no: "未见编造",
};

/**
 * v27 证据质量与结论支撑（方案 §3.1）。
 *
 * 与上面的 `ReportQualityPanel` 是**两件事**，故并列展示而非合并：
 *  - ReportQualityPanel 管**形式**（引用编号是否真实存在、字数、小节）；
 *  - 本面板管**实质**（这条判断所引来源是否真的覆盖了它需要的字段、等级够不够）。
 * 不渲染条件：既无 evidence_quality 也无 claims（旧记录）——不编造分级。
 */
/**
 * Q09（2026-09-19 路线图）：来源目录表——研报证据区与追问附录共用一份实现。
 * 缺链接一律如实写「（未提供链接）」，绝不按来源名猜 URL；`used_by` 让「S1 支撑了哪条判断」可回查。
 */
export function SourceCatalogTable({ rows, title = "来源目录（编号 / 名称 / 可用链接 / 数据日期 / 获取时间 / 口径限制）" }: {
  rows: SourceCatalogEntry[];
  title?: string;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="wb-source-catalog">
      <span className="wb-counter-label">{title}</span>
      {/* Q23：九列审计表在窄窗（≈480）下不压成竖排、也不越界遮挡正文——横向滚动 + 表头可换行。 */}
      <div className="wb-table-scroll">
        <table className="wb-judgement-table wb-source-catalog-table">
          <thead>
            <tr>
              <th>编号</th><th>名称</th><th>可用链接</th><th>数据日期</th><th>获取时间</th><th>等级</th><th>引用状态</th><th>被哪些判断使用</th><th>口径限制</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={`${row.source_id}-${i}`}>
                <td>{row.source_id || "—"}</td>
                <td>{row.name || "（未提供名称）"}</td>
                <td>
                  {row.url?.trim()
                    ? <a className="wb-source-link" href={row.url.trim()} target="_blank" rel="noreferrer">{row.url.trim()}</a>
                    : <span className="report-meta">{sourceCatalogUrlText(row)}</span>}
                </td>
                <td>{row.data_date || "未标注"}</td>
                <td>{row.retrieved_at || "未标注"}</td>
                <td>{row.quality || "—"}</td>
                <td>{row.cited === false ? "可得未引用" : "已引用"}</td>
                <td>{(row.used_by ?? []).join("、") || "—"}</td>
                <td>{row.scope_note || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="report-meta">链接口径：只列来源记录里真实存在的链接；「（未提供链接）」表示本次没有可访问地址，不代表来源不存在。</p>
    </div>
  );
}

export function EvidenceQualityPanel({ report }: { report: StockReport }) {
  const quality = report.evidence_quality;
  const findings = report.claim_findings;
  const claims = report.claims ?? findings?.claims ?? [];
  // 只有来源目录（无分级/无 claims）时也要渲染——Q09 的来源可回查不依赖证据分级是否落地。
  if (!quality && claims.length === 0 && (report.source_catalog?.length ?? 0) === 0) return null;
  // J03（桌面端升级路线图 2026-09-18）：三态与分母从 findings.claims 现算（旧新档案同一公式），
  // 「有支撑」= 核心判断全数支撑；部分支撑带分母，禁止用非核心条数撑住判定。
  const { state: coreState, counts: coreCounts } = coreSupportState(findings);
  // Q09：与导出同一份编号清单——屏显的「2/3」也要能说出数的是哪几条判断。
  const coreBreakdown = coreSupportBreakdown(findings);
  const coreSupportHint = coreBreakdown.coreIds.length > 0
    ? `计入统计的核心判断：${coreBreakdown.coreIds.join("、")}`
      + `（完整支撑 ${coreBreakdown.supportedIds.join("、") || "无"}；未完整支撑 ${coreBreakdown.unsupportedIds.join("、") || "无"}）`
    : "模型未输出结构化核心判断清单，无法回查统计对象";
  const warnings = findings?.warnings ?? [];
  const hasTierA = (quality?.tiers?.A ?? []).length > 0;
  /* JV04：语义支撑层回执。缺失（undefined）≠ 未运行：老档案本就没有这一层。 */
  const jev = report.jev ?? findings?.jev;
  const jevSkipped = jev?.skipped ?? [];
  const jevInvented = jev?.invented ?? [];
  const jevWeakened = jev?.weakened ?? [];
  return (
    <div className="wb-evidence">
      <div className="wb-evidence-head">
        <span className="answer-block-title">证据质量与结论支撑</span>
        {coreState === "supported" && <span className="soft-tag mint" title={coreSupportHint}>核心结论有支撑</span>}
        {coreState === "partial" && (
          <span
            className="soft-tag warn"
            title={`${coreSupportHint}；核心判断仅部分获得支撑，按方案口径未全数支撑不得作为可发布的核心结论`}
          >
            核心结论部分支撑（{coreCounts.supported}/{coreCounts.total}）
          </span>
        )}
        {coreState === "unsupported" && (
          <span className="soft-tag coral" title={`${coreSupportHint}；没有任何一条核心判断达到「有支撑」，按方案口径不得作为可发布的核心结论`}>
            核心结论无支撑
          </span>
        )}
        {coreState === null && (
          <span className="soft-tag warn" title="模型未输出结构化核心判断清单（claims），无法校验「引用是否支撑该判断」——不假装通过">
            无法判定 · 未输出 claims
          </span>
        )}
        {findings && (
          <span className="soft-tag gray">
            全部判断 {findings.supported}/{findings.total} 有支撑
            {findings.downgraded.length > 0 && ` · ${findings.downgraded.length} 降级`}
            {findings.no_source.length > 0 && ` · ${findings.no_source.length} 无来源`}
          </span>
        )}
        {/* JV04：语义支撑层状态——「没跑」「跑了但有降级」「跑了且通过」三态必须能区分。 */}
        {jev?.available && jev.downgraded.length > 0 && (
          <span className="soft-tag warn" title={jev.note || undefined}>
            语义支撑 {jev.downgraded.length} 条降级
          </span>
        )}
        {jev?.available && jevInvented.length > 0 && (
          <span className="soft-tag coral" title="判断里出现所引证据中找不到的具体事实，须人工复核">
            {jevInvented.length} 条疑似编造事实
          </span>
        )}
        {jev?.available && (
          <span className="soft-tag gray" title={jev.note || undefined}>
            语义比对 {jev.evaluated} 条 · {jev.model || "Jev"}
          </span>
        )}
        {jev && !jev.available && (
          <span className="soft-tag gray" title={jev.note || undefined}>
            语义支撑层未运行
          </span>
        )}
      </div>

      <p className="wb-evidence-legend">
        等级口径：{(["A", "B", "C", "D"] as const).map((tier) => QUALITY_TIER_LABEL[tier]).join(" · ")};
        核心结论需 <b>{(quality?.core_support_qualities ?? ["B"]).join("/")}</b> 级及以上。
        {!hasTierA && <span className="wb-evidence-gap"> 本次无任何来源达 A 级。</span>}
      </p>

      {/* JV04：两套口径必须讲清分工，否则用户会把「引用真实」当成「引用支撑得住」。 */}
      <p className="wb-evidence-legend">
        {jev?.available
          ? "支撑判定分两层：引用真实性/字段覆盖/证据等级由系统确定性校验；「所引内容在语义上是否真的支撑该判断」由 Jev 判定（只标注与降级，不改写原话）。"
          : "支撑判定只跑了确定性层（引用真实性/字段覆盖/证据等级）：「所引内容在语义上是否真的支撑该判断」本次未校验，相关结论按未做语义核验对待。"}
      </p>
      {(jevSkipped.length > 0 || jevWeakened.length > 0 || jevInvented.length > 0) && (
        <p className="wb-evidence-legend">
          {jevInvented.length > 0 && (
            <span className="wb-evidence-gap">
              疑似编造：{jevInvented.join("、")}（判断含所引证据中找不到的具体事实，须人工复核）。{" "}
            </span>
          )}
          {jevWeakened.length > 0 && (
            <>语义认为强度不足但未改判定：{jevWeakened.join("、")}（中文样本尚未标定，暂只标注）。{" "}</>
          )}
          {jevSkipped.length > 0 && (
            <>
              未进语义比对 {jevSkipped.length} 条：
              {jevSkipped.map((item) => `${item.claim_id}（${JEV_SKIP_LABEL[item.reason] ?? item.reason}）`).join("、")}
              ——「未校验」不等于「通过」。
            </>
          )}
        </p>
      )}

      {quality && quality.sources.length > 0 && (
        <ul className="wb-evidence-sources">
          {quality.sources.map((source) => {
            /* v29 来源元数据：提供方 / 数据截至 / 取数时刻（都是后端取数结果，缺哪个少哪个）。 */
            const meta = report.source_meta?.[source.id];
            const asof = report.source_asof?.[source.id];
            return (
              <li key={source.id}>
                <span className={`quality-pill q-${source.quality}`}>{source.quality}</span>
                <b>{source.id}</b>
                <span className="wb-evidence-label">{source.label}</span>
                <small>可覆盖字段：{source.coverage.length > 0 ? source.coverage.join("、") : "—（该来源不覆盖任何词表字段）"}</small>
                {(meta?.provider || asof || meta?.retrieved_at) && (
                  <small className="wb-src-meta">
                    {meta?.provider && <>来源 {meta.provider}</>}
                    {asof && <> · 数据截至 {asof}</>}
                    {meta?.retrieved_at && <> · 取数 {meta.retrieved_at.slice(0, 19).replace("T", " ")}</>}
                  </small>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {/* Q09：来源目录（含可用链接与 used_by）——只有编号时导出件查无出处，屏显同理。 */}
      <SourceCatalogTable rows={report.source_catalog ?? []} />

      {claims.length > 0 && (
        <table className="wb-evidence-table">
          <thead>
            <tr>
              <th>核心判断</th>
              <th title="经校验真实存在的引用编号；不属于本次证据包的编号已被剔除">引用</th>
              <th title="该判断需要证据覆盖的字段（取自覆盖词表）">需要字段</th>
              <th title="所引来源实际未能覆盖的字段——这些结论只能当待核验线索">覆盖缺口</th>
              <th>最高等级</th>
              <th title="supported=引用真实且覆盖齐全且等级达 B 以上；downgraded=降级为「未独立验证」；no_source=引用来源均不存在">结论</th>
            </tr>
          </thead>
          <tbody>
            {claims.map((claim) => (
              <Fragment key={claim.claim_id}>
                <tr className={claim.status === "supported" ? "" : "is-weak"}>
                  <td>
                    {claim.text || "—"}
                    {(claim.aspects?.length ?? 0) > 0 && (
                      <details className="wb-aspects">
                        <summary>三层拆分（事实/比较/判断）</summary>
                        <ul className="wb-aspect-list">
                          {claim.aspects!.map((aspect, ai) => (
                            <li key={ai} className={`wb-aspect aspect-${aspect.kind}`}>
                              <span className={`soft-tag ${SUPPORT_TONE[aspect.support ?? ""] ?? "gray"}`} title={aspect.note || undefined}>
                                {aspect.kind_label ?? aspect.kind} · {aspect.support_label ?? aspect.support ?? "—"}
                              </span>
                              <span className="wb-aspect-text">{aspect.text || "—"}</span>
                              <small className="wb-aspect-meta">
                                {aspect.sources.join("/") || "无来源"}
                                {aspect.missing_coverage.length > 0 && ` · 缺口 ${aspect.missing_coverage.join("、")}`}
                              </small>
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </td>
                  <td>
                    {claim.sources.join("/") || "—"}
                    {claim.dropped_sources.length > 0 && (
                      <small className="coral">已剔除 {claim.dropped_sources.join("/")}</small>
                    )}
                  </td>
                  <td>{claim.requires.join("、") || "—"}</td>
                  <td>{claim.missing_coverage.join("、") || "—"}</td>
                  <td>{claim.evidence_quality || "—"}</td>
                  <td>
                    <span className={`soft-tag ${CLAIM_STATUS_TONE[claim.status] ?? "gray"}`} title={claim.note || undefined}>
                      {CLAIM_STATUS_LABEL[claim.status] ?? claim.status}
                    </span>
                    {/* JV04：语义判定与确定性结论并列——两者可能不一致，不一致本身就是信息。 */}
                    {claim.jev && (
                      <span
                        className={`soft-tag ${JEV_SUPPORT_TONE[claim.jev.support] ?? "gray"}`}
                        title={
                          `语义比对来源：${claim.jev.sources_checked.join("、") || "—"}`
                          + (claim.jev.confidence != null ? `；confidence ${claim.jev.confidence}` : "")
                          + (claim.jev.invented_value != null ? `；编造判定值 ${claim.jev.invented_value}` : "")
                        }
                      >
                        语义 · {claim.jev.support_label}
                      </span>
                    )}
                    {claim.jev?.invented && claim.jev.invented !== "no" && (
                      <span className={`soft-tag ${JEV_INVENTED_TONE[claim.jev.invented] ?? "gray"}`}>
                        {JEV_INVENTED_LABEL[claim.jev.invented]}
                      </span>
                    )}
                    {claim.support_source === "jev_semantic" && (
                      <small className="wb-evidence-gap">支撑强度由语义层改写</small>
                    )}
                  </td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      )}

      {warnings.length > 0 && (
        <details className="wb-quality-warn" open>
          <summary>覆盖校验告警 {warnings.length} 条（只降级与标注，不改写模型原话）</summary>
          <ul className="answer-list">{warnings.map((item, i) => <li key={i}>{item}</li>)}</ul>
        </details>
      )}
      {quality?.note && <p className="report-meta">{quality.note}</p>}
    </div>
  );
}

/**
 * v27 轻量反方检查（方案 §3.2）：单次短调用，只指出软肋，不重写报告、不新增结论。
 * `ok=false` 时如实展示「未完成」与原因——**不阻断报告交付**，这是设计而非降级。
 */
export function CounterCheckPanel({ report }: { report: StockReport }) {
  const check = report.counter_check;
  if (!check) return null;
  if (!check.ok) {
    return (
      <div className="wb-counter wb-counter-failed">
        <div className="wb-counter-head">
          <span className="answer-block-title">反方检查</span>
          <span className="soft-tag warn">本次未完成</span>
          {check.prompt_version && <span className="soft-tag gray">提示词 {check.prompt_version}</span>}
        </div>
        <p className="report-meta">{check.detail || "本次未能完成反方检查（不影响本报告的交付与结论）。"}</p>
      </div>
    );
  }
  const robustness = ROBUSTNESS_META[check.verdict_robustness ?? "mixed"] ?? ROBUSTNESS_META.mixed!;
  const conditions = check.necessary_conditions ?? [];
  const misread = check.most_misread_sentence;
  return (
    <div className="wb-counter">
      <div className="wb-counter-head">
        <span className="answer-block-title">反方检查</span>
        <span className={`soft-tag ${robustness.tone}`}>{robustness.text}</span>
        {check.prompt_version && <span className="soft-tag gray">提示词 {check.prompt_version}</span>}
      </div>
      <p className="wb-counter-note">本段为一次独立的短调用，只负责指出软肋；正文未被改写，规则分与 AI 判断并列不互相覆盖。</p>
      <div className="wb-counter-grid">
        <div className="wb-counter-item">
          <span className="wb-counter-label">最强支持证据</span>
          <p>{check.strongest_support || "—"}</p>
        </div>
        <div className="wb-counter-item is-counter">
          <span className="wb-counter-label">最强反证</span>
          <p>{check.strongest_counter || "—"}</p>
        </div>
      </div>
      {conditions.length > 0 && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">结论成立的必要条件（缺一即不成立）</span>
          <ul className="answer-list">{conditions.map((item, i) => <li key={i}>{item}</li>)}</ul>
        </div>
      )}
      {misread?.quote && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">最容易被误读的一句</span>
          <blockquote className="wb-counter-quote">「{misread.quote}」</blockquote>
          {misread.why && <p className="report-meta">为什么会被误读：{misread.why}</p>}
        </div>
      )}
    </div>
  );
}

/** v28 结论方向 → 展示文案与色号（涨红跌绿遵循中国市场惯例；越界值原样展示，不猜测）。 */
export const CONCLUSION_DIRECTION_META: Record<string, { text: string; tone: string }> = {
  bullish: { text: "偏多", tone: "coral" },
  bearish: { text: "偏空", tone: "mint" },
  neutral: { text: "中性", tone: "gray" },
  conflict: { text: "多空并存", tone: "warn" },
};

/** v28 支撑强度 → 文案与色号（后端确定性判定，前端只搬运不重判）。 */
export const SUPPORT_TONE: Record<string, string> = { full: "mint", partial: "warn", none: "coral" };

/**
 * v28 首屏结论卡（二次方案 §2.1/§2.2）：结论 / 证据强弱 / 最大支持 / 最大反证 / 下一动作。
 * 纯聚合后端已有的结构化字段（evidence_quality / claim_findings / counter_check / watchpoints / valuation），
 * 前端**零新判断**；字段缺失时逐项如实降级，不编造。
 */
export function DecisionCardPanel({ report }: { report: StockReport }) {
  const card = report.decision_card;
  if (!card) return null;
  const direction = card.conclusion.direction;
  const directionMeta = CONCLUSION_DIRECTION_META[direction];
  const counts = card.evidence_strength.counts ?? {};
  const support = card.evidence_strength.support_counts;
  return (
    <div className="wb-decision-card">
      <div className="wb-counter-head">
        <span className="answer-block-title">结论卡</span>
        {card.conclusion.statement ? (
          <span className={`soft-tag ${directionMeta?.tone ?? "gray"}`}>
            {directionMeta?.text ?? direction}
          </span>
        ) : (
          <span className="soft-tag warn" title="模型未输出一句话结论（conclusion.statement），首屏结论卡只能留空">
            无一句话结论
          </span>
        )}
        {/* J03：结论卡与证据面板/导出同一三态口径（核心全数支撑才算「有支撑」，部分带分母）。 */}
        {card.evidence_strength.core_conclusion_state === "supported" && (
          <span className="soft-tag mint">核心判断有支撑</span>
        )}
        {card.evidence_strength.core_conclusion_state === "partial" && (
          <span className="soft-tag warn">
            核心判断部分支撑（{card.evidence_strength.core_support_counts?.supported ?? "—"}/{card.evidence_strength.core_support_counts?.total ?? "—"}）
          </span>
        )}
        {card.evidence_strength.core_conclusion_state === "unsupported" && (
          <span className="soft-tag coral">核心判断无支撑</span>
        )}
        {card.evidence_strength.core_conclusion_state == null &&
          card.evidence_strength.core_conclusion_supported == null && (
          <span className="soft-tag gray">支撑度未判定</span>
        )}
      </div>

      {card.conclusion.statement && (
        <p className="wb-decision-statement">
          <b>{card.conclusion.statement}</b>
          {card.conclusion.core_conflict && (
            <span className="report-meta"> 核心分歧：{card.conclusion.core_conflict}</span>
          )}
        </p>
      )}

      <div className="wb-decision-grid">
        <div className="wb-counter-item">
          <span className="wb-counter-label">证据强弱</span>
          <p>
            共 {card.evidence_strength.total_sources} 个来源
            {(["A", "B", "C", "D"] as const)
              .filter((tier) => (counts[tier] ?? 0) > 0)
              .map((tier) => ` · ${tier} ${counts[tier]}`)
              .join("")}
            {card.evidence_strength.core_min_quality && ` · 核心判断最低 ${card.evidence_strength.core_min_quality} 级`}
          </p>
          {support && (
            <p className="report-meta">
              判断支撑：完整 {support.full} / 部分 {support.partial} / 无来源 {support.none}
            </p>
          )}
          {card.evidence_strength.note && <p className="report-meta">{card.evidence_strength.note}</p>}
        </div>

        <div className="wb-counter-item">
          <span className="wb-counter-label">估值口径</span>
          {(() => {
            /* v32：四态估值状态随卡下发（服务端确定性推导），不再从 verdict 文本猜贵贱；
               正常化盈利有值时并列展示——它是「低估」结论成立的前提（低 PE 分位 ≠ 低估）。
               v39 Q05：结论词与估值卡走同一三态函数——结论卡说「合理价值未评估」、估值卡说「无数据」
               这类两套口径，正是样报里被读者当成矛盾的地方。 */
            const evidence = valuationEvidenceDisplay(report.valuation);
            const rawLabel = (card.valuation_label || "").trim();
            const labelMasked = rawLabel === "" || rawLabel === "无数据" || rawLabel === "无法判断";
            const displayLabel = labelMasked ? evidence.verdictDisplay : rawLabel;
            const statusMeta = card.valuation_status ? VALUATION_STATUS_META[card.valuation_status] : undefined;
            const earnings = card.normalized_earnings;
            return (
              <>
                {(displayLabel || statusMeta) ? (
                  <p>
                    {displayLabel}
                    {statusMeta && !labelMasked && (
                      <span
                        className={`soft-tag ${statusMeta.tone}`}
                        title={card.valuation_status_note || statusMeta.hint}
                      >
                        {statusMeta.label}
                      </span>
                    )}
                    <span className={`soft-tag ${evidence.tone}`} title={evidence.note || evidence.label}>
                      {evidence.label}
                    </span>
                    {card.valuation_label_note && <span className="report-meta">（{card.valuation_label_note}）</span>}
                  </p>
                ) : (
                  <p className="report-meta">模型未输出结构化估值判断，估值结论只在正文里，无法机读展示。</p>
                )}
                {labelMasked && evidence.note && (
                  <p className="report-meta amber" role="note">{evidence.note}</p>
                )}
                {earnings && typeof earnings.value === "number" && (
                  <p className="report-meta" title="正常化盈利 = 剔除一次性/周期因素后的可持续口径，是估值结论的前提">
                    正常化盈利 {earnings.value} 亿（{earnings.confidence || "置信未标注"}）
                    {earnings.basis ? `：${earnings.basis}` : ""}
                  </p>
                )}
                {card.valuation_status && !statusMeta && (
                  <p className="report-meta">估值状态「{card.valuation_status}」不在既定词表内，按原值如实展示。</p>
                )}
              </>
            );
          })()}
        </div>

        <div className="wb-counter-item is-support">
          <span className="wb-counter-label">最大支持</span>
          {card.max_support ? (
            <p>
              {card.max_support.text}
              {card.max_support.source && (
                <small className="report-meta">
                  {" "}来源 {card.max_support.source}
                  {card.max_support.as_of ? ` · 数据 ${card.max_support.as_of}` : ""}
                </small>
              )}
            </p>
          ) : (
            <p className="report-meta">{card.counter_check_reason || "本次未取得最大支持项。"}</p>
          )}
        </div>

        <div className="wb-counter-item is-counter">
          <span className="wb-counter-label">最大反证</span>
          {card.max_counter ? (
            <p>
              {card.max_counter.text}
              {card.max_counter.source && (
                <small className="report-meta">
                  {" "}来源 {card.max_counter.source}
                  {card.max_counter.as_of ? ` · 数据 ${card.max_counter.as_of}` : ""}
                </small>
              )}
            </p>
          ) : (
            <p className="report-meta">{card.counter_check_reason || "本次未取得最大反证项。"}</p>
          )}
        </div>
      </div>

      {(card.pillar_rows?.length ?? 0) > 0 && (
        <div className="wb-pillars">
          <span className="wb-counter-label">三行分项（按判断依赖字段归类，服务端确定性可复算；无归类的判断不强行分柱）</span>
          <ul className="wb-pillar-list">
            {card.pillar_rows!.map((row) => (
              <li key={row.pillar} className={`wb-pillar-row pillar-${row.pillar}`}>
                <b>{row.label}</b>
                <span className="wb-pillar-counts">
                  判断 {row.total} · <span className="mint">完整 {row.full}</span> / <span className="warn">部分 {row.partial}</span> / <span className="coral">无来源 {row.none}</span>
                </span>
                {row.representative && <span className="wb-pillar-representative" title="该柱 importance 最高的判断">代表：{row.representative}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {card.next_action && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">下一动作（最近的验证点）</span>
          <p>
            {card.next_action.signal || "（未给出信号描述）"} · 验证时点 {card.next_action.verify_by || "—"}
            {card.next_action.event_anchored && (
              <span className="soft-tag gray" title="验证时点锚定事件（如「三季报披露后」），无确切日期，由事件触发复盘——不猜日期">
                事件触发
              </span>
            )}
          </p>
          {card.next_action.expected_if_true && (
            <p className="report-meta">届时应看到：{card.next_action.expected_if_true}</p>
          )}
        </div>
      )}

      {(card.downgrade_notes?.length ?? 0) > 0 && (
        <ul className="answer-list wb-decision-notes">
          {card.downgrade_notes!.map((item, i) => <li key={i}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

/* —— v30 结果区页签；v33（2026-09-14 路线图 A05/A06）重排为六页签：
   概览（结论卡+执行摘要+估值）/ 正文 / 图表 / 情景与验证 / 证据 / 追问。
   导航先于长内容出现且在阅读区内吸顶（`is-sticky`）；首屏只保留身份信息与驾驶舱三行，
   长结论卡、摘要、最大支持/反证全部收进「概览」，避免一屏塞满。
   各面板在旧记录字段缺失时自身返回 null，整组全空时如实标注（不显示假页签）。 */
export type ReportTabKey = "overview" | "body" | "charts" | "verify" | "evidence" | "followup";

export const REPORT_TAB_META: Record<ReportTabKey, { label: string; hint: string }> = {
  overview: { label: "概览", hint: "结论卡（最大支持/反证/下一动作）+ 执行摘要 + 估值判断" },
  body: { label: "正文", hint: "研报正文（目录锚点 + 关键数字标注）" },
  charts: { label: "图表", hint: "价格量能 / 单季财务 / 估值分位（与报告引用同源，含数值口径说明）" },
  verify: { label: "情景与验证", hint: "情景失效条件与有效窗口 / 关键价位 / 验证点清单（可证伪口径）" },
  evidence: { label: "证据", hint: "质量闸门痕迹 / 证据分级与结论支撑 / 反方检查" },
  followup: { label: "追问", hint: "追问本报告：受影响判断 / 结论变化 / 新增验证点，结果保存为不可变分析附录" },
};

export function ReportResultTabs({
  report,
  bodyAppend,
  evidenceAppend,
  followup,
  followupCount = 0,
  followupActive = false,
}: {
  report: StockReport;
  /** 追加在正文页签末尾的调用方专属内容（如局限声明）。 */
  bodyAppend?: ReactNode;
  /** 追加在证据页签末尾的调用方专属内容（如部分来源取数失败说明）。 */
  evidenceAppend?: ReactNode;
  /** v32 追问页签内容；未提供（undefined）时该页签整体不显示。 */
  followup?: ReactNode;
  /** 追问附录数量（页签角标）。 */
  followupCount?: number;
  /** 追问正在提交/载入时给页签挂动态态。 */
  followupActive?: boolean;
}) {
  const [tab, setTab] = useState<ReportTabKey>("overview");
  const evidenceEmpty = report.quality_warnings == null
    && report.citation_dropped == null
    && report.evidence_quality == null
    && report.claim_findings == null
    && report.counter_check == null
    && !evidenceAppend;
  // 页签角标：待复核数量（情景与验证=待复盘验证点；证据=待核验判断；追问=附录数）。
  const supportCounts = report.claim_findings?.support_counts;
  const pendingClaims = (supportCounts?.partial ?? 0) + (supportCounts?.none ?? 0);
  const pendingWatchpoints = (report.watchpoints ?? []).length;
  const highlights = useMemo(() => keyFactHighlights(report), [report]);
  const badges: Partial<Record<ReportTabKey, number>> = {
    verify: pendingWatchpoints,
    evidence: pendingClaims,
    followup: followupCount,
  };
  const tabs = (Object.keys(REPORT_TAB_META) as ReportTabKey[]).filter(
    (key) => key !== "followup" || followup !== undefined,
  );
  return (
    <div className="wb-result-tabs">
      <div className="wb-result-tabbar is-sticky" role="tablist" aria-label="研报阅读导航">
        {tabs.map((key) => {
          const badge = badges[key] ?? 0;
          return (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              className={`wb-result-tab ${tab === key ? "active" : ""}`}
              title={REPORT_TAB_META[key]!.hint}
              onClick={() => setTab(key)}
            >
              {REPORT_TAB_META[key]!.label}
              {badge > 0 && (
                <span
                  className={`wb-tab-badge ${key === "verify" ? "blue" : key === "followup" ? "blue" : "warn"}`}
                  title={key === "verify" ? `${badge} 条验证点待复盘` : key === "followup" ? `${badge} 条追问附录` : `${badge} 条判断待核验（部分支持/无来源）`}
                >
                  {badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
      {tab === "overview" && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          {/* A05：长结论卡 / 执行摘要 / 最大支持与反证归入概览；报告导航（页签）先于这些内容出现。 */}
          <DecisionCardPanel report={report} />
          {report.executive_summary && (
            <div className="wb-exec-summary">
              {report.executive_summary}
              {(report.summary_downgrade_notes?.length ?? 0) > 0 && (
                <ul className="answer-list wb-summary-notes">
                  {report.summary_downgrade_notes!.map((item, i) => <li key={i}>{item}</li>)}
                </ul>
              )}
            </div>
          )}
          <ValuationCard report={report} />
        </div>
      )}
      {tab === "body" && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          <ReportBodyToc report={report} />
          <MarkdownishText content={report.report} highlights={highlights} />
          {bodyAppend}
        </div>
      )}
      {tab === "charts" && (
        <div className="wb-result-tabpane" role="tabpanel">
          {/* v33 A11：图表独立页签，脱离长正文；高度受控（A12：含口径说明与数值表）。 */}
          <ReportChartPack report={report} />
        </div>
      )}
      {tab === "verify" && (
        <div className="wb-result-tabpane" role="tabpanel">
          <ScenarioFalsifiability report={report} />
          <StructuredJudgement report={report} />
          {pendingWatchpoints === 0 && (report.watchpoints ?? []).length === 0 && (
            <p className="report-meta">本报告没有结构化验证点（旧版本生成），无法安排跟踪复盘。</p>
          )}
        </div>
      )}
      {tab === "evidence" && (
        <div className="wb-result-tabpane" role="tabpanel">
          <ReportQualityPanel report={report} />
          <EvidenceQualityPanel report={report} />
          <CounterCheckPanel report={report} />
          {evidenceEmpty && evidenceAppend == null && (
            <p className="report-meta">本报告无质量闸门痕迹、证据分级或反方检查记录（旧版本生成）。</p>
          )}
          {evidenceAppend}
        </div>
      )}
      {tab === "followup" && followup !== undefined && (
        <div className={`wb-result-tabpane ${followupActive ? "is-busy" : ""}`} role="tabpanel">
          {followup}
        </div>
      )}
    </div>
  );
}

/**
 * v27 情景补字段（方案 §3.3）：`invalidates` + `horizon`（+ 概率/预期区间）。
 * 与 `StructuredJudgement` 的情景表分开渲染：那张表回答「什么会触发」，
 * 这张表回答「什么会让它失效、多长窗口内有效」——后者才是可证伪的关键。
 */
/* —— v29 三次升级（方案 §3）：研报图表数据包 ——
   三组图全部由服务端取数结果直接生成（与报告引用同一份 evidence_meta），前端只做整型渲染，
   不做任何二次推断；取不到的块如实显示「数据不可用」，绝不画空图冒充。旧记录无图表字段时整块不渲染。 */

/** 金额缩写：≥1 亿 →「12.3 亿」，≥1 万 →「4567 万」，否则原值；null →「—」。 */
export function formatMoneyShort(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
  return value.toFixed(0);
}

/** 涨跌语义色：与 KLineCard 同源（--mkt-up/--mkt-down 令牌，红涨绿跌随设置切换）。 */
export function readWbMarketColors(): { up: string; down: string } {
  const style = getComputedStyle(document.body);
  return {
    up: style.getPropertyValue("--mkt-up").trim() || "#e2726a",
    down: style.getPropertyValue("--mkt-down").trim() || "#5fb389",
  };
}

/** 价格量能图：前复权日线蜡烛 + 成交量副图 + MA5/20/60（MA 由服务端按全序列算好，与 candles 按下标对齐）。 */
export function ReportPriceChart({ chart }: { chart: PriceChartPack }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const rawCandles = chart.candles ?? [];
  const valid = rawCandles
    .map((bar, index) => ({ bar, index }))
    .filter(({ bar }) => bar.open != null && bar.high != null && bar.low != null && bar.close != null && bar.date);
  const market = readWbMarketColors();

  useEffect(() => {
    const host = hostRef.current;
    if (!host || valid.length === 0) return;
    const chartInstance = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#7b828b",
        fontSize: 10,
        fontFamily: "'JetBrains Mono', 'Noto Sans SC', monospace",
      },
      grid: {
        vertLines: { color: "rgba(231,236,233,.06)" },
        horzLines: { color: "rgba(231,236,233,.06)" },
      },
      rightPriceScale: { borderColor: "rgba(231,236,233,.14)" },
      timeScale: { borderColor: "rgba(231,236,233,.14)", timeVisible: false, rightOffset: 3 },
      crosshair: { mode: CrosshairMode.Normal },
    });
    const candle = chartInstance.addSeries(CandlestickSeries, {
      upColor: market.up,
      downColor: market.down,
      borderUpColor: market.up,
      borderDownColor: market.down,
      wickUpColor: market.up,
      wickDownColor: market.down,
    });
    candle.setData(
      valid.map(({ bar }) => ({ time: bar.date, open: bar.open!, high: bar.high!, low: bar.low!, close: bar.close! })),
    );
    const volume = chartInstance.addSeries(HistogramSeries, { priceScaleId: "", priceFormat: { type: "volume" } });
    chartInstance.priceScale("").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volume.setData(
      valid
        .filter(({ bar }) => bar.volume != null)
        .map(({ bar }) => ({
          time: bar.date,
          value: bar.volume!,
          color: (bar.close ?? 0) >= (bar.open ?? 0) ? `${market.up}73` : `${market.down}73`,
        })),
    );
    const maSeries: [ReturnType<typeof chartInstance.addSeries>, (number | null)[] | undefined][] = [
      [chartInstance.addSeries(LineSeries, { color: "#7dc4ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }), chart.ma5],
      [chartInstance.addSeries(LineSeries, { color: "#ffd27d", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }), chart.ma20],
      [chartInstance.addSeries(LineSeries, { color: "#b9e6ca", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }), chart.ma60],
    ];
    for (const [line, values] of maSeries) {
      if (!values) continue;
      line.setData(
        valid
          .map(({ index }, slot) => ({ time: valid[slot]!.bar.date, value: values[index] }))
          .filter((point): point is { time: string; value: number } => typeof point.value === "number"),
      );
    }
    chartInstance.timeScale().fitContent();
    return () => chartInstance.remove();
    // valid/market 由 props 派生（每次渲染重建），依赖它们等价于依赖 chart。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart]);

  if (valid.length === 0) {
    return <p className="report-meta">价格量能图数据不可用（本次 K 线取数失败或无有效日线）。</p>;
  }
  return (
    <div className="wb-pricechart">
      <p className="wb-chart-meta">
        {chart.provider || "行情"} · {chart.adjust || "前复权"} · {valid.length} 根日线
        {chart.candles?.length ? `（截至 ${chart.candles[chart.candles.length - 1]!.date}）` : ""}
        <span className="wb-ma-legend">
          {/* K01（2026-09-18 排版与研报呈现一致性路线图）：MA5/MA20/MA60 三条均线图例色是「区分
              不同周期均线」的标识色，不是涨跌语义色，故刻意不走 --mkt-up/--mkt-down、也不随涨跌
              开关变色——归入 K 组的均线豁免项（对照 tactics.css 已 token 化的涨跌色）。 */}
          <i style={{ background: "#7dc4ff" }} />MA5 <i style={{ background: "#ffd27d" }} />MA20 <i style={{ background: "#b9e6ca" }} />MA60
        </span>
      </p>
      <div className="lwc-host" ref={hostRef} />
    </div>
  );
}

/** 单季财务柱图（SVG）：正=盈利/流入（涨红），负=亏损/流出（跌绿）；value=null 的报告期保留空槽（断点不补零）。 */
export function QuarterlyBarChart({ title, points }: { title: string; points: QuarterlyPoint[] }) {
  const drawable = points.filter((point) => typeof point.value === "number") as (QuarterlyPoint & { value: number })[];
  if (drawable.length === 0) {
    return (
      <div className="wb-quarterly">
        <span className="wb-qbar-title">{title}</span>
        <p className="report-meta">无可绘制的单季数据（报告期断档或缺该行项目）。</p>
      </div>
    );
  }
  const market = readWbMarketColors();
  const W = 680;
  const H = 168;
  const PAD_L = 8;
  const PAD_B = 26;
  const PAD_T = 16;
  const slotW = (W - PAD_L) / points.length;
  const values = drawable.map((point) => point.value);
  const maxV = Math.max(0, ...values);
  const minV = Math.min(0, ...values);
  const span = maxV - minV || 1;
  const yOf = (v: number) => PAD_T + ((maxV - v) / span) * (H - PAD_T - PAD_B);
  const barW = Math.min(34, slotW * 0.62);
  const zeroY = yOf(0);
  return (
    <div className="wb-quarterly">
      <span className="wb-qbar-title">{title}</span>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} className="wb-qbar-svg">
        <line x1={PAD_L} y1={zeroY} x2={W} y2={zeroY} stroke="rgba(231,236,233,.2)" strokeDasharray="3 3" />
        {points.map((point, i) => {
          const x = PAD_L + i * slotW + (slotW - barW) / 2;
          const label = point.label.slice(2);
          if (typeof point.value !== "number") {
            return (
              <g key={point.label || i}>
                <line x1={x + barW / 2} y1={PAD_T + 6} x2={x + barW / 2} y2={zeroY} stroke="rgba(231,236,233,.18)" strokeDasharray="2 4" />
                <title>{`${point.label}：累计值无法拆为单季（紧邻报告期缺失，断点保留不补零）`}</title>
                <text x={x + barW / 2} y={H - 8} textAnchor="middle" fontSize="9" fill="#7b828b">{label}</text>
              </g>
            );
          }
          const y = yOf(point.value);
          const height = Math.max(1, Math.abs(zeroY - y));
          const color = point.value >= 0 ? market.up : market.down;
          return (
            <g key={point.label || i}>
              <rect x={x} y={Math.min(y, zeroY)} width={barW} height={height} fill={color} rx="2" opacity="0.88">
                <title>{`${point.label} 单季 ${formatMoneyShort(point.value)}（${point.is_derived ? "相邻报告期差分" : "披露原值"}）`}</title>
              </rect>
              {point.yoy != null && (
                <text x={x + barW / 2} y={Math.min(y, zeroY) - 4} textAnchor="middle" fontSize="9" fill={color}>
                  {point.yoy >= 0 ? "+" : ""}{(point.yoy * 100).toFixed(0)}%
                </text>
              )}
              <text x={x + barW / 2} y={H - 8} textAnchor="middle" fontSize="9" fill="#7b828b">{label}</text>
            </g>
          );
        })}
      </svg>
      <p className="report-meta wb-qbar-note">
        最大 {formatMoneyShort(maxV)} · 最小 {formatMoneyShort(minV)}；柱上百分比为单季同比（缺去年同季不标）。
      </p>
      {/* v33 A12：可访问的数值表——图表之外保留可核对数字；缺数据保留缺口不补零。 */}
      <details className="wb-chart-table">
        <summary>数值表（报告期 / 单季值 / 同比 / 口径）</summary>
        <table className="wb-judgement-table">
          <thead><tr><th>报告期</th><th className="num">单季值</th><th className="num">单季同比</th><th>口径</th></tr></thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.label || point.period_end}>
                <td>{point.label || point.period_end}</td>
                <td className="num">{point.value != null ? formatMoneyShort(point.value) : "—（累计值无法拆分，断点保留）"}</td>
                <td className="num">{point.yoy != null ? `${point.yoy >= 0 ? "+" : ""}${(point.yoy * 100).toFixed(0)}%` : "—"}</td>
                <td>{point.is_derived ? "相邻报告期差分" : "披露原值"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

/** 估值分位面板：当前值 + 5 年历史分位条 + 同业中位参考；分位缺失如实标「无历史序列」。 */
export const VALUATION_METRICS: { key: string; label: string }[] = [
  { key: "pe_ttm", label: "PE（TTM）" },
  { key: "pb_mrq", label: "PB（MRQ）" },
  { key: "ps_ttm", label: "PS（TTM）" },
];
export function ValuationPctPanel({ chart }: { chart: ValuationChartPack }) {
  const rows = VALUATION_METRICS.map((metric) => ({
    ...metric,
    current: chart[metric.key as "pe_ttm"] ?? null,
    pct: chart.percentiles?.[metric.key],
    median: chart.peer_median?.[metric.key],
  })).filter((row) => row.current != null || typeof row.pct === "number");
  if (rows.length === 0) return <p className="report-meta">估值分位数据不可用（取数失败或该标的无有效估值序列）。</p>;
  return (
    <div className="wb-valpct">
      {rows.map((row) => (
        <div key={row.key} className="wb-valpct-row">
          <span className="wb-valpct-name">{row.label}</span>
          <b>{row.current != null ? row.current.toFixed(2) : "—"}</b>
          {typeof row.pct === "number" ? (
            <span className="wb-valpct-bar" title={`近 ${chart.percentile_window_bars ?? "—"} 个交易日（自 ${chart.percentile_window_from || "—"}）的历史分位：${row.pct.toFixed(0)}%`}>
              <span className="wb-valpct-fill" style={{ width: `${Math.max(0, Math.min(100, row.pct))}%` }} />
            </span>
          ) : (
            <span className="wb-valpct-bar empty" title="无历史估值序列，无法计算分位（不编造）">无历史序列</span>
          )}
          {typeof row.pct === "number" && <span className="wb-valpct-num">{row.pct.toFixed(0)}% 分位</span>}
          <span className="wb-valpct-peer" title="同行业二级当日全部正估值个股的中位数（样本数见结论卡估值标签）">
            同业中位 {row.median != null ? row.median.toFixed(2) : "—"}
          </span>
        </div>
      ))}
      {chart.trade_date && <p className="report-meta wb-qbar-note">估值快照交易日 {chart.trade_date}；分位窗口 {chart.percentile_window_bars ?? "—"} 个交易日{chart.percentile_window_from ? `（自 ${chart.percentile_window_from}）` : ""}。</p>}
      {/* v33 A12：数值表——分位同时标历史窗口与样本条件；正负金额不解释成价格涨跌。 */}
      <details className="wb-chart-table">
        <summary>数值表（指标 / 当前值 / 历史分位 / 同业中位）</summary>
        <table className="wb-judgement-table">
          <thead><tr><th>指标</th><th className="num">当前值</th><th>历史分位</th><th className="num">同业中位</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{row.label}</td>
                <td className="num">{row.current != null ? row.current.toFixed(2) : "—"}</td>
                <td>{typeof row.pct === "number" ? `${row.pct.toFixed(0)}%（近 ${chart.percentile_window_bars ?? "—"} 个交易日）` : "无历史序列"}</td>
                <td className="num">{row.median != null ? row.median.toFixed(2) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

/** 研报图表数据包组合块：三组图并列；旧记录（字段不存在）不渲染，新记录空块如实占位。 */
export function ReportChartPack({ report }: { report: StockReport }) {
  const hasPrice = (report.price_chart?.candles?.length ?? 0) > 0;
  const quarterlyEntries = Object.entries(report.quarterly ?? {});
  const hasQuarterly = quarterlyEntries.some(([, points]) => points.length > 0);
  const hasValuation = report.valuation_chart != null
    && Object.values(report.valuation_chart.percentiles ?? {}).some((v) => typeof v === "number");
  // 旧记录：三个字段都不存在 → 整块不渲染（不假装有图）。
  if (report.price_chart === undefined && report.quarterly === undefined && report.valuation_chart === undefined) return null;
  if (!hasPrice && !hasQuarterly && !hasValuation) {
    return (
      <div className="wb-chartpack">
        <span className="answer-block-title">数据图表</span>
        <p className="report-meta">本次图表数据包不可用（相关来源取数失败），报告文字结论不受影响。</p>
      </div>
    );
  }
  const quarterlyLabels: Record<string, string> = {
    revenue: "单季营业收入（万元/亿元为缩写，原值单位为元）",
    net_profit: "单季归母净利润（负值=亏损）",
    operating_cash_flow: "单季经营活动现金流净额（负值=净流出）",
  };
  return (
    <div className="wb-chartpack">
      <span className="answer-block-title">数据图表（由本次证据包直接生成，与报告引用同源）</span>
      {hasPrice && <ReportPriceChart chart={report.price_chart!} />}
      {hasQuarterly && quarterlyEntries.map(([key, points]) => (
        <QuarterlyBarChart key={key} title={quarterlyLabels[key] ?? key} points={points} />
      ))}
      {hasValuation && <ValuationPctPanel chart={report.valuation_chart!} />}
    </div>
  );
}

export function ScenarioFalsifiability({ report }: { report: StockReport }) {
  const scenarios = report.scenarios ?? [];
  if (scenarios.length === 0) return null;
  const hasExtra = scenarios.some((item) => item.invalidates || item.horizon || item.expected_range?.low != null || item.expected_range?.high != null);
  if (!hasExtra) {
    return (
      <p className="wb-scenario-note amber">
        本报告的情景未给出「失效条件」与「有效窗口」——按 v27 口径，这样的情景尚不可被证伪，只能作为方向参考。
      </p>
    );
  }
  /* v30 区间五态（服务端确定性推导）：禁止单一「—」糊弄——每态都有明确的语义与后续动作。 */
  const rangeCell = (item: ReportScenario): ReactNode => {
    const status = item.range_status;
    const basis = item.range_basis ? `依据：${item.range_basis}` : undefined;
    if (status === "estimated") {
      const low = formatPrice(item.expected_range?.low ?? null);
      const high = formatPrice(item.expected_range?.high ?? null);
      const single = low === "—" || high === "—";
      return (
        <span title={item.range_status_note || basis}>
          {single ? (low !== "—" ? `≥ ${low}` : `≤ ${high}`) : `${low} ~ ${high}`}
          {item.range_basis && <small className="wb-range-basis"> {item.range_basis}</small>}
        </span>
      );
    }
    if (status === "technical_range") {
      const low = formatPrice(item.expected_range?.low ?? null);
      const high = formatPrice(item.expected_range?.high ?? null);
      const single = low !== "—" ? `≥ ${low}` : `≤ ${high}`;
      return (
        <span title={item.range_status_note || basis}>
          {single}
          <small className="wb-range-basis"> 仅单端（{item.range_basis || "技术位"}）</small>
        </span>
      );
    }
    if (status === "not_applicable") {
      return <span className="wb-range-event" title={item.range_status_note || basis}>待事件验证</span>;
    }
    if (status === "invalid") {
      return <span className="coral" title={item.range_status_note || "区间倒挂：下限高于上限，属模型输出错误"}>区间倒挂 · 不可用</span>;
    }
    return <span className="wb-range-none" title={item.range_status_note || "模型未给出区间，也未说明依据"}>无区间</span>;
  };
  // R04：屏显沿用与导出**同一**同值判定（format.scenarioProbabilityDisplay），三档同值一律标「未给概率」。
  const probability = scenarioProbabilityDisplay(scenarios);
  // Q08（2026-09-19 路线图）：列名不再写「概率」——probability_basis 恒为 none（无后验基准），
  // 读者会把它当上涨概率；未校准的取值只能叫「倾向（未统计校准）」。导出用同一函数。
  const probabilityWording = scenarioProbabilityWording(undefined, scenarios);
  return (
    <div className="wb-scenario">
      <span className="answer-block-title">情景的失效条件与有效窗口（可证伪口径）</span>
      <table className="wb-judgement-table">
        <thead>
          <tr>
            <th>情景</th>
            <th title={probabilityWording.note}>
              {probabilityWording.column}
            </th>
            <th>预期区间</th>
            <th>有效窗口</th>
            <th title="什么条件下该情景作废——没有这一列，情景只是态度声明">失效条件</th>
          </tr>
        </thead>
        <tbody>
          {scenarios.map((item, i) => (
            <tr key={`sf-${i}`}>
              <td>{item.name || "—"}</td>
              <td title={item.probability_note || probabilityWording.note}>{probability.displayOf(item)}</td>
              <td>{rangeCell(item)}</td>
              <td>{item.horizon || "—"}</td>
              <td>{item.invalidates || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="wb-scenario-note">
        概率口径：{probabilityWording.note}
        {probability.uniform && " 三档概率同值，按「未给概率」处理——不作排序或建仓依据（与导出同口径）。"}
      </p>
    </div>
  );
}

/**
 * v27 待复盘验证点队列（方案 §3.5）；v33 A15（2026-09-14 路线图）重构为**操作列表**：
 * 状态筛选（全部/已到期/待到期/事件触发）+ 代码/关键词检索 + 分页（默认 10 条，可 20）；
 * 首行只显示 标的/信号/状态/到期信息，情景与失效条件展开查看；可跳回来源报告。
 *
 * G01（桌面端升级路线图 2026-09-18）：本面板**不再只读**。服务端在读路径上把带确切日期的验证点
 * 幂等落成 `verifiable_judgments`（`judgment_id` 由 report_id + 序号确定性派生），面板据此提供
 * 「回填结论」表单（成立/失效/无数据 + 说明），走既有 `POST /judgments/{id}/verify`——只**追加**
 * 验证结果并推状态，**原研报 payload 一字不改**。锚定事件的验证点没有日期，不落库也不给回填入口，
 * 如实显示原因。「已到期」仍然不自动等于「已验证」。
 */
export const REVIEW_BUCKET_LABEL: Record<string, { text: string; tone: string; hint: string }> = {
  overdue: { text: "已到期", tone: "coral", hint: "验证时点已到，等待人工回填结论（不自动等于已验证）" },
  scheduled: { text: "待到期", tone: "blue", hint: "验证时点未到，按剩余天数排序" },
  event_anchored: { text: "锚定事件", tone: "gray", hint: "验证时点由事件触发（如「三季报披露后」），无确切日期，不猜日期" },
};

/** v39 Q21：复盘五态色调（与后端 `TRACKING_LABELS` 同词表，措辞由服务端下发）。 */
export const REVIEW_TRACKING_TONE: Record<string, string> = {
  suggest_observe: "gray",
  tracked: "blue",
  pending_verify: "warn",
  verified: "mint",
  unverifiable: "coral",
};
export const REVIEW_TRACKING_HINT
  = "复盘状态：只有已落成可回填判断的条目才算「已加入跟踪」；到期不会自动监控，结果由人工回填。";

/** G01：判断状态 → 展示文案与色调（后端 `JudgmentStatus`：pending/verified/refuted/insufficient_data）。 */
export const REVIEW_STATUS_LABEL: Record<string, { text: string; tone: string }> = {
  pending: { text: "待回填", tone: "blue" },
  verified: { text: "成立", tone: "mint" },
  refuted: { text: "失效", tone: "coral" },
  insufficient_data: { text: "无数据", tone: "gray" },
};

/** G01：回填三态（与后端 `JudgmentVerifyRequest.result` 的 pattern 逐字一致）。 */
const REFILL_RESULTS: Array<{ value: "verified" | "refuted" | "insufficient_data"; text: string }> = [
  { value: "verified", text: "成立" },
  { value: "refuted", text: "失效" },
  { value: "insufficient_data", text: "无数据" },
];

const REVIEW_PAGE_SIZES = [10, 20] as const;

export function ReviewQueuePanel({ queue, onOpenReport, onRefill }: {
  queue: ReviewQueue | null;
  /** 跳回来源报告（A15：可定位到原报告；实现由调用方注入）。 */
  onOpenReport?: (reportId: string) => void;
  /** G01：回填判断结论（成立/失效/无数据）。返回错误文案，null=成功；实现由调用方注入（复用 verify 通道）。 */
  onRefill?: (judgmentId: string, result: "verified" | "refuted" | "insufficient_data", outcome: string) => Promise<string | null>;
}) {
  const [bucket, setBucket] = useState<"all" | "overdue" | "scheduled" | "event_anchored">("all");
  const [query, setQuery] = useState("");
  // G01：回填表单状态。同时在展开多条时也只允许一条在填（避免误提交到另一条判断）。
  const [refillFor, setRefillFor] = useState<string | null>(null);
  const [refillResult, setRefillResult] = useState<"verified" | "refuted" | "insufficient_data">("verified");
  const [refillOutcome, setRefillOutcome] = useState("");
  const [refillBusy, setRefillBusy] = useState(false);
  const [refillError, setRefillError] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState<(typeof REVIEW_PAGE_SIZES)[number]>(10);
  const [page, setPage] = useState(1);
  if (!queue) return null;

  async function submitRefill(judgmentId: string) {
    if (!onRefill || refillBusy) return;
    setRefillBusy(true);
    setRefillError(null);
    const error = await onRefill(judgmentId, refillResult, refillOutcome);
    setRefillBusy(false);
    if (error) {
      setRefillError(error);
      return;
    }
    // 成功：收起表单（状态由服务端返回后随队列刷新带回来，不在前端猜测）。
    setRefillFor(null);
    setRefillOutcome("");
  }

  const buckets: Array<keyof typeof queue.counts> = ["overdue", "scheduled", "event_anchored"];
  const items = queue.items.filter((item) => {
    if (bucket !== "all" && (item.bucket ?? "event_anchored") !== bucket) return false;
    const keyword = query.trim().toLowerCase();
    if (!keyword) return true;
    const haystack = `${item.symbol ?? ""} ${item.title ?? ""} ${item.signal ?? ""}`.toLowerCase();
    return haystack.includes(keyword);
  });
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.min(page, pageCount);
  const pageItems = items.slice((safePage - 1) * pageSize, safePage * pageSize);
  return (
    <div className="wb-review">
      <div className="wb-review-head">
        <span className="answer-block-title">验证点待复盘队列（{queue.item_count} 条）</span>
        {buckets.map((key) => (
          <button
            key={key}
            className={`tag-button ${bucket === key ? "active" : ""}`}
            title={REVIEW_BUCKET_LABEL[key]!.hint}
            onClick={() => { setBucket(key); setPage(1); }}
          >
            {REVIEW_BUCKET_LABEL[key]!.text} {queue.counts[key] ?? 0}
          </button>
        ))}
        <button className={`tag-button ${bucket === "all" ? "active" : ""}`} onClick={() => { setBucket("all"); setPage(1); }}>全部</button>
        {queue.closure && queue.closure.filled > 0 && (
          <span className="soft-tag mint" title="已回填结论的判定数（只含已落库的判定；锚定事件点没有日期、不落库，不计入）">
            已回填 {queue.closure.filled}
          </span>
        )}
        {queue.closure && queue.closure.overdue_unfilled > 0 && (
          <span className="soft-tag coral" title="已到期但尚未回填结论的判定数——「已到期」不自动等于「已验证」">
            到期未回填 {queue.closure.overdue_unfilled}
          </span>
        )}
        <input
          className="wb-review-search"
          placeholder="搜索代码 / 关键词"
          aria-label="检索验证点（代码或关键词）"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setPage(1); }}
        />
      </div>
      <div className="wb-review-toolbar">
        <span className="report-meta">
          {items.length} 条命中 · 已到期不自动等于已验证
          {queue.closure
            ? ` · 已落库判定 ${queue.closure.materialized}（已回填 ${queue.closure.filled} / 到期未回填 ${queue.closure.overdue_unfilled}${queue.closure.not_materialized > 0 ? ` / 锚定事件未落库 ${queue.closure.not_materialized}` : ""}）`
            : ""}
        </span>
        <div className="wb-review-pager">
          <label className="report-meta" htmlFor="wb-review-pagesize">每页</label>
          <select
            id="wb-review-pagesize"
            value={pageSize}
            onChange={(event) => { setPageSize(Number(event.target.value) as 10 | 20); setPage(1); }}
          >
            {REVIEW_PAGE_SIZES.map((size) => <option key={size} value={size}>{size} 条</option>)}
          </select>
          <button className="tag-button" disabled={safePage <= 1} onClick={() => setPage(safePage - 1)}>上一页</button>
          <span className="report-meta">第 {safePage} / {pageCount} 页</span>
          <button className="tag-button" disabled={safePage >= pageCount} onClick={() => setPage(safePage + 1)}>下一页</button>
        </div>
      </div>
      {pageItems.length === 0 ? (
        <p className="wb-history-empty">
          {queue.items.length === 0
            ? "暂无验证点。生成研报后，模型给出的验证时点会自动进入本队列。"
            : "没有命中筛选条件的验证点，请调整筛选或关键词。"}
        </p>
      ) : (
        <ul className="wb-review-list">
          {queue.tracking?.summary && (
            <li className="wb-review-tracking-summary">
              {queue.tracking.summary}
              <span className="report-meta"> · {queue.tracking.note || REVIEW_TRACKING_HINT}</span>
            </li>
          )}
          {pageItems.map((item, index) => {
            const meta = REVIEW_BUCKET_LABEL[item.bucket ?? "event_anchored"] ?? REVIEW_BUCKET_LABEL.event_anchored!;
            const judgmentId = item.judgment_id || null;
            const statusMeta = judgmentId
              ? (REVIEW_STATUS_LABEL[item.status ?? "pending"] ?? REVIEW_STATUS_LABEL.pending!)
              : null;
            const refillOpen = judgmentId !== null && refillFor === judgmentId;
            return (
              <li key={`${item.report_id ?? "r"}-${index}`}>
                <div className="wb-review-line">
                  <span className={`soft-tag ${meta.tone}`} title={meta.hint}>{meta.text}</span>
                  {item.tracking_label && (
                    /* v39 Q21：复盘状态与「是否真落成了跟踪」分开展示——没有 judgment_id 的条目只会是「建议观察」。 */
                    <span
                      className={`soft-tag ${REVIEW_TRACKING_TONE[item.tracking_state ?? ""] ?? "gray"}`}
                      title={item.tracking_note || REVIEW_TRACKING_HINT}
                    >
                      跟踪：{item.tracking_label}
                    </span>
                  )}
                  {statusMeta && (
                    <span className={`soft-tag ${statusMeta.tone}`} title="判定状态：由回填结果推进（pending=待回填）；判断原文与研报 payload 均不改写">
                      {statusMeta.text}
                    </span>
                  )}
                  <b>{item.symbol || "—"}</b>
                  <span className="wb-review-signal">{item.signal || "—"}</span>
                  <span className="wb-review-due">
                    {item.due_on
                      ? `${item.due_on}${typeof item.days_until === "number" ? (item.days_until <= 0 ? `（已过 ${Math.abs(item.days_until)} 天）` : `（还有 ${item.days_until} 天）`) : ""}`
                      : item.verify_by || "—"}
                  </span>
                  {item.report_id && onOpenReport && (
                    <button className="wb-review-open" title="跳回来源报告（恢复该报告的阅读视图）" onClick={() => onOpenReport(item.report_id!)}>
                      打开来源报告
                    </button>
                  )}
                  {judgmentId && onRefill && (
                    <button
                      className="wb-review-open"
                      title="回填结论：成立 / 失效 / 无数据。只追加验证结果并推状态，判断原文与研报原文不变。"
                      onClick={() => { setRefillFor(refillOpen ? null : judgmentId); setRefillError(null); }}
                    >
                      {refillOpen ? "收起回填" : statusMeta?.text === "待回填" ? "回填结论" : "重新回填"}
                    </button>
                  )}
                </div>
                {item.expected_if_true && <small>若成立则预期：{item.expected_if_true}</small>}
                {item.verification && (
                  <small>
                    最近回填：
                    {REVIEW_STATUS_LABEL[item.verification.result ?? ""]?.text ?? item.verification.result ?? "—"}
                    {item.verification.outcome ? ` · ${item.verification.outcome}` : ""}
                    {item.verification.checked_at ? `（${item.verification.checked_at.slice(0, 10)}）` : ""}
                  </small>
                )}
                {!judgmentId && item.materialization_note && (
                  <small className="wb-review-refill-note">{item.materialization_note}</small>
                )}
                {refillOpen && judgmentId && (
                  <div className="wb-review-refill">
                    <div className="wb-review-refill-row">
                      {REFILL_RESULTS.map((option) => (
                        <button
                          key={option.value}
                          className={`tag-button ${refillResult === option.value ? "active" : ""}`}
                          aria-pressed={refillResult === option.value}
                          onClick={() => setRefillResult(option.value)}
                        >
                          {option.text}
                        </button>
                      ))}
                      <input
                        className="wb-review-refill-outcome"
                        placeholder="实际结果说明（可选）"
                        aria-label="回填说明"
                        value={refillOutcome}
                        onChange={(event) => setRefillOutcome(event.target.value)}
                      />
                      <button className="wb-review-open" disabled={refillBusy} onClick={() => void submitRefill(judgmentId)}>
                        {refillBusy ? "提交中…" : "提交回填"}
                      </button>
                      <button className="wb-review-open" disabled={refillBusy} onClick={() => { setRefillFor(null); setRefillError(null); }}>取消</button>
                    </div>
                    {refillError && <small className="wb-review-refill-error">回填失败：{refillError}</small>}
                    <small className="wb-review-refill-note">
                      回填只追加验证结果并推状态；判断原文与研报 payload 均不变（append-only，与报告质量既有约定一致）。
                    </small>
                  </div>
                )}
                {item.scenarios && item.scenarios.length > 0 && (
                  <details className="wb-review-scenarios-details">
                    <summary>情景与失效条件（{item.scenarios.length}）</summary>
                    <small className="wb-review-scenarios">
                      {item.scenarios
                        .map((sc) => `${sc.name || "情景"}${sc.invalidates ? `（失效：${sc.invalidates}）` : ""}`)
                        .join(" · ")}
                    </small>
                  </details>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {queue.note && <p className="report-meta">{queue.note}</p>}
    </div>
  );
}

/** 结构化判断：情景 / 关键价位 / 验证点。模型未输出时整块不渲染（不编造填空）。 */
export function StructuredJudgement({ report }: { report: StockReport }) {
  const scenarios = report.scenarios ?? [];
  const support = report.levels?.support ?? [];
  const resistance = report.levels?.resistance ?? [];
  const watchpoints = report.watchpoints ?? [];
  if (scenarios.length === 0 && support.length === 0 && resistance.length === 0 && watchpoints.length === 0) return null;
  const price = (value: number | null): string => formatPrice(value);
  return (
    <div className="wb-judgement">
      <span className="answer-block-title">结构化判断（可机读，供复盘与后验评估）</span>
      {scenarios.length > 0 && (
        <table className="wb-judgement-table">
          <thead><tr><th>情景</th><th>触发条件</th><th>依据来源</th></tr></thead>
          <tbody>
            {scenarios.map((item, i) => (
              <tr key={`sc-${i}`}><td>{item.name || "—"}</td><td>{item.trigger || "—"}</td><td>{item.source || "—"}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {(support.length > 0 || resistance.length > 0) && (
        <table className="wb-judgement-table">
          <thead><tr><th>类型</th><th>价位</th><th>依据</th></tr></thead>
          <tbody>
            {support.map((item, i) => (
              <tr key={`sp-${i}`}><td>支撑</td><td>{price(item.price)}</td><td>{item.basis || "—"}</td></tr>
            ))}
            {resistance.map((item, i) => (
              <tr key={`rs-${i}`}><td>压力</td><td>{price(item.price)}</td><td>{item.basis || "—"}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {watchpoints.length > 0 && (
        <WatchpointTable points={watchpoints} />
      )}
    </div>
  );
}

/**
 * JV06：验证点表——按服务端给的 `rank` 排序，**「暂缓」档折叠但条目不删除**。
 *
 * 排序与分组只此一份（`format.watchpointTriageView`）：屏显、追问附录与导出都走它，
 * 否则「优先级排序」会在三处各长一份并漂移成三种说法。
 *
 * 未分诊时（`triage` 全为 null）保持报告原序、无优先级列、无折叠——
 * 没评过的东西没有理由被挪动，更没有理由被藏起来。
 */
export function WatchpointTable({
  points,
  withDateBasis = false,
  renderSignal,
  renderDateBasis,
}: {
  points: ReportWatchpoint[];
  withDateBasis?: boolean;
  renderSignal?: (item: ReportWatchpoint) => ReactNode;
  renderDateBasis?: (item: ReportWatchpoint) => ReactNode;
}) {
  const { main, deferred, triaged } = watchpointTriageView(points);
  const head = (
    <tr>
      <th>观察信号</th>
      <th>验证时点</th>
      {withDateBasis && <th title="日期从哪来：交易日历推算 / 已披露日程 / 输入材料">日期依据</th>}
      <th>若成立则预期</th>
      {triaged && <th title="值不值得现在花一次取数 + 研报额度去查（展示分为归一化值）">优先级</th>}
    </tr>
  );
  const rows = (items: ReportWatchpoint[]) =>
    items.map((item, i) => {
      const priority = watchpointPriorityView(item);
      return (
        <tr key={`wp-${i}`}>
          <td>{renderSignal ? renderSignal(item) : item.signal || "—"}</td>
          <td>{item.verify_by || "—"}</td>
          {withDateBasis && (
            <td>
              {renderDateBasis
                ? renderDateBasis(item)
                : watchpointDateView(item).basis}
            </td>
          )}
          <td>{item.expected_if_true || "—"}</td>
          {triaged && (
            <td>
              {priority ? (
                <span className={`soft-tag ${priority.tone}`} title={priority.title}>
                  {priority.label} · {priority.percent}
                </span>
              ) : null}
            </td>
          )}
        </tr>
      );
    });
  return (
    <div className="wb-watchpoints">
      {triaged && (
        <span className="wb-counter-label">
          已按优先级分诊排序（是否可证伪 / 是否已有证据覆盖 / 是否影响决策）；「暂缓」档已折叠，**未删除**。
        </span>
      )}
      <table className="wb-judgement-table">
        <thead>{head}</thead>
        <tbody>{rows(main)}</tbody>
      </table>
      {deferred.length > 0 && (
        <details className="wb-watchpoints-deferred">
          <summary>暂缓 · {deferred.length}（展开可逐条查看，未删除）</summary>
          <table className="wb-judgement-table">
            <thead>{head}</thead>
            <tbody>{rows(deferred)}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}

/**
 * v24 档 A/B/C：估值卡（市盈率一类）。
 * v32（2026-09-13 方案 §二.3）扩展：正常化盈利、三情景估值（bear/base/bull）、
 * 安全边际与四态估值状态。旧记录没有新字段时对应区块不渲染（不编造口径）。
 * v39 Q05（2026-09-19 路线图）：估值口径改走三态（指标缺失 / 指标已取得但合理价值未评估 / 已评估），
 * 已有 PE/PB 的样例不再被一个「无数据」chip 打发——「没做过正常化验证」与「没取到数据」是两件事。
 * 模型未输出估值块、或估值来源（S5）取数失败时整块不渲染——绝不用占位数字冒充估值。
 */
export function ValuationCard({ report }: { report: StockReport }) {
  const valuation = report.valuation;
  if (!valuation) return null;
  // 三态判定与结论词只有一份来源（format.valuationEvidenceDisplay）：屏显与导出不得两套口径。
  const evidence = valuationEvidenceDisplay(valuation);
  const verdict = (valuation.verdict || "").trim();
  // Q05：结论词缺失/占位时，用三态结论词替下笼统的「无数据」（指标明明在，只是没做正常化验证）。
  const maskedVerdict = verdict === "" || verdict === "无数据" || verdict === "无法判断";
  const displayVerdict = maskedVerdict ? evidence.verdictDisplay : verdict;
  const rows = ([
    ["PE(TTM)", valuation.pe_ttm],
    ["历史分位", valuation.pe_percentile],
    ["同业位置", valuation.peer_position],
    ["判断依据", valuation.basis],
  ] as [string, string][]).filter(([, value]) => Boolean(value && value.trim()));
  if (rows.length === 0 && !verdict) return null;
  const tone = displayVerdict.includes("贵") ? "coral"
    : displayVerdict.includes("便宜") ? "mint"
    : displayVerdict === "合理" ? "blue" : "gray";
  const statusMeta = valuation.valuation_status ? VALUATION_STATUS_META[valuation.valuation_status] : undefined;
  const earnings = valuation.normalized_earnings;
  // J02：三列全空的情景行不进表（与导出同一判定，屏显与导出不得两套口径）。
  const casesAll = valuation.valuation_cases ?? [];
  const cases = casesAll.filter((item) => item.name && (item.earnings != null || item.multiple != null || item.fair_value != null));
  const casesEmptiedOut = cases.length === 0 && casesAll.some((item) => item.name);
  const margin = valuation.margin_of_safety;
  const statusNote = valuation.valuation_status_note || statusMeta?.hint;
  return (
    <div className="wb-valuation">
      <div className="wb-valuation-head">
        <span className="answer-block-title">估值判断（市盈率一类）</span>
        {displayVerdict && (
          <span className="soft-tag-verdict">
            <span
              className={`soft-tag ${tone}`}
              title={maskedVerdict
                ? `${evidence.label}——三态口径（指标缺失 / 合理价值未评估 / 已评估），不是买卖建议`
                : valuation.verdict_note
                  ? `${valuation.verdict_note}（模型原结论词：${valuation.verdict_raw}）`
                  : "模型基于估值证据（S5）给出的贵贱判断，非买卖建议"}
            >{displayVerdict}</span>
          </span>
        )}
        {statusMeta && !maskedVerdict && (
          <span
            className={`soft-tag ${statusMeta.tone}`}
            title={statusNote || statusMeta.hint}
          >
            正常化口径 · {statusMeta.label}
          </span>
        )}
        {/* Q05：三态标签常驻——它回答的是「这条估值结论做到哪一步」，与贵贱判断不是一个轴。 */}
        <span className={`soft-tag ${evidence.tone}`} title={evidence.note || evidence.label}>
          {evidence.label}
        </span>
      </div>
      {maskedVerdict && evidence.note && (
        <p className="report-meta amber" role="note">{evidence.note}</p>
      )}
      {/* 结论词被服务端归一时原因必须留下（与导出同一行）——换了措辞就不能把「为什么换」丢掉。 */}
      {maskedVerdict && valuation.verdict_note && (
        <p className="report-meta">
          结论词归一说明：{valuation.verdict_note}
          {valuation.verdict_raw ? `（模型原结论词：${valuation.verdict_raw}）` : ""}
        </p>
      )}
      {valuation.valuation_status_note && (
        <p className="report-meta amber" role="note">{valuation.valuation_status_note}</p>
      )}
      {rows.length > 0 && (
        <table className="wb-judgement-table">
          <tbody>
            {rows.map(([label, value], i) => (
              <tr key={`valuation-${i}`}><td className="wb-valuation-label">{label}</td><td>{value}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {earnings && (typeof earnings.value === "number" || earnings.basis) && (
        <div className="wb-val-normalized">
          <span className="wb-counter-label">正常化盈利（剔除一次性/周期因素后的可持续口径）</span>
          <p>
            {typeof earnings.value === "number"
              ? <><b>{earnings.value}</b> 亿/年{earnings.confidence && <span className="soft-tag gray"> 置信 {earnings.confidence}</span>}</>
              : <span className="soft-tag warn" title="未完成正常化验证：低 PE 分位不能排除利润周期高点、一次性收益或现金流较弱">未完成正常化验证</span>}
            {earnings.basis && <span className="report-meta"> 口径：{earnings.basis}</span>}
          </p>
        </div>
      )}
      {cases.length > 0 && (
        <table className="wb-judgement-table" title="三情景估值：正常化盈利 × 适用倍数 → 隐含公允价值（方案 §二.3）；推不出的字段如实留空">
          <thead><tr><th>情景</th><th>年化盈利（亿）</th><th>适用倍数</th><th>隐含公允价值（元）</th></tr></thead>
          <tbody>
            {cases.map((item, i) => (
              <tr key={`case-${i}`}>
                <td>{item.name_label || item.name}</td>
                <td>{item.earnings ?? "—"}</td>
                <td>{item.multiple ?? "—"}</td>
                <td>{item.fair_value ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {casesEmptiedOut && (
        <p className="report-meta" role="note">
          未做三情景估值：正常化盈利未经验证{earnings?.basis ? `（${earnings.basis}）` : ""}，情景表不占版面。
        </p>
      )}
      {typeof margin === "number" && Number.isFinite(margin) && (
        <p className="wb-val-margin" title="安全边际 = 1 − 当前价/基准公允价值；负值表示现价高于公允价值（方案 §二.3）">
          安全边际：<b className={margin >= 0 ? "mint" : "coral"}>{(margin * 100).toFixed(1)}%</b>
          <span className="report-meta">{margin >= 0 ? "（现价低于基准公允价值）" : "（现价高于基准公允价值）"}</span>
        </p>
      )}
      {(valuation.valuation_limitations?.length ?? 0) > 0 && (
        <ul className="answer-list wb-val-limits">
          {valuation.valuation_limitations!.map((item, i) => <li key={i}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}

/** 行内渲染：**加粗** 与 [S1] 引用标签；纯 React 元素，不走 innerHTML。
 *  v31 P1：`highlights` 按「已定位的关键数字」给正文数字加信息状态色（蓝/琥珀/珊瑚红），
 *  每个文本段最多命中 2 处（§八.5：每段最多突出 1～2 处，正文黑白层次保持主导）；
 *  颜色仅是辅助编码——同时有 title 文字标签 + aria-label，加粗保证灰度打印仍可识别。 */
export function renderInline(
  text: string,
  keyPrefix: string,
  highlights: ReportHighlight[] = [],
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let seq = 0;
  /** 段内高亮预算：每段最多 2 处（§八.5）。 */
  let hlBudget = 2;
  // 高亮按词长降序匹配，避免「19.7」先于「19.78」吃掉匹配位置。
  const sortedHighlights = [...highlights].sort((a, b) => b.text.length - a.text.length);
  /** 把一段纯文本按 highlights 切片（高亮之外保持原样）。 */
  const renderPlain = (plain: string, keyBase: string): ReactNode[] => {
    if (hlBudget <= 0 || sortedHighlights.length === 0 || !plain) return [plain];
    const out: ReactNode[] = [];
    let rest = plain;
    let cursor = 0;
    while (rest && hlBudget > 0) {
      let bestIndex = -1;
      let best: ReportHighlight | null = null;
      for (const highlight of sortedHighlights) {
        const found = rest.indexOf(highlight.text);
        if (found >= 0 && (bestIndex < 0 || found < bestIndex)) {
          bestIndex = found;
          best = highlight;
        }
      }
      if (!best || bestIndex < 0) break;
      if (bestIndex > 0) out.push(rest.slice(0, bestIndex));
      out.push(
        <mark
          key={`${keyBase}-hl${cursor++}`}
          className={`rpt-hl rpt-hl-${best.tone}`}
          title={best.title}
          aria-label={best.title}
        >
          {best.text}
        </mark>,
      );
      hlBudget -= 1;
      rest = rest.slice(bestIndex + best.text.length);
    }
    if (rest) out.push(rest);
    return out;
  };
  // 先按 **bold** 切，再在每段内切 [S#] 引用
  const boldParts = text.split(/(\*\*[^*]+\*\*)/g);
  boldParts.forEach((part, pi) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) {
      nodes.push(<b key={`${keyPrefix}-b${pi}`}>{part.slice(2, -2)}</b>);
      return;
    }
    const citeParts = part.split(/(\[S\d+\])/g);
    citeParts.forEach((cp, ci) => {
      if (/^\[S\d+\]$/.test(cp)) {
        nodes.push(<span key={`${keyPrefix}-${pi}-${ci}`} className="wb-md-cite">{cp}</span>);
      } else if (cp) {
        nodes.push(<Fragment key={`${keyPrefix}-${pi}-${ci}`}>{renderPlain(cp, `${keyPrefix}-${pi}-${ci}`)}</Fragment>);
      }
      seq += 1;
    });
  });
  return nodes.length > 0 ? nodes : [text];
}

/**
 * 轻量 markdown 渲染（研报/研判正文）：支持 ### 小节、- 无序列表、1. 有序列表、
 * | 表格 |（需带 |---| 分隔行）、**加粗**、[S#] 引用标签。
 * 模型输出只约定了这几种形态，不做通用 markdown 引擎（零依赖、无 XSS 面）。
 */
/** §4.2 长段落折叠阈值（去空白字符数）：超过则默认折叠，可展开；数字与关键短语颜色不受折叠影响。 */
const PARAGRAPH_COLLAPSE_CHARS = 240;
/** 折叠态显示的行数（CSS line-clamp 同步调整）。 */
const PARAGRAPH_COLLAPSE_LINES = 4;

/** §4.2 长段落折叠：默认折叠（line-clamp），展开/收起可切换；短段落原样渲染（零开销）。 */
function CollapsibleParagraph({ line, highlights, keyId }: { line: string; highlights: ReportHighlight[]; keyId: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = line.replace(/\s+/g, "").length > PARAGRAPH_COLLAPSE_CHARS;
  if (!isLong) {
    return <p className="wb-md-p">{renderInline(line, keyId, highlights)}</p>;
  }
  return (
    <p className={`wb-md-p wb-md-collapse ${expanded ? "is-expanded" : ""}`}>
      <span className={expanded ? "" : "wb-md-clamp"}>{renderInline(line, keyId, highlights)}</span>
      <button
        type="button"
        className="wb-md-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        {expanded ? "收起" : `展开全文（${line.replace(/\s+/g, "").length} 字）`}
      </button>
    </p>
  );
}

/* C（frontend-optimization-roadmap）：memo 化——长报告正文解析较重，列表筛选/状态切换时同 content 不再重解析。
 * v31 §4.2：标题命中四大必需小节时写入锚点 id（供目录跳转）；highlights 见 renderInline；
 * 长段落折叠见 CollapsibleParagraph（折叠态下数字与关键短语颜色保留）。 */
export const MarkdownishText = memo(function MarkdownishText(
  { content, highlights = [] }: { content: string; highlights?: ReportHighlight[] },
) {
  const blocks: ReactNode[] = [];
  const lines = content.split("\n").map((line) => line.trimEnd());
  let listItems: string[] = [];
  let listTag: "ul" | "ol" | null = null;
  let seq = 0;
  /** 已写入的锚点 id（同名小节只取第一个，避免重复 id）。 */
  const usedAnchors = new Set<string>();

  function flushList(): void {
    if (!listTag || listItems.length === 0) {
      listItems = [];
      listTag = null;
      return;
    }
    const tag = listTag;
    const items = listItems;
    listItems = [];
    listTag = null;
    const key = `${tag}-${seq++}`;
    blocks.push(
      tag === "ul"
        ? <ul key={key} className="wb-md-ul">{items.map((item, i) => <li key={i}>{renderInline(item, `${key}-${i}`, highlights)}</li>)}</ul>
        : <ol key={key} className="wb-md-ol">{items.map((item, i) => <li key={i}>{renderInline(item, `${key}-${i}`, highlights)}</li>)}</ol>,
    );
  }

  /** 表格行 → 单元格数组（去掉首尾竖线）。 */
  const cells = (line: string): string[] =>
    line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  /** 分隔行判定：只由 | - : 空白组成且至少含一个 -（如 |---|:--:|）。 */
  const isSeparator = (line: string): boolean => {
    const text = line.trim();
    return text.includes("|") && text.includes("-") && /^[\s|:-]+$/.test(text);
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] ?? "";
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      flushList();
      const headingText = heading[1] ?? "";
      const sectionName = REQUIRED_SECTION_NAMES.find((name) => headingText.includes(name) && !usedAnchors.has(name));
      if (sectionName) usedAnchors.add(sectionName);
      blocks.push(
        <div
          key={`h-${seq++}`}
          id={sectionName ? `wb-sec-${sectionName}` : undefined}
          className={`wb-md-h${sectionName ? " wb-md-h-sec" : ""}`}
        >
          {renderInline(headingText, `h-${seq}`, highlights)}
        </div>,
      );
      continue;
    }
    // 表格：本行以 | 起始，且下一行是分隔行 → 吃到连续 | 行结束
    if (line.trim().startsWith("|") && isSeparator(lines[i + 1] ?? "")) {
      flushList();
      const header = cells(line);
      const rows: string[][] = [];
      let j = i + 2;
      while (j < lines.length && (lines[j] ?? "").trim().startsWith("|")) {
        rows.push(cells(lines[j] ?? ""));
        j += 1;
      }
      i = j - 1;
      const key = `t-${seq++}`;
      blocks.push(
        <table key={key} className="wb-md-table">
          <thead>
            <tr>{header.map((cell, ci) => <th key={ci}>{renderInline(cell, `${key}-th${ci}`, highlights)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{renderInline(cell, `${key}-${ri}-${ci}`, highlights)}</td>)}</tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }
    const bullet = line.match(/^[-*]\s+(.*)$/);
    const ordered = line.match(/^\d+[.)]\s+(.*)$/);
    if (bullet) {
      if (listTag !== "ul") { flushList(); listTag = "ul"; }
      listItems.push(bullet[1] ?? "");
      continue;
    }
    if (ordered) {
      if (listTag !== "ol") { flushList(); listTag = "ol"; }
      listItems.push(ordered[1] ?? "");
      continue;
    }
    flushList();
    if (line.trim() !== "") {
      blocks.push(<CollapsibleParagraph key={`p-${seq++}`} line={line} highlights={highlights} keyId={`p-${seq}`} />);
    }
  }
  flushList();
  return <div className="wb-md">{blocks}</div>;
});
