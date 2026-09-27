import { useRef, useState } from "react";
import type { ArtifactPoolView } from "@investment-steward/domain-contracts";
import { detailOf, type CoreClient } from "../state/coreClient";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：量化研究域数据，自 AppShell 原样下沉。
 * QuantPage 直接消费本钩子；pool 由 AppShell 的 loadAll 启动装载经 setPool 回填。
 * 以下领域类型原样自 AppShell 搬出（QuantPage 改从本模块导入）。
 */

/** 三段式切分（QL02）:train 选模 / valid 调阈值 / test 只确认一次。 */
export interface QuantFactorSplit {
  ratio: number[];
  train_end: number;
  valid_end: number;
  usable_samples: number;
  planned: { train: number; valid: number; test: number };
  degraded: boolean;
  degraded_reason: string | null;
  min_segment_samples: number;
}

/** 相关性去重簇（QL08）:同簇变体只留排序最优者,被合并的公式在此列明。 */
export interface QuantFactorCluster {
  size: number;
  merged_formulas: string[];
  max_abs_correlation: number;
  merged_truncated?: boolean;
  merged_sample_size?: number;
}

/** IC 评估套件（QL05）:逐期 IC/ICIR、IC 衰减、因子自相关、分位分组收益。 */
export interface QuantFactorEvaluation {
  spec_version: number;
  forward_days: number;
  samples: number;
  period_ic: {
    folds: number;
    fold_samples?: number | null;
    series: number[];
    mean: number | null;
    std: number | null;
    icir: number | null;
    positive_ratio: number | null;
    degraded_reason?: string;
  };
  ic_decay: { days: number; ic: number; t_stat: number | null; p_value: number; samples: number }[];
  autocorrelation: Record<string, number>;
  group_returns: {
    groups: number;
    group_samples?: number | null;
    mean_forward_return: number[];
    spread_top_bottom: number | null;
  };
}

/** 泄漏检查（QL04）:全量打分 vs 截断打分逐点比对。 */
export interface QuantFactorLookahead {
  uses_future_data: boolean | null;
  checked_points: number[];
  mismatch_count: number;
  mismatches: { index: number; probe: number; full: number; prefix: number }[];
  detail: string;
}

/** 多重检验校正报告（QL03）。 */
export interface QuantFactorCorrection {
  method: "fdr" | "permutation";
  correction?: string;
  alpha: number;
  seed: number;
  candidates_total: number;
  candidates_evaluated: number;
  candidates_tested: number;
  passed_screen: number;
  rejected_total: number;
  threshold: number | null;
  permutations?: number;
  p_resolution?: number | null;
  calibration?: {
    null_mean: number | null;
    null_sigma: number | null;
    empirical_critical_95: number | null;
    pool_size: number;
    calibration_candidates?: number;
    permutations: number;
    seed: number;
    block: number;
    degraded_reason?: string;
  };
}

/** 公式语法契约（QL06）:由 Core 的 quant_factors.formula_grammar() 导出,前端不再自建 token 清单。 */
export interface QuantFormulaGrammar {
  formula_spec_version: number;
  atoms: string[];
  legacy_atoms: string[];
  binary_ops: string[];
  unary_ops: string[];
  time_series: {
    bases: string[];
    windows: number[];
    tokens: string[];
    token_pattern: string;
    semantics: Record<string, string>;
  };
  search: {
    default: "beam" | "exhaustive";
    modes: string[];
    beam_width: number;
    beam_levels: number;
    beam_pair_pool: number;
    exhaustive_atoms: string[];
    exhaustive_max_depth: number;
  };
  evaluation: Record<string, string>;
  notes: string[];
}

/** 搜索报告（QL06）:束宽/层数/生成数与候选池,全部可复算。 */
export interface QuantFactorSearchReport {
  strategy: "beam" | "exhaustive";
  beam_width?: number;
  levels?: number;
  pair_pool?: number;
  max_depth?: number;
  atoms: number | string[];
  ts_tokens?: number;
  candidates_generated?: number;
  candidates_evaluated?: number;
  candidates_tested?: number;
  selection_segment: string;
  randomness: string;
  meaning: string;
}

