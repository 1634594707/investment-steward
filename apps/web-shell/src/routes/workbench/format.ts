/**
 * B3（frontend-optimization-roadmap-2026-09-12）：工作台展示辅助，自 ResearchWorkbenchPage 原样搬出。
 * 页面面板与 workbench/export.tsx 共用的价位/概率格式化与状态文案映射。
 * v39（2026-09-19 研报与追问质量路线图 Q05/Q06/Q07/Q08/Q09）：估值三态、价位时点、
 * 验证点日期口径、补证计数与核心统计口径也收在这里——**同一句话只允许有一个来源**，
 * 屏显与导出各自复制一份三元表达式正是上一轮口径漂移的根因。
 */
import type {
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

/** R04/J09（2026-09-18 路线图）：三档概率同值时的统一展示文案。 */
export const SCENARIO_PROBABILITY_UNGIVEN = "—（未给概率）";

/**
 * 一组情景的概率展示（R04：屏显与导出**共用同一同值判定**，两处口径必然一致）。
 * 三档（>1）概率取值全部相同 = 模型根本没做概率区分（样报 002468 三个「中」），
 * 照排会读起来像给了概率；`uniform=true` 时 `displayOf` 对每档一律返回「未给概率」。
 */
export function scenarioProbabilityDisplay(scenarios: ReportScenario[]): {
  uniform: boolean;
  displayOf: (item: ReportScenario) => string;
} {
  const distinct = new Set(scenarios.map((item) => formatScenarioProbability(item)));
  const single = distinct.size === 1 ? [...distinct][0] : null;
  const uniform = scenarios.length > 1 && single != null && single !== "—";
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
