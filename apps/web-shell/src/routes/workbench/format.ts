/**
 * B3（frontend-optimization-roadmap-2026-09-12）：工作台展示辅助，自 ResearchWorkbenchPage 原样搬出。
 * 页面面板与 workbench/export.tsx 共用的价位/概率格式化与状态文案映射。
 * v39（2026-09-19 研报与追问质量路线图 Q05/Q06/Q07/Q08/Q09）：估值三态、价位时点、
 * 验证点日期口径、补证计数与核心统计口径也收在这里——**同一句话只允许有一个来源**，
 * 屏显与导出各自复制一份三元表达式正是上一轮口径漂移的根因。
 */
import type {
  CashFlowQualityBlock,
  StockReport,
  EarningsGrowthRow,
  ComparableReview,
  ClaimFindings,
  DirectionPoolCandidate,
  ReportScenario,
  ReportValuation,
  ReportWatchpoint,
  ScenarioBoundView,
  ScenarioProbabilityView,
  FollowupRetrievalStatus,
  ValuationEvidenceView,
} from "./researchTypes";
import type { ReportHighlightTone } from "./reportHighlightTones";

export const RESEARCH_COMPLETENESS_READING_NOTE = "完成度按已核验的研究项统计；未核验或证据不足的项目不算完成。字数仅供参考。";

/** R04/H05：概览与导出共用的阅读边界，只展示既有记录，不补造判断。 */
export function reportReadingSummary(report: StockReport): string[] {
  const support = coreSupportState(report.claim_findings);
  const completeness = report.research_completeness;
  const valuation = valuationEvidenceDisplay(report.valuation);
  const staleClaims = report.claim_findings?.stale_claims ?? [];
  const action = report.decision_card?.next_action;
  const lines = [
    report.evidence_quality
      ? `A 级来源：${report.evidence_quality.tiers.A?.length ?? 0} 条；等级不等于本报告全部判断均已核验。`
      : "A 级来源：未记录分级结果。",
    support.counts.total > 0
      ? `核心判断支撑：${support.counts.supported}/${support.counts.total}（完整支撑的核心判断数／纳入校验的核心判断数）。`
      : "核心判断支撑：未记录可统计的判断校验结果。",
    completeness?.items?.length
      ? `研究完成度：${completeness.done_count}/${completeness.items.length}（已完成研究项数／要求研究项数）；判断有支撑不等于研究已完成。`
      : "研究完成度：未记录研究项检查结果，不能据此认定完整。",
    `估值评估：${valuation.label}${valuation.state !== "fair_value_assessed" ? "；合理价值尚未评估，不据指标高低认定低估或偏贵。" : ""}`,
    staleClaims.length > 0
      ? `证据时效（生成时记录）：${staleClaims.length} 条判断所引数据超期（${staleClaims.join("、")}），使用前须重新取数核验。`
      : report.claim_findings?.staleness_checked === true
        ? "证据时效（生成时记录）：未记录超期命中；不代表全部来源日期齐全，也不代表数据当前仍有效。"
        : "证据时效：未记录可用的时效检查结果，不能认定来源未过期。",
  ];
  if (action) {
    lines.push(`下一验证动作：${action.signal || "未提供验证事项"}；验证方式：${action.verify_by || "未提供"}；${action.event_anchored ? "按事件触发，不预填日期" : `到期日：${action.due_on || "未提供"}`}。`);
    if (action.expected_if_true) lines.push(`若验证成立：${action.expected_if_true}`);
  } else {
    lines.push("下一验证动作：未提供，需补充验证事项与依据。");
  }
  return lines;
}

/** 正文与摘要各自的逐句记录保持独立，旧记录没有规则编号时明确说明。 */
export function valuationRepairDisplayLines(report: StockReport): string[] {
  return [
    { label: "正文", rows: report.valuation_language_repairs },
    { label: "执行摘要", rows: report.summary_overreach?.valuation_language_repairs },
  ].flatMap(({ label, rows }) => (rows ?? []).flatMap((row) => [
    `${label}第 ${row.line} 行；规则：${row.rules?.join("、") || "旧记录未记录编号"}`,
    `原句：${row.sentence}`,
    ...row.notes.map((note) => `加注：${note}`),
  ]));
}

export function earningsAnalysisDisplay(row: EarningsGrowthRow): string {
  const label = row.analysis_state === "analyzed" ? "证据核验支持完成分析"
    : row.analysis_state === "insufficient" ? "已检查，证据不足" : "未核验，不计为完成分析";
  return `${label}${row.analysis_note ? `；${row.analysis_note}` : ""}${row.analysis_model ? `（核验模型：${row.analysis_model}）` : ""}`;
}

/** H05: translate only the two fixed notes authored by normalize_earnings_growth. */
export function earningsNoteDisplay(note: string): string {
  return note
    .replaceAll("未给出可复算的贡献比例（服务端不代填数字）", "未给出可复算的贡献比例，不补填数值")
    .replaceAll("贡献比例缺少计算依据（basis/formula），按未量化处理", "贡献比例缺少计算依据或公式，按未量化处理");
}