/** 窗口来源（QL09）:请求档位 vs 实际取回,上游静默截断时显式说明。 */
export interface QuantFactorWindow {
  requested: number | null;
  bars: number;
  tier: number;
  tiers: number[];
  reason: string;
  clamped?: boolean;
  bars_actual?: number;
  source?: string;
  truncated?: boolean;
  source_row_cap?: number | null;
  first_bar?: string | null;
  last_bar?: string | null;
  truncation_note?: string;
}

/** 因子挖掘（POST /quant/factors/mine/{symbol}）:确定性搜索 + 三段式切分 + 多重检验校正。 */
export interface QuantFactorItem {
  formula: string;
  formula_tokens: string[];
  train_ic: number;
  valid_ic: number;
  test_ic: number;
  sign_consistency: number;
  rank_score: number;
  /** QL06:复杂度收缩系数与收缩后的排序键（榜单按后者排序）。 */
  complexity_shrink?: number;
  rank_score_adjusted?: number;
  samples: number;
  segment_samples: { train: number; valid: number; test: number };
  test_effective_samples: number;
  test_t_stat: number | null;
  test_p_value: number;
  test_significant: boolean;
  significance_note: string | null;
  complexity: number;
  cluster: QuantFactorCluster | null;
  evaluation?: QuantFactorEvaluation;
  lookahead?: QuantFactorLookahead | null;
}
export interface QuantFactorMineResult {
  available: boolean;
  symbol: string;
  bars?: number;
  window?: QuantFactorWindow;
  top: QuantFactorItem[];
  /** 未达统计显著的候选:显式标注,不当有效因子（不再静默丢弃）。 */
  marginal: QuantFactorItem[];
  split?: QuantFactorSplit;
  search?: QuantFactorSearchReport;
  multiple_testing?: QuantFactorCorrection;
  dedup?: { threshold: number; scanned: number; scan_limit: number; clusters: (QuantFactorCluster & { representative: string })[]; merged_total: number };
  label_shuffle?: { seed: number; block: number; meaning: string } | null;
  thresholds?: { min_train_ic: number; min_valid_ic: number };
  grammar?: QuantFormulaGrammar;
  caliber?: { mine_spec_version: number; formula_spec_version?: number; ic_spec_version: number; ic_suite_spec_version: number; forward_days: number };
  note?: string;
  source?: string;
  /** 分享池阶段 A 形态字段:收口 bar 时间与数据版本(导出参数集用)。 */
  as_of?: string | null;
  dataset_version?: string | null;
  degraded_reason?: string | null;
}

/** 挖掘参数:窗口档位 / 校正口径 / 过拟合自检 seed / 搜索策略（QL06）。 */
export interface QuantFactorMineOptions {
  window?: number;
  correction?: "fdr" | "permutation";
  alpha?: number;
  labelPermutationSeed?: number;
  search?: "beam" | "exhaustive";
}

/** 分享池阶段 A/D 制品(GET /quant/parameter-sets):内容寻址、不可变;模型条目带权重与训练快照。 */
export interface QuantParameterSet {
  artifact_id: string;
  name: string;
  symbol: string;
  formula_tokens?: string[];
  formula?: string;
  weights?: number[];
  feature_order?: string[];
  /** 阶段 D 新口径:训练段冻结的 z-score 统计量(缺失=旧制品,按原始量纲解释)。 */
  standardization?: { mean: number[]; std: number[] } | null;
  metrics: {
    train_ic: number;
    valid_ic: number;
    samples: number;
    forked_from?: string;
    /** QL02 起:三段式切分的 test 段 IC 与显著性。 */
    test_ic?: number;
    test_t_stat?: number | null;
    test_p_value?: number;
    segment_samples?: { train: number; valid: number; test: number };
    split?: QuantFactorSplit;
    /** QL05:IC 评估套件(口径版本挂在 caliber)。 */
    ic_suite?: QuantFactorEvaluation;
    caliber?: { mine_spec_version: number; ic_spec_version: number; ic_suite_spec_version: number; forward_days: number };
  };
  training?: {
    lambda?: number;
    lambda_selection?: {
      method: string;
      criterion?: string;
      grid?: number[] | null;
      selected?: number;
      reports?: { lambda: number; available?: boolean; median_excess_vs_baseline?: number; oos_consistency?: number; degraded_reason?: string }[];
    };
    standardized?: boolean;
    forward_days?: number;
    split_index?: number;
    as_of?: string | null;
  };
  type: string;
  stage: string;
  author: string;
  parent_id: string | null;
  note: string;
  as_of: string | null;
  dataset_version: string | null;
  created_at: string;
  track_records?: QuantTrackRecord[];
}

/** 实盘记录两级制(GET /quant/parameter-sets 联表):自报 vs 对账单核验,只作筛选不参与排序。 */
export interface QuantTrackRecord {
  record_id: string;
  artifact_id: string;
  state: "self_reported" | "broker_verified";
  period_start: string;
  period_end: string;
  return_pct: number;
  max_drawdown_pct: number | null;
  source: string | null;
  statement_sha256: string | null;
  note: string;
  created_at: string;
}

/** 策略包目录(GET /quant/strategy-packs):发布者签名制,受限执行器运行。 */
export interface QuantStrategyPack {
  pack_id: string;
  name: string;
  version: string;
  author: string;
  entrypoint: string;
  symbol?: string;
  capabilities: string[];
  artifact_sha256: string;
  payload_sha256?: string;
  signature_valid: boolean;
  state: string;
  imported_at: string;
}

/** 策略包回测(POST /quant/strategy-packs/{id}/run):受限执行器 + 确定性统计。 */
export interface QuantPackRunResult {
  available?: boolean;
  pack_id: string;
  name?: string;
  equity?: number;
  curve?: Array<{ date: string; position: number; equity: number }>;
  win_rate?: number | null;
  bars?: number;
  source?: string;
  note?: string;
  degraded_reason?: string | null;
}

/** 分享池阶段条(GET /quant/stages):各阶段开放状态与原因,UI 动态渲染。 */
export interface QuantStageInfo {
  key: string;
  open: boolean;
  reason: string;
}

/** 确定性回放（GET /quant/parameter-sets/{id}/replay）：tanh 仓位 × 次日收益。 */
export interface QuantCaliber {
  kind: "gross" | "net";
  costs_applied: boolean;
  detail: string;
  compare_with: string;
  net_spec_version: number;
  net_costs: {
    commission_bps: number;
    slippage_bps: number;
    max_position: number;
    max_turnover_per_bar: number;
  };
}

export interface QuantReplayResult {
  available?: boolean;
  artifact_id: string;
  equity: number;
  curve: Array<{ date: string; position: number; equity: number }>;
  win_rate: number | null;
  bars: number;
  note: string;
  source?: string;
  degraded_reason?: string | null;
  /** 第四轮审计：本页是**毛收益**，与「我的实验」页的净收益不是同一口径，两者净值不可直接比较。 */
  caliber?: QuantCaliber;
}

/* ------------------------------------------------------------------------- *
 * QL10 / QL11 / QL12（量化研究实验室质量提升路线图 2026-09-22，P2 数据面质变）
 * 单标的时序口径 → 横截面口径:本地面板（QL10）→ 横截面挖掘（QL11）→ 报告卡闭环（QL12）。
 * ------------------------------------------------------------------------- */

/** 面板构建/挖掘的逐标的进度（done/total）。 */
export interface QuantPanelProgress {
  done: number;
  total: number;
}

/** 逐标的取数留痕（QL10：成功带来源与行数，失败带原因，不静默跳过）。 */
export interface QuantPanelStep {
  symbol: string;
  ok: boolean;
  from_cache?: boolean;
  source?: string;
  rows?: number;
  first_bar?: string | null;
  last_bar?: string | null;
  reason?: string;
}

export interface QuantPanelFailure {
  symbol: string;
  reason: string;
}