export function comparableReviewNote(review: ComparableReview): string {
  const note = review.note || "";
  // Only replace the exact recorded conflict when its structured counts exist.
  if (!review.count_conflict || review.model_claimed_sample_count == null || review.sample_count == null) return note;
  const labels: Record<string, string> = { pe_ttm: "PE（TTM）", pb_mrq: "PB（MRQ）", ps_ttm: "PS（TTM）" };
  const basis = Object.entries(review.comparable_basis ?? {}).map(([key, count]) => `${labels[key] ?? key} ${count} 只`);
  const conflict = `样本数量不一致：模型列出 ${review.model_claimed_sample_count} 只，同业快照采用 ${review.sample_count} 只。`
    + (review.server_peer_count == null ? "" : `同业池共 ${review.server_peer_count} 只。`)
    + (basis.length ? `各指标可比样本：${basis.join("、")}。` : "")
    + "中位数按同业快照的样本口径解读。";
  return note.includes(review.count_conflict) ? note.replace(review.count_conflict, conflict) : `${note} ${conflict}`.trim();
}

/** R01: keep period disclosures identical in the panel and both export formats. */
export function cashFlowDisplayLines(cash: CashFlowQualityBlock): string[] {
  if (cash.periods?.length) {
    const { periods, ...topLevel } = cash;
    const hasTopLevel = [cash.operating_cash_flow, cash.net_income, cash.capex, cash.free_cash_flow,
      cash.free_cash_flow_reported, cash.depreciation_amortization, cash.working_capital,
      cash.working_capital_change].some((value) => value != null);
    return [
      ...(hasTopLevel ? ["原始顶层口径", ...cashFlowDisplayLines(topLevel)] : []),
      ...periods.flatMap((block, index) => [
      `现金流分期 ${index + 1}`,
      ...cashFlowDisplayLines(block),
      `分期判定：${block.verdict || "未判定"}；${block.note || ""}`,
      ...(block.downgrade_note ? [`分期降档说明：${block.downgrade_note}`] : []),
      ]),
    ];
  }
  const show = (value?: number | null) => value == null ? "未取到" : String(value);
  const context = (key: keyof NonNullable<CashFlowQualityBlock["metric_metadata"]>, fallback = "") => {
    const meta = cash.metric_metadata?.[key];
      return `统计期：${meta?.period || fallback || "未标注"}；单位：${meta?.unit || "未标注"}；模型标注来源：${meta?.sources.length ? meta.sources.join("、") : "未标注"}${meta?.currency ? `；币种：${meta.currency}` : ""}`;
  };
  return [
    `经营现金流 ${show(cash.operating_cash_flow)}（${context("operating_cash_flow", cash.operating_cash_flow_period)}）`,
    `归母净利 ${show(cash.net_income)}（${context("net_income")}）｜覆盖倍数 ${show(cash.coverage_ratio)}`,
    `资本开支 ${show(cash.capex)}（${context("capex", cash.capex_period)}）`,
    `自由现金流 ${show(cash.free_cash_flow)}（${context("free_cash_flow")}；${cash.free_cash_flow_derived ? "同期间输入复算" : "复算状态未核验"}）`,
    ...(cash.funding_balance_note ? [`资本开支后资金余缺：${cash.funding_balance_note}`] : []),
    `折旧摊销 ${show(cash.depreciation_amortization)}（${context("depreciation_amortization")}）`,
    `营运资本变动 ${show(cash.working_capital ?? cash.working_capital_change)}（${context("working_capital")}）`,
      "金额须同期间、同单位、同币种才能相减或比较；未标注币种不代表已核验，缺失口径请回查原始来源。",
    ...(cash.free_cash_flow_reported != null ? [`模型提供的自由现金流 ${cash.free_cash_flow_reported}（原值留痕，不代表核验通过）`] : []),
  ];
}

/**
 * 价位展示精度：A 股报价到分（2 位小数）。
 * 模型偶发把来源的全精度浮点直接写进 levels（如 6.648999999999999）；证据层已收口，
 * 这里再兜一层，保证界面与导出都不会出现机器精度数字。
 */
export function formatPrice(value: number | null | undefined): string {
  if (value == null) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return String(Number(number.toFixed(2)));
}

/**
 * R7（研报产出与交付链路路线图 2026-09-27）：时间展示**统一走本地时区、秒级精度**。
 *
 * 同一份导出件曾有两套时间格式：`generated_at` 走 `toLocaleString()`（本地时区、秒级），
 * 而来源目录的 `retrieved_at` 是服务端 `datetime.now(UTC).isoformat()` 原样打印
 * （`2026-09-27T08:55:56.785097+00:00`）——读者要心算 8 小时时差，微秒精度对用户无意义。
 * 解析不出来的值**原样显示**（不编造、不补今天）。
 */
export function formatTimestamp(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  if (!text) return "未标注";
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  const offset = -parsed.getTimezoneOffset();
  const hours = String(Math.floor(Math.abs(offset) / 60)).padStart(2, "0");
  const minutes = String(Math.abs(offset) % 60).padStart(2, "0");
  return `${parsed.toLocaleString()}（UTC${offset >= 0 ? "+" : "-"}${hours}:${minutes}）`;
}

/**
 * 正文关键数字高亮：tone 来自信息状态（蓝=关键结论 / 琥珀=待核验 / 珊瑚红=无来源支撑），
 * **不是**涨跌方向。定义收在这里（而非 panels.tsx）是为了让 `splitKeyFactSegments`
 * 与屏显、导出三处共用同一份类型——R1 之前两侧各写一套切分实现，同一份报告高亮位置必然不一致。
 */
export interface ReportHighlight {
  text: string;
  /** 仅在该判断原文覆盖的范围内显示证据颜色；不按相同数字传播状态。 */
  context?: string;
  tone: ReportHighlightTone;
  /** 悬停/读屏的文字标签（每种颜色必须同时有文字表达，§3.2）。 */
  title?: string;
}

/** R1：段内高亮预算——每段最多 2 处（§八.5，屏显与导出共用同一常量）。 */
export const HIGHLIGHT_BUDGET_PER_PARAGRAPH = 2;

/** 一段纯文本按关键数字切分后的片段；命中片段带 highlight。 */
export interface HighlightSegment {
  text: string;
  highlight?: ReportHighlight;
}

/**
 * R1（研报产出与交付链路路线图 2026-09-27）：把一段**纯文本**按关键数字切成有序片段。
 *
 * 屏显 `panels.renderInline` 与导出 `export.markdownToHtml` **共用本函数**，因此两侧的
 * `<mark>` 序列必然逐字相同。此前屏显走 `rest.indexOf(highlight.text)` 子串匹配、导出走
 * `highlight.text === token` 全等匹配，同一份报告两边高亮位置不同源（路线图 §3 R1「两套实现不同源」）。
 *
 * 仅命中完整数字；带 context 的条目必须处于对应判断原文范围内。
 * 同一起点取更长文本。`budget` 由调用方按段递减。
 */
export function splitKeyFactSegments(
  plain: string,
  highlights: ReportHighlight[],
  budget: number = HIGHLIGHT_BUDGET_PER_PARAGRAPH,
): HighlightSegment[] {
  if (!plain) return [];
  if (budget <= 0 || highlights.length === 0) return [{ text: plain }];
  const sorted = [...highlights].sort((a, b) => b.text.length - a.text.length);
  const out: HighlightSegment[] = [];
  let rest = plain;
  let left = budget;
  while (rest && left > 0) {
    let bestIndex = -1;
    let best: ReportHighlight | null = null;
    for (const highlight of sorted) {
      if (!highlight.text) continue;
      let found = rest.indexOf(highlight.text);
      while (found >= 0) {
        const absolute = plain.length - rest.length + found;
        const contextStart = highlight.context ? plain.lastIndexOf(highlight.context, absolute) : 0;
        const inContext = !highlight.context || (contextStart >= 0
          && absolute + highlight.text.length <= contextStart + highlight.context.length);
        const before = plain[absolute - 1] ?? "";
        const after = plain[absolute + highlight.text.length] ?? "";
        if (inContext && !/[A-Za-z0-9_.%+-]/.test(before) && !/[A-Za-z0-9_.%]/.test(after)) break;
        found = rest.indexOf(highlight.text, found + 1);
      }
      if (found < 0) continue;
      if (bestIndex < 0 || found < bestIndex
        || (found === bestIndex && highlight.text.length > (best?.text.length ?? 0))) {
        bestIndex = found;
        best = highlight;
      }
    }
    if (!best || bestIndex < 0) break;
    if (bestIndex > 0) out.push({ text: rest.slice(0, bestIndex) });
    out.push({ text: best.text, highlight: best });
    left -= 1;
    rest = rest.slice(bestIndex + best.text.length);
  }
  if (rest) out.push({ text: rest });
  return out;
}

export const CLAIM_STATUS_LABEL: Record<string, string> = {
  supported: "有支撑",
  downgraded: "未独立验证",
  no_source: "无来源",
};

/**
 * JV04：没进语义支撑层的三类原因——「未校验」必须说清是哪一种，
 * 否则用户会把没跑过的判断当成已通过（屏显与导出共用同一份措辞）。
 */
export const JEV_SKIP_LABEL: Record<string, string> = {
  no_cited_source: "无有效引用可比对",
  over_claim_limit: "超出单次评估条数上限",
  over_state_budget: "超出单次评估预算",
};

/** J03（桌面端升级路线图 2026-09-18）核心结论三态：supported / partial / unsupported；无 claims 时 null。 */
export type CoreSupportState = "supported" | "partial" | "unsupported" | null;

/**
 * J03 核心结论三态与分母：核心判断（importance=high）**全数支撑**才算「有支撑」；
 * 模型未标 importance 时全部判断视为核心（与后端 report_quality.check_claims 同一公式）。
 * 从 findings.claims 现算——旧档案（J03 之前落库、无 core_conclusion_state 字段）与
 * 新档案走同一口径，屏显/导出不需要区分两代数据。
 */
export function coreSupportState(findings: ClaimFindings | null | undefined): {
  state: CoreSupportState;
  counts: { supported: number; total: number };
} {
  const claims = findings?.claims ?? [];
  if (claims.length === 0) return { state: null, counts: { supported: 0, total: 0 } };
  const marked = claims.filter((item) => item.importance === "high");
  const core = marked.length > 0 ? marked : claims;
  const supported = core.filter((item) => item.support === "full").length;
  const state: CoreSupportState =
    supported === core.length ? "supported" : supported > 0 ? "partial" : "unsupported";
  return { state, counts: { supported, total: core.length } };
}