/** 因历史长度不足被剔除的标的（v2）：交集口径下它的历史长度就是整个面板的长度。 */
export interface QuantPanelExclusion {
  symbol: string;
  rows: number;
  reason: string;
}

/** 面板摘要（GET /quant/panel/load 与构建任务 summary 同形；矩阵本体不回传）。 */
export interface QuantPanelSummary {
  available: boolean;
  panel_spec_version: number;
  alignment: string;
  window: number;
  symbol_count: number;
  date_count: number;
  cells: number;
  first_date: string | null;
  last_date: string | null;
  coverage: number | null;
  alignment_loss?: {
    union_dates: number;
    common_dates: number;
    dropped_by_intersection: number;
    requested_symbols?: number;
    eligible_symbols?: number;
    excluded_by_history?: number;
  };
  /** v2 起：因历史短于门槛被剔除的标的（交集口径下它会决定整个面板的长度）。 */
  excluded?: QuantPanelExclusion[];
  history_floor?: number;
  symbols: string[];
  failures: QuantPanelFailure[];
  steps: QuantPanelStep[];
  cancelled: boolean;
  degraded_reason: string | null;
  digest?: string;
  note?: string;
  snapshot?: QuantPanelSnapshotInfo;
  universe?: QuantPanelUniverse;
  caliber?: { panel_spec_version: number; alignment: string; window: number; board: string; use_cache: boolean };
}

export interface QuantPanelSnapshotInfo {
  snapshot_hash: string;
  file_name: string;
  content_hash: string;
  date_count: number;
  symbol_count: number;
  cells: number;
  first_date?: string | null;
  last_date?: string | null;
}

export interface QuantPanelUniverse {
  rule: string;
  requested_size: number;
  size: number;
  symbols: string[];
  candidates_available: number;
  source: string;
  observed_at: string;
  names: string[];
}

/** 面板快照索引行（GET /quant/panel/snapshots）：内容寻址，供「从快照重建」选键。 */
export interface QuantPanelSnapshot {
  artifact_id: string;
  kind: string;
  stage: string;
  symbol: string;
  name: string;
  created_at: string;
  file_name: string;
  content_hash: string;
  payload_meta: {
    panel_spec_version: number;
    alignment: string;
    window: number;
    universe_rule: string;
    universe_source: string;
    universe_observed_at: string;
    first_date: string | null;
    last_date: string | null;
    coverage: number | null;
    failures: QuantPanelFailure[];
  };
}

/** QL11：IC 序列统计（重叠标签按 forward_days 等距抽样后才做 t 检验）。 */
export interface QuantCrossIcSummary {
  samples: number;
  mean: number | null;
  std: number | null;
  icir: number | null;
  positive_ratio: number | null;
  t_stat: number | null;
  p_value: number | null;
  effective_samples: number;
  overlap: number;
  degraded_reason: string | null;
}

/** QL11：一条横截面候选（三段 IC + BH 校正 + 复杂度收缩）。 */
export interface QuantCrossRow {
  formula: string;
  formula_tokens: string[];
  complexity: number;
  train_ic: number | null;
  valid_ic: number | null;
  test_ic: number;
  train_icir: number | null;
  test_icir: number | null;
  test_positive_ratio: number | null;
  test_t_stat: number | null;
  test_p_value: number | null;
  test_effective_samples?: number;
  sign_consistency: number;
  rank_score: number;
  complexity_shrink: number;
  rank_score_adjusted: number;
  test_dates?: number;
  test_significant: boolean;
  significance_note: string | null;
  neutralise_mode?: string;
  raw_test_ic?: number | null;
  ic_delta_test?: number | null;
  neutralisation?: { mode: string; groups_used: number; unassigned_ratio: number };
}