/**
 * Q09（2026-09-19 路线图）：把「2/3」这类统计**说清数的是谁**——
 * 导出与屏显都只写过「部分支撑（2/3）」，读者无法知道分子分母各自是哪几条判断。
 * 计数口径与 coreSupportState 完全一致（同一条规则，两处调用），只是把编号带出来。
 *
 * `coreFlags` 按 `claims` 原顺序给出「这条有没有进核心支撑统计」：判定只能靠位置，
 * 不能靠 claim_id 字符串——旧档案没有编号时按 id 匹配会把每条都判成「未列入核心」，
 * 于是同一份数据里出现「部分支撑（2/3）」与「七条全不列入核心」的自相矛盾。
 */
export function coreSupportBreakdown(findings: ClaimFindings | null | undefined): {
  coreIds: string[];
  supportedIds: string[];
  unsupportedIds: string[];
  coreFlags: boolean[];
} {
  const claims = findings?.claims ?? [];
  if (claims.length === 0) return { coreIds: [], supportedIds: [], unsupportedIds: [], coreFlags: [] };
  const marked = claims.filter((item) => item.importance === "high");
  const core = marked.length > 0 ? marked : claims;
  const coreSet = new Set(core);
  // 无编号的旧档案按「第 n 条」点名，仍然可回查，不再显示成一片「—」。
  const labelOf = (item: ClaimFindings["claims"][number], index: number) => item.claim_id || `第${index + 1}条`;
  const coreIds = core.map((item) => labelOf(item, claims.indexOf(item)));
  const supportedIds = core.filter((item) => item.support === "full").map((item) => labelOf(item, claims.indexOf(item)));
  const unsupportedIds = core.filter((item) => item.support !== "full").map((item) => labelOf(item, claims.indexOf(item)));
  return {
    coreIds,
    supportedIds,
    unsupportedIds,
    coreFlags: claims.map((item) => coreSet.has(item)),
  };
}

/** 主观概率展示：后端已把百分数写法归一到 0–1；非数值一律「—」，不补 0。 */
export function formatProbability(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

/**
 * v28 §2.4 概率展示口径：无历史基准 → 数值概率一律 null，只用等级（低/中/高）表达。
 * 等级缺失时回退数值展示（后端将来接入后验统计后会同时给出两者）；
 * 两者都缺才显示「—」，**不补 0、不猜数**。
 */
export function formatScenarioProbability(item: ReportScenario): string {
  if (item.probability_level) return item.probability_level;
  return formatProbability(item.probability);
}

/** R04/J09（2026-09-18 路线图）：情景倾向同值时的统一展示文案。 */
export const SCENARIO_PROBABILITY_UNGIVEN = "—（未给概率）";

/**
 * 一组情景的概率展示（R04：屏显与导出**共用同一同值判定**，两处口径必然一致）。
 *
 * G3（研报产出与交付链路路线图 2026-09-27）：判定从「三档全同」放宽到「**存在重复档**」。
 * 旧口径只覆盖「三档完全相同」，样报 002468 的乐观=中 / 中性=中 / 悲观=低 非全同，
 * 于是两条「中」照原样印出，读起来仍像做了概率区分；模型没做区分时不该被读成有区分。
 * 现在只要有两档取到同一个等级就按「未给概率」处理（`uniform=true` 时每档一律返回该文案）。
 * 全为「—」（压根没给取值）时不触发——那时本身已显示「—」，不需要再补一句「未给概率」。
 */
export function scenarioProbabilityDisplay(scenarios: ReportScenario[]): {
  uniform: boolean;
  displayOf: (item: ReportScenario) => string;
} {
  const displays = scenarios.map((item) => formatScenarioProbability(item));
  const uniform = scenarios.length > 1
    && displays.some((value) => value !== "—")
    && new Set(displays).size < displays.length;
  return {
    uniform,
    displayOf: (item) => (uniform ? SCENARIO_PROBABILITY_UNGIVEN : formatScenarioProbability(item)),
  };
}

/** 结论对反证的耐受度 → 展示文案与色号。 */
export const ROBUSTNESS_META: Record<string, { text: string; tone: string }> = {
  robust: { text: "对反证耐受（robust）", tone: "mint" },
  mixed: { text: "部分可被反证削弱（mixed）", tone: "warn" },
  fragile: { text: "易被反证推翻（fragile）", tone: "coral" },
};

/* —— M1（2026-09-15 第二轮路线图 B04）：候选身份判定与代码列口径 ——
 * 候选表与 Markdown 导出共用同一套判定，避免「界面显示无有效代码、导出却写成公司名」的两套口径。 */

/** A 股代码前缀白名单（与后端 instruments 同一前缀段口径的保守子集）。 */
const CN_STOCK_PREFIXES = new Set([
  "60", "68", "605", "601", "603",  // 沪主板/科创板（按两位前缀归并）
  "60", "68", "90",
  "00", "30", "20", "12", "18",      // 深主板/创业板/ETF
  "43", "83", "87", "88", "92",      // 北交所
]);

/** v33 D09：旧记录（v33 之前生成）没有 identity_status 字段——
 * 按 A 股代码前缀规则在**前端重新校验**，使旧候选可以继续送雷达扫描，
 * 而不是被误判为全部不可发送。新记录直接采信服务端核验结果。 */
export function legacyIdentityStatus(candidate: DirectionPoolCandidate): string {
  if (candidate.identity_status) return candidate.identity_status;
  const code = (candidate.symbol || candidate.symbol_raw || "").replace(/\D/g, "");
  if (code.length !== 6) return "invalid";
  const prefix2 = code.slice(0, 2);
  const prefix3 = code.slice(0, 3);
  const ok = CN_STOCK_PREFIXES.has(prefix2) || CN_STOCK_PREFIXES.has(prefix3)
    || ["600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301"].includes(prefix3);
  return ok ? "verified" : "invalid";
}

/** M1-B04：候选表代码列文本——只有**核验通过的 6 位代码**才作为代码展示；
 * 无效/缺失时明确写「无有效代码」，绝不把公司名或模型原文当代码显示。
 * （实测样报：6 只候选 symbol 全是公司名，旧渲染把「中国通号」直接印在代码行。） */
export function poolCodeText(candidate: DirectionPoolCandidate): string {
  const status = legacyIdentityStatus(candidate);
  const code = (candidate.symbol || "").replace(/\D/g, "");
  if (status === "verified" && code.length === 6) return code;
  return "无有效代码";
}

/** 代码列补充说明（鼠标悬停）：区分「模型给对」「服务端按证据补码」「不可用」。 */
export function poolCodeHint(candidate: DirectionPoolCandidate): string {
  const status = legacyIdentityStatus(candidate);
  const code = (candidate.symbol || "").replace(/\D/g, "");
  if (status === "verified" && code.length === 6) {
    return candidate.symbol_backfilled
      ? `标准代码 ${code}（服务端按本次估值证据的「公司名(代码)」匹配回填）`
      : `标准代码 ${code}（模型输出，已通过证券主数据校验）`;
  }
  if (status === "unverified") return "格式合法但主数据未接入，需人工核对";
  const raw = candidate.symbol_raw || candidate.symbol || "";
  return raw && raw !== code
    ? `无有效 6 位股票代码（模型原文：${raw}）——不可送雷达扫描`
    : "无有效 6 位股票代码——不可送雷达扫描";
}

/* ============================================================================
   v39（2026-09-19 研报与追问质量路线图 Q05/Q06/Q07/Q08/Q09/Q11）：口径文案集中处。
   这些函数存在的唯一理由：屏显与导出必须说同一句话。上一轮「界面显示无有效代码、
   导出却写成公司名」的漂移，根源就是同一判定被复制成两份三元表达式。
   ========================================================================== */

/** 估值三态标签（与后端 VALUATION_EVIDENCE_STATE_LABELS 同词；后端下发 `label` 时优先用后端的）。 */
export const VALUATION_EVIDENCE_STATE_LABEL: Record<string, string> = {
  metric_missing: "估值指标缺失",
  normalization_unverified: "指标已取得，合理价值尚未评估",
  fair_value_assessed: "合理价值已完成评估",
};
/** 三态的信息状态色（不是买卖方向：缺失=灰、待验证=琥珀、已评估=青绿）。 */
export const VALUATION_EVIDENCE_STATE_TONE: Record<string, string> = {
  metric_missing: "gray",
  normalization_unverified: "warn",
  fair_value_assessed: "mint",
};
/** 三态对应的结论词——Q05 的核心：PE/PB 都在的样例不能再显示笼统「无数据」。 */
export const VALUATION_VERDICT_DISPLAY: Record<string, string> = {
  metric_missing: "无估值指标数据",
  normalization_unverified: "合理价值未评估（指标已有，缺正常化盈利验证）",
  fair_value_assessed: "合理价值已评估",
};
/** 估值指标字段的中文名（与后端 VALUATION_METRIC_FIELDS 同一集合，PB 为来源附带口径留位）。 */
const VALUATION_METRIC_LABEL: Record<string, string> = {
  pe_ttm: "PE(TTM)",
  pe_percentile: "历史分位",
  peer_position: "同业位置",
  pb_mrq: "PB",
};
/** 模型常把「没取到」写成占位词，这些取值一律按缺失处理（与后端 _VALUATION_PLACEHOLDERS 同表）。 */
const VALUATION_PLACEHOLDERS = new Set(["无", "—", "-", "不适用", "无数据", "n/a", "na", "null", "未提供", ""]);

/**
 * v39 真机修订：深研块里的 PE/PB/ROE 现在来自**服务端估值快照**，是全精度浮点（11.69555428）。
 * 屏显与导出共用这一份格式化（两位小数；空值如实「未取到」，不补 0、不写「无数据」）。
 */
export function deepMetricCell(value?: number | string | null, empty = "未取到"): string {
  if (value === null || value === undefined || value === "") return empty;
  if (typeof value === "number") return String(Math.round(value * 100) / 100);
  return value;
}

/**
 * 估值三态的统一展示口径（研报估值卡、结论卡估值行、追问附录、Markdown 导出共用）。
 *
 * 入参可以是研报的 `ReportValuation`（读它的 `valuation_evidence`）或追问附录的
 * `valuation_view`（后端同形下发）——**判定发生在服务端，前端只搬运**；
 * 旧档案两个来源都没有时，按与后端同一规则补齐 state/结论词：这只是把「无数据」这句
 * 误导的话换成如实的话，不新增任何估值判断。
 */
export function valuationEvidenceDisplay(source?: ReportValuation | ValuationEvidenceView | null): {
  state: string;
  label: string;
  tone: string;
  verdictDisplay: string;
  note: string;
  metrics: { key: string; label: string; value: string }[];
  metricCount: number;
  /** true = 该记录无 valuation_evidence/valuation_view，三态由前端按同一规则补齐（只换措辞，不改判）。 */
  derived: boolean;
} {
  const isView = Boolean(source && "state" in source);
  const evidence = isView ? (source as ValuationEvidenceView) : (source as ReportValuation | undefined)?.valuation_evidence;
  const valuation = isView ? undefined : (source as ReportValuation | undefined);
  const metrics = Object.entries(VALUATION_METRIC_LABEL)
    .map(([key, label]) => ({ key, label, value: String(valuation?.[key as keyof ReportValuation] ?? "").trim() }))
    .filter((item) => item.value !== "" && !VALUATION_PLACEHOLDERS.has(item.value) && !VALUATION_PLACEHOLDERS.has(item.value.toLowerCase()));
  if (evidence?.state) {
    const state = evidence.state;
    return {
      state,
      label: evidence.label || VALUATION_EVIDENCE_STATE_LABEL[state] || state,
      tone: VALUATION_EVIDENCE_STATE_TONE[state] ?? "gray",
      verdictDisplay: evidence.verdict_display || VALUATION_VERDICT_DISPLAY[state] || evidence.label || state,
      note: evidence.note || "",
      metrics: evidence.metrics
        ? Object.entries(evidence.metrics).map(([key, value]) => ({ key, label: VALUATION_METRIC_LABEL[key] ?? key, value: String(value) }))
        : metrics,
      metricCount: evidence.metric_count ?? metrics.length,
      derived: false,
    };
  }
  // 旧档案回退：与后端 derive_valuation_evidence_state 同一分支顺序（正常化盈利+公允价值齐备才算已评估）。
  const earnings = valuation?.normalized_earnings;
  const earningsReady = typeof earnings?.value === "number" || Boolean(earnings?.basis?.trim());
  const fairValueReady = (valuation?.valuation_cases ?? []).some((item) => item.fair_value != null)
    || typeof valuation?.margin_of_safety === "number";
  const status = (valuation?.valuation_status ?? "").trim().toLowerCase();
  const state = earningsReady && fairValueReady && ["undervalued", "fair", "expensive"].includes(status)
    ? "fair_value_assessed"
    : metrics.length > 0
      ? "normalization_unverified"
      : "metric_missing";
  const listed = metrics.map((item) => `${item.label} ${item.value}`).join("｜");
  return {
    state,
    label: VALUATION_EVIDENCE_STATE_LABEL[state] ?? state,
    tone: VALUATION_EVIDENCE_STATE_TONE[state] ?? "gray",
    verdictDisplay: VALUATION_VERDICT_DISPLAY[state] ?? "—",
    note: state === "normalization_unverified"
      ? `${listed} 已取得，但正常化盈利未经验证（未拆分一次性损益/周期因素），因此不能给出低估或偏贵结论——这属于「合理价值尚未评估」，不是没有估值数据。`
      : state === "metric_missing"
        ? "未取得 PE/PB/历史分位等同口径估值指标（来源缺失或获取失败），本轮不对合理价值作判断。"
        : "",
    metrics,
    metricCount: metrics.length,
    derived: true,
  };
}

/** Translate the exact metric prefix authored by derive_valuation_evidence_state. */
export function valuationEvidenceReadingNote(view: ReturnType<typeof valuationEvidenceDisplay>): string {
  // Same order as report_quality.VALUATION_METRIC_FIELDS; archived JSON can reorder keys.
  const metrics = ["pe_ttm", "pe_percentile", "peer_position"].flatMap((key) => view.metrics.filter((item) => item.key === key));
  const prefix = metrics.map((item) => `${item.key} ${item.value}`).join("｜");
  if (!prefix || !view.note.startsWith(`${prefix} 已取得`)) return view.note;
  const reading = metrics.map((item) => `${item.label} ${item.value}`).join("｜");
  return reading + view.note.slice(prefix.length);
}

/** 价位角色 → 中文（追问引用的价位必须说明它是什么位，否则读者会把均线当目标价）。 */
export const PRICE_ROLE_LABEL: Record<string, string> = {
  close: "收盘价",
  ma: "均线",
  support: "支撑位",
  resistance: "压力位",
  other: "参考价位",
};

export function priceLevelLabel(role?: string | null): string {
  return PRICE_ROLE_LABEL[(role ?? "").trim().toLowerCase()] ?? "参考价位";
}

/**
 * Q08：区间界别的口径词——只凭「历史高点/低点」身份出现的价位是**参考位**，
 * 不是未来窗口的目标价（样报把 18.95 历史高点直接放进 5–10 交易日区间）。
 */
export function referenceBoundLabel(bound?: ScenarioBoundView | null): {
  label: string;
  note: string;
  isReference: boolean;
} {
  const side = (bound?.side ?? "").trim();
  const note = bound?.note ?? "";
  switch ((bound?.kind ?? "").trim()) {
    case "reference":
      return { label: side === "low" ? "参考支撑位" : "参考压力位", note, isReference: true };
    case "technical":
      return { label: side === "low" ? "技术支撑位" : "技术压力位", note, isReference: false };
    default:
      return { label: "模型自述界（未核对）", note, isReference: false };
  }
}

/** 未校准倾向的默认列名与说明（后端下发 label/note 时以后端为准，两者同词）。 */
export const UNCALIBRATED_PROBABILITY_LABEL = "倾向（未统计校准）";
export const UNCALIBRATED_PROBABILITY_NOTE = "无历史后验基准：低/中/高 只是模型倾向，未做统计校准，不等于上涨概率。";

/**
 * Q08：概率列的表头与口径说明。`calibrated` 目前后端恒为 false，
 * 因此列名只能是「倾向（未统计校准）」——写成「概率」会被读成上涨概率（样报缺陷）。
 * 无 view（旧记录/研报）时按情景自身的 probability_basis 推断：出现 posterior 才恢复「概率」列名。
 */
export function scenarioProbabilityWording(
  view?: ScenarioProbabilityView | null,
  scenarios: ReportScenario[] = [],
): { column: string; note: string; calibrated: boolean } {
  if (view && view.calibrated === true) return { column: "概率", note: view.note ?? "", calibrated: true };
  const calibrated = view
    ? false
    : scenarios.some((item) => (item.probability_basis ?? "").trim() === "posterior");
  if (calibrated) return { column: "概率", note: "", calibrated: true };
  const noteFromData = view?.note
    ?? scenarios.map((item) => item.probability_note).find((note) => Boolean(note?.trim()))
    ?? "";
  return {
    column: view?.probability_label || UNCALIBRATED_PROBABILITY_LABEL,
    note: noteFromData || UNCALIBRATED_PROBABILITY_NOTE,
    calibrated: false,
  };
}

/** Q06：追问引用的价位行 → 单元格集合（屏显表格与 Markdown 表格同词，不各写一套）。 */
export function priceRefCells(ref: {
  level: number; role: string; as_of: string; window: string; snapshot_only: boolean; forward_looking: boolean; note: string;
}): { level: string; role: string; asOf: string; window: string; status: string; note: string } {
  const status: string[] = [];
  if (ref.forward_looking) status.push("适用窗口指向未来");
  status.push(ref.snapshot_only ? "历史快照（本轮未刷新）" : "本轮刷新数据");
  return {
    level: formatPrice(ref.level),
    role: priceLevelLabel(ref.role),
    asOf: ref.as_of || "未标注",
    window: ref.window || "—",
    status: status.join(" · "),
    note: ref.note || "",
  };
}

/** 验证点日期口径（Q07）：预计时间与事实时间分开写，被降级的原值留痕但不当作用户该信的日期。 */
export const DATE_KIND_LABEL: Record<string, string> = {
  factual: "事实时间",
  estimated: "预计时间",
  event_anchor: "事件锚定",
  outside_coverage: "超出行情覆盖期",
  demoted: "日期已降级",
};
export const DATE_KIND_TONE: Record<string, string> = {
  factual: "mint",
  estimated: "blue",
  event_anchor: "gray",
  outside_coverage: "warn",
  demoted: "warn",
};

export function watchpointDateView(point?: ReportWatchpoint | null): {
  text: string;
  basis: string;
  kind: string;
  tone: string;
  note: string;
  demotedFrom: string;
  assumptions: string[];
} {
  const kind = (point?.date_kind ?? "").trim();
  const demotedFrom = (point?.date_demoted_from ?? "").trim();
  return {
    text: point?.verify_by || "—",
    // 新契约才有 date_basis：缺失时如实说「未标注」，不替旧记录编一个依据。
    basis: point?.date_basis_label || (point && point.date_basis === undefined ? "日期依据未标注（旧版记录）" : "无依据（模型自述）"),
    kind: DATE_KIND_LABEL[kind] ?? (kind ? kind : "未标注"),
    tone: DATE_KIND_TONE[kind] ?? "gray",
    note: point?.date_note || "",
    demotedFrom,
    assumptions: point?.assumptions ?? [],
  };
}

/** Q07：验证点的重复与方向核对（判定在服务端 `review_watchpoint_consistency` 完成，这里只负责露出来）。 */
export function watchpointReview(point?: ReportWatchpoint | null): { duplicateOf: number; note: string } {
  return {
    duplicateOf: Number(point?.duplicate_of ?? 0) || 0,
    note: String(point?.direction_note ?? "").trim(),
  };
}

/**
 * G2（研报产出与交付链路路线图 2026-09-27）：验证点能否**自动回填**。
 *
 * 判定与服务端 `longterm.materialize_watchpoint_judgment` 逐字同源：只有能从验证时点解析出
 * **确切日期**（`due_on`）的验证点，才会幂等落成可回填的判断；锚定事件（「三季报披露后」
 * 「下次月度经营简报披露后」）没有日期，而判断的 `due_at` 是必填字段——服务端**不猜日期**、
 * 不落库，界面因此也不给它回填入口。
 *
 * 为什么这条判定必须有唯一实现：角标、验证点表、导出件、复盘队列四处口径一旦各写一份，
 * 就会出现「角标说 5 条待复盘、队列里只有 3 条能点」这种对不上账的界面。
 */
export function watchpointRefillable(point?: ReportWatchpoint | null): boolean {
  return Boolean(String(point?.due_on ?? "").trim());
}

/** G2：验证点清单的「可回填 / 锚定事件」计数（角标与表格说明共用）。 */
export function watchpointRefillSummary(points?: ReportWatchpoint[] | null): {
  total: number;
  refillable: number;
  anchored: number;
} {
  const rows = points ?? [];
  const refillable = rows.filter((item) => watchpointRefillable(item)).length;
  return { total: rows.length, refillable, anchored: rows.length - refillable };
}

/**
 * G2：验证点「可回填」列的唯一文案（屏显表格与导出件共用，避免两处各说一套）。
 *
 * 注意措辞只承诺**回填入口存在**，不承诺会自动监控、也不承诺已回填——
 * 「已到期」不等于「已验证」（与复盘队列同一口径）。
 */
export function watchpointRefillLabel(point?: ReportWatchpoint | null): { text: string; tone: string; hint: string } {
  return watchpointRefillable(point)
    ? {
      text: "可回填",
      tone: "blue",
      hint: "验证时点可解析出确切日期，已幂等落成可回填判断——结论由人工回填（成立/失效/无数据）",
    }
    : {
      text: "锚定事件·不可自动回填",
      tone: "gray",
      hint: "验证时点锚定事件、无确切日期，而判断必须带到期时间——不猜日期，故不自动落库；可在复盘队列里手工标记已复核",
    };
}

/**
 * JV06：验证点的**展示顺序与折叠分组**（屏显与导出同口径，只此一份）。
 *
 * 三条纪律：
 *  1. 顺序**只读服务端的 `rank`**，前端不重排——否则「排序逻辑」会在两处各长一份并漂移；
 *  2. 折叠的条目**仍在返回的分组里**，只是进 `deferred`（不删除、不隐藏到查不到）；
 *  3. `triaged=false`（Jev 未启用 / 本轮没分诊）时保持**原序**且 `deferred` 为空——
 *     没评过的东西没有理由被挪动，更没有理由被藏起来。
 */
export function watchpointTriageView(points?: ReportWatchpoint[] | null): {
  ordered: ReportWatchpoint[];
  main: ReportWatchpoint[];
  deferred: ReportWatchpoint[];
  triaged: boolean;
} {
  const rows = points ?? [];
  const triaged = rows.some((item) => item.triage != null);
  const ordered = triaged
    ? [...rows].sort((a, b) => (a.triage?.rank ?? 0) - (b.triage?.rank ?? 0))
    : rows;
  return {
    ordered,
    main: ordered.filter((item) => item.triage?.deferred !== true),
    deferred: ordered.filter((item) => item.triage?.deferred === true),
    triaged,
  };
}

/** JV06：单条验证点的优先级展示（未分诊时返回 null，界面据此不渲染该列）。 */
export function watchpointPriorityView(point?: ReportWatchpoint | null): {
  label: string;
  tone: string;
  percent: string;
  confidence: string;
  title: string;
} | null {
  const triage = point?.triage;
  if (!triage) return null;
  // 未取得判定 / 未送评 → 如实说「未分诊」，绝不渲染成「暂缓」或「不急」。
  const rated = triage.score_percent != null;
  const percent = triage.score_percent ?? 0;
  const tone = !rated ? "gray" : triage.deferred ? "warn" : percent >= 50 ? "blue" : "green";
  return {
    label: rated ? triage.level_label : "未分诊",
    tone,
    percent: rated ? `${triage.score_percent}` : "—",
    confidence: triage.confidence != null ? triage.confidence.toFixed(2) : "—",
    title: triage.note || "",
  };
}

/**
 * Q11：补证取数结果一句话（屏显与导出同口径）。
 * 全部失败不能写成「研究完成」，未发起也不能留白——「未发起补证」才是解读模式的实情。
 */
export function retrievalStatusView(status?: FollowupRetrievalStatus | null): {
  text: string;
  tone: string;
  note: string;
  failedItems: { kind: string; label: string; error: string }[];
} {
  if (!status || !status.requested) {
    return { text: "未发起补证取数", tone: "gray", note: status?.note ?? "本次只依据原报告与你的补充材料，未获取新事实。", failedItems: [] };
  }
  const summary = `补证 ${status.succeeded}/${status.requested} 成功`;
  const failed = status.failed_items ?? [];
  switch (status.state) {
    case "all_succeeded":
      return { text: `${summary} · 全部成功`, tone: "mint", note: status.note ?? "", failedItems: failed };
    case "partial":
      return { text: `${summary} · 部分失败`, tone: "warn", note: status.note ?? "", failedItems: failed };
    case "all_failed":
      return { text: `${summary} · 全部失败`, tone: "coral", note: status.note ?? "", failedItems: failed };
    default:
      return { text: summary, tone: "gray", note: status.note ?? "", failedItems: failed };
  }
}

/** Q09：来源目录里的链接列——没有链接就如实写「未提供链接」，绝不猜一个看起来像的 URL。 */
export function sourceCatalogUrlText(entry: { url?: string; url_note?: string }): string {
  const url = (entry.url ?? "").trim();
  return url || `（未提供链接${entry.url_note ? `：${entry.url_note}` : ""}）`;
}

/**
 * R2（研报产出与交付链路路线图 2026-09-27）：来源目录链接列的 **Markdown** 写法。
 *
 * 导出件（Markdown / HTML / Word / PNG）里链接要可点击，因此写成 `[URL](URL)`；
 * `markdownToHtml` 已支持该语法并渲染成带 `rel="noopener noreferrer"` 的 `<a>`。
 * 取不到链接时与屏显同一文案（`sourceCatalogUrlText`）——**绝不按来源名猜一个 URL**。
 */
export function sourceCatalogUrlMarkdown(entry: { url?: string; url_note?: string }): string {
  const url = (entry.url ?? "").trim();
  // Markdown 链接目标里的空格与括号会打断语法，取不到干净 URL 就退回纯文本（不猜、不拼）。
  if (!url || /[\s()]/.test(url)) return sourceCatalogUrlText(entry);
  return `[${url}](${url})`;
}