/** QL11：横截面榜单（top 只含校正后显著者，未达显著进 marginal 并显式标注）。 */
export interface QuantCrossBoard {
  available: boolean;
  cs_spec_version: number;
  top: QuantCrossRow[];
  marginal: QuantCrossRow[];
  split?: {
    train_end: number;
    valid_end: number;
    usable_dates: number;
    planned: { train: number; valid: number; test: number };
    degraded: boolean;
    degraded_reason: string | null;
    min_segment_dates: number;
    feasible?: boolean;
  };
  multiple_testing?: {
    method: string;
    alpha: number;
    candidates_evaluated: number;
    candidates_tested: number;
    passed_screen: number;
    rejected_total: number;
    threshold: number | null;
  };
  dedup?: { threshold: number; scanned: number; merged_total: number };
  search?: {
    strategy: string;
    beam_width: number;
    beam_levels: number;
    selection_segment: string;
    candidates_evaluated: number;
    cs_ops: string[];
  };
  neutralise?: {
    mode: string;
    unassigned_ratio: number;
    groups_used: number;
    comparison?: {
      pairs: number;
      neutralised_test_ic_mean: number | null;
      raw_test_ic_mean: number | null;
      delta_test_ic_mean: number | null;
      note: string;
    };
  };
  ic_definition?: { kind: string; min_symbols_per_date: number; forward_days: number; effective_sampling: string };
  dates?: { total: number; first: string | null; last: string | null };
  label_shuffle?: { seed: number; dates_permuted: number; values_permuted: number } | null;
  degraded_reason: string | null;
}

/** QL12：成本口径分位多空（建仓日按满仓换手计费；容量只给代理量级）。 */
export interface QuantLongShortReport {
  samples: number;
  first_date: string;
  last_date: string;
  gross_mean: number;
  gross_total: number;
  net_mean: number;
  net_total: number;
  cost_total: number;
  net_std: number;
  net_sharpe: number | null;
  net_annualised: number;
  max_drawdown: number;
  turnover_mean: number;
  quantile_mean_forward_returns: (number | null)[];
  equity_curve: { date: string; gross: number; net: number; turnover: number; equity: number }[];
}

/** QL12：一张报告卡（IC 套件 + 分位多空 + fold 稳定性 + 容量 + 口径 + 结果哈希）。 */
export interface QuantPanelReportCard {
  available: boolean;
  formula: string;
  formula_tokens: string[];
  ic: QuantCrossIcSummary;
  quantiles?: number;
  long_short?: QuantLongShortReport;
  folds?: {
    fold: number;
    first_date: string;
    last_date: string;
    ic_mean: number | null;
    ic_samples: number;
    net_mean: number | null;
    net_total: number | null;
    skipped_sections?: number;
  }[];
  capacity?: {
    available: boolean;
    proxy?: string;
    volume_unit?: number;
    universe_median_adv_cny?: number;
    participation?: number;
    per_name_notional_cny?: number;
    universe_notional_cny?: number;
    caveat?: string;
    degraded_reason?: string;
  };
  caliber?: Record<string, string | number>;
  report_hash?: string;
  test_significant?: boolean;
  degraded_reason?: string | null;
}

/** QL11+QL12 挖掘闭环 summary：面板摘要 + 榜单 + 逐公式报告卡 + 行业分组缺口。 */
export interface QuantPanelMineSummary {
  panel: QuantPanelSummary;
  board: QuantCrossBoard;
  reports: QuantPanelReportCard[];
  industry_groups: {
    mode: string;
    source?: string;
    assigned?: number;
    unassigned?: string[];
    unassigned_ratio?: number;
    usable?: boolean;
    sectors_total?: number;
    sectors_scanned?: number;
    errors?: { scope: string; reason: string }[];
    degraded_reason?: string | null;
  };
  caliber: Record<string, string | number | null>;
  panel_snapshot: { content_hash: string; file_name: string; digest: string };
}

export interface QuantPanelBuildOptions {
  universeSize?: number;
  board?: string;
  window?: number;
  refresh?: boolean;
}

export interface QuantCrossMineOptions {
  forwardDays?: number;
  topN?: number;
  beamWidth?: number;
  beamLevels?: number;
  alpha?: number;
  neutralise?: "none" | "demean" | "rank";
  industryGroups?: boolean;
  labelShuffleSeed?: number;
  quantiles?: number;
  folds?: number;
}

export function useQuant(client: CoreClient) {
  const [quantPool, setQuantPool] = useState<ArtifactPoolView | null>(null);

  // —— AlphaMaster 补充:确定性因子挖掘(三段式切分 + 多重检验校正 + 相关性去重) ——
  async function fetchFactorMine(symbol: string, options: QuantFactorMineOptions = {}): Promise<QuantFactorMineResult | null> {
    const query = new URLSearchParams();
    if (options.window) query.set("window", String(options.window));
    if (options.correction) query.set("correction", options.correction);
    if (options.alpha !== undefined) query.set("alpha", String(options.alpha));
    if (options.labelPermutationSeed !== undefined) query.set("label_permutation_seed", String(options.labelPermutationSeed));
    if (options.search) query.set("search", options.search);
    const suffix = query.toString() ? `?${query.toString()}` : "";
    const response = await client.request<QuantFactorMineResult>({ method: "POST", path: `/quant/factors/mine/${symbol}${suffix}` });
    if (response.status >= 400) return null;
    return response.data;
  }

  // —— A4-1 分享池阶段 A 通道:本机参数集列表 / 发布 / Fork / 确定性回放 / 谱系 ——
  async function fetchQuantParameterSets(): Promise<QuantParameterSet[] | null> {
    const response = await client.request<QuantParameterSet[]>({ method: "GET", path: "/quant/parameter-sets" });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  async function publishQuantParameterSet(input: { symbol: string; formulaTokens: string[]; name?: string; note?: string }): Promise<{ ok: boolean; entry?: QuantParameterSet; detail?: string }> {
    const response = await client.request<QuantParameterSet>({ method: "POST", path: "/quant/parameter-sets", body: { symbol: input.symbol, formula_tokens: input.formulaTokens, name: input.name ?? "", note: input.note ?? "" } });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "发布失败(Core 未就绪或公式非法)。" };
    return { ok: true, entry: response.data };
  }

  async function forkQuantParameterSet(artifactId: string): Promise<QuantParameterSet | null> {
    const response = await client.request<QuantParameterSet>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/fork`, body: {} });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchQuantReplay(artifactId: string): Promise<QuantReplayResult | null> {
    const response = await client.request<QuantReplayResult>({ method: "GET", path: `/quant/parameter-sets/${artifactId}/replay` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchQuantLineage(artifactId: string): Promise<QuantParameterSet[] | null> {
    const response = await client.request<QuantParameterSet[]>({ method: "GET", path: `/quant/parameter-sets/${artifactId}/lineage` });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  // —— 阶段条/模型/策略包/实盘记录(B/C/D 本机忠实形态) ——
  async function fetchQuantStages(): Promise<QuantStageInfo[] | null> {
    const response = await client.request<{ stages: QuantStageInfo[] }>({ method: "GET", path: "/quant/stages" });
    if (response.status >= 400 || !Array.isArray(response.data?.stages)) return null;
    return response.data.stages;
  }

  async function trainQuantModel(input: { symbol: string; lambda?: number | null }): Promise<{ ok: boolean; entry?: QuantParameterSet; detail?: string }> {
    const body: Record<string, unknown> = {};
    // 缺省 / null → 交给内核按 λ 网格 walk-forward 选择（不再默认写死 1.0）
    if (input.lambda !== undefined && input.lambda !== null) body.lambda = input.lambda;
    const response = await client.request<QuantParameterSet & { available?: boolean; degraded_reason?: string }>({ method: "POST", path: `/quant/models/${input.symbol}`, body });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "训练失败(Core 未就绪或行情不可用)。" };
    if (response.data?.available === false) return { ok: false, detail: response.data.degraded_reason ?? "行情不可用,未训练。" };
    return { ok: true, entry: response.data };
  }

  async function importQuantStrategyPack(manifestText: string): Promise<{ ok: boolean; entry?: QuantStrategyPack; detail?: string }> {
    let manifest: unknown;
    try {
      manifest = JSON.parse(manifestText);
    } catch {
      return { ok: false, detail: "manifest 不是合法 JSON。" };
    }
    const response = await client.request<QuantStrategyPack>({ method: "POST", path: "/quant/strategy-packs", body: { manifest } });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "策略包校验失败。" };
    return { ok: true, entry: response.data };
  }

  async function fetchQuantStrategyPacks(): Promise<QuantStrategyPack[] | null> {
    const response = await client.request<QuantStrategyPack[]>({ method: "GET", path: "/quant/strategy-packs" });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  async function runQuantStrategyPack(packId: string): Promise<QuantPackRunResult | null> {
    const response = await client.request<QuantPackRunResult>({ method: "POST", path: `/quant/strategy-packs/${packId}/run` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function addQuantTrackRecord(artifactId: string, input: Omit<QuantTrackRecord, "record_id" | "artifact_id" | "state" | "source" | "statement_sha256" | "created_at">): Promise<{ ok: boolean; detail?: string }> {
    const response = await client.request<QuantTrackRecord>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/track-records`, body: input });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "自报记录提交失败。" };
    return { ok: true };
  }

  async function verifyQuantTrackRecord(artifactId: string, input: { statement: string; source: string; period_start: string; period_end: string; return_pct: number; max_drawdown_pct: number | null; note: string }): Promise<{ ok: boolean; detail?: string }> {
    const response = await client.request<QuantTrackRecord>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/track-records/verify`, body: input });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "对账单核验失败。" };
    return { ok: true };
  }

  // —— QL10/QL11/QL12 横截面通道:面板快照 / 任务化构建 / 挖掘闭环 ——
  const [quantPanelProgress, setQuantPanelProgress] = useState<QuantPanelProgress | null>(null);
  const [quantPanelSnapshots, setQuantPanelSnapshots] = useState<QuantPanelSnapshot[]>([]);
  const [quantPanelJobBusy, setQuantPanelJobBusy] = useState(false);
  const panelJobRef = useRef<string | null>(null);

  async function fetchQuantPanelSnapshots(): Promise<QuantPanelSnapshot[] | null> {
    const response = await client.request<QuantPanelSnapshot[]>({ method: "GET", path: "/quant/panel/snapshots" });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    setQuantPanelSnapshots(response.data);
    return response.data;
  }

  /** QL10：按内容寻址键从快照重建面板（哈希在 Core 侧复核，不一致返回 null）。 */
  async function loadQuantPanel(contentHash: string, fileName: string): Promise<QuantPanelSummary | null> {
    const response = await client.request<QuantPanelSummary>({
      method: "POST", path: "/quant/panel/load", body: { content_hash: contentHash, file_name: fileName },
    });
    if (response.status >= 400) return null;
    return response.data;
  }

  /** 任务化执行器:POST 建任务 → 轮询进度（1.2s）→ 终态取 summary。停止（stopQuantPanelJob）退出循环。 */
  async function runPanelJob<T>(
    startPath: string, statusPath: (jobId: string) => string, body: Record<string, unknown>, failureLabel: string,
  ): Promise<{ ok: boolean; summary?: T; cancelled?: boolean; detail?: string }> {
    setQuantPanelJobBusy(true);
    setQuantPanelProgress(null);
    try {
      const start = await client.request<{ ok: boolean; job_id: string; reused?: boolean; done?: number; total?: number }>({
        method: "POST", path: startPath as `/${string}`, body,
      });
      if (start.status >= 400 || !start.data?.ok) {
        return { ok: false, detail: `${failureLabel}（${detailOf(start.data) ?? `HTTP ${start.status}`}）` };
      }
      const jobId = start.data.job_id;
      panelJobRef.current = jobId;
      if (start.data.reused) setQuantPanelProgress({ done: start.data.done ?? 0, total: start.data.total ?? 0 });
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        if (panelJobRef.current !== jobId) return { ok: false, cancelled: true, detail: "已停止。" };
        const status = await client.request<{ ok: boolean; state: string; done: number; total: number; summary: T | null; error: { detail?: string } | null }>({
          method: "GET", path: statusPath(jobId) as `/${string}`,
        });
        if (status.status >= 400 || !status.data?.ok) {
          return { ok: false, detail: `进度获取失败（HTTP ${status.status}）` };
        }
        const job = status.data;
        if (job.state === "running") {
          setQuantPanelProgress({ done: job.done, total: job.total });
          continue;
        }
        if (job.state === "done" && job.summary) return { ok: true, summary: job.summary };
        if (job.state === "cancelled") return { ok: false, cancelled: true, detail: "任务已停止。" };
        return { ok: false, detail: `${failureLabel}：${job.error?.detail ?? "任务失败，请重试"}` };
      }
    } catch {
      return { ok: false, detail: `${failureLabel}：请求失败，请重试。` };
    } finally {
      setQuantPanelJobBusy(false);
      setQuantPanelProgress(null);
      panelJobRef.current = null;
    }
  }

  /** QL10：构建本地面板（成交额前 N → 逐标的日线 → 交集对齐 → 内容寻址快照）。 */
  async function buildQuantPanel(options: QuantPanelBuildOptions = {}) {
    const body: Record<string, unknown> = {};
    if (options.universeSize !== undefined) body.universe_size = options.universeSize;
    if (options.board) body.board = options.board;
    if (options.window !== undefined) body.window = options.window;
    if (options.refresh) body.refresh = true;
    const result = await runPanelJob<QuantPanelSummary>(
      "/quant/panel/build", (jobId) => `/quant/panel/build/${jobId}`, body, "面板构建失败",
    );
    if (result.ok) void fetchQuantPanelSnapshots();
    return result;
  }

  /** QL11 + QL12：在面板快照上做横截面挖掘，top 公式自动出成本口径报告卡。 */
  async function mineQuantPanel(contentHash: string, fileName: string, options: QuantCrossMineOptions = {}) {
    const body: Record<string, unknown> = { content_hash: contentHash, file_name: fileName };
    if (options.forwardDays !== undefined) body.forward_days = options.forwardDays;
    if (options.topN !== undefined) body.top_n = options.topN;
    if (options.beamWidth !== undefined) body.beam_width = options.beamWidth;
    if (options.beamLevels !== undefined) body.beam_levels = options.beamLevels;
    if (options.alpha !== undefined) body.alpha = options.alpha;
    if (options.neutralise) body.neutralise = options.neutralise;
    if (options.industryGroups) body.industry_groups = true;
    if (options.labelShuffleSeed !== undefined) body.label_shuffle_seed = options.labelShuffleSeed;
    if (options.quantiles !== undefined) body.quantiles = options.quantiles;
    if (options.folds !== undefined) body.folds = options.folds;
    return runPanelJob<QuantPanelMineSummary>(
      "/quant/panel/mine", (jobId) => `/quant/panel/mine/${jobId}`, body, "横截面挖掘失败",
    );
  }

  /** 停止在途面板任务（构建或挖掘）——后端逐标的/逐报告卡检测，停止后不再发新取数。 */
  async function stopQuantPanelJob(): Promise<void> {
    const jobId = panelJobRef.current;
    if (!jobId) return;
    panelJobRef.current = null;
    setQuantPanelJobBusy(false);
    await Promise.allSettled([
      client.request({ method: "POST", path: `/quant/panel/build/${jobId}/cancel` }),
      client.request({ method: "POST", path: `/quant/panel/mine/${jobId}/cancel` }),
    ]);
  }

  return {
    quantPool, setQuantPool,
    fetchFactorMine, fetchQuantParameterSets, publishQuantParameterSet, forkQuantParameterSet,
    fetchQuantReplay, fetchQuantLineage, fetchQuantStages, trainQuantModel,
    importQuantStrategyPack, fetchQuantStrategyPacks, runQuantStrategyPack,
    addQuantTrackRecord, verifyQuantTrackRecord,
    quantPanelProgress, quantPanelSnapshots, quantPanelJobBusy,
    fetchQuantPanelSnapshots, loadQuantPanel, buildQuantPanel, mineQuantPanel, stopQuantPanelJob,
  };
}
