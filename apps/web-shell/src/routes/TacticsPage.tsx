import { useEffect, useMemo, useRef, useState } from "react";
import {
  ColorType,
  CrosshairMode,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { formatDate } from "../state/format";
import { createCoreClient } from "../state/coreClient";
import { useTactics } from "../hooks/useTactics";
import type { TacticsHandoff } from "../state/handoff";
import { useUiPrefs } from "../shell/uiprefs";
import "./tactics.css";

/* ---------- 数据形状（与 Core /tactics/* 同源） ---------- */

export interface TacticMeta {
  id: string;
  name: string;
  direction: "bullish" | "bearish" | "neutral";
  description: string;
  /** 战法分类：均线 / 动能 / 量价形态 / 趋势结构 / 短线情绪（近似）。 */
  category?: string;
  /* —— v27 信号语义（方案 §4.2）：让「什么算确认 / 什么算失效」在界面上可见 —— */
  /** 族（= category 的对等别名，后端显式回传，避免前端各写一套映射）。 */
  family?: string;
  /** 同一形态多少根 K 线内视为「同一次触发」。 */
  cooldown_bars?: number;
  /** 什么算被后续行情确认。 */
  confirmation_rule?: string;
  /** 什么条件下该信号作废。 */
  invalidation_rule?: string;
  weight?: number;
  min_bars?: number;
}

/** 战法分类顺序与中文名（后端 category 字段）。 */
export const TACTIC_CATEGORY_LABEL: Record<string, string> = {
  ma: "均线",
  momentum: "动能",
  volume_price: "量价形态",
  structure: "趋势结构",
  sentiment: "短线情绪（近似）",
};

export interface TacticSignal {
  tactic_id: string;
  tactic_name: string;
  direction: "bullish" | "bearish" | "neutral";
  detail: string;
  date: string;
}

export interface TacticIndicators {
  close: number | null;
  ma5: number | null;
  ma10: number | null;
  ma20: number | null;
  macd_dif: number | null;
  macd_dea: number | null;
  kdj_k: number | null;
  kdj_d: number | null;
  kdj_j: number | null;
  rsi14: number | null;
  volume: number | null;
  volume_ma20: number | null;
}

export interface TacticBar {
  time: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

/* —— v24 战法质量分（Core tactics_score，与后端字段逐字对齐） —— */

/** 评分分项（全部可复算）：多空基础分 + 跨类共振 − 多空对冲 + v2 量价/位置/板块。 */
export interface ScoreParts {
  bullish: number;
  bearish: number;
  neutral: number;
  resonance: number;
  conflict: number;
  raw: number;
  norm_scale: number;
  /* —— v2 新增（可能缺失：旧数据或旧 Core 需如实降级，不得臆造）—— */
  /** 量能确认：占优方向最强信号当日的量比换算的加成（≤2.0）。 */
  volume_bonus?: number;
  /** 位置加成：低位 +1.5、高位 −1.0（区间位置 ≤0.5 / ≥0.9）。 */
  position_bonus?: number;
  /** 板块共振加成（≤3.0）：仅板块扫描且方向一致时非零。 */
  sector_bonus?: number;
  /** bonus 缩放系数 = 占优方向计分信号的最大新鲜度；无新鲜信号则 bonus 趋近 0。 */
  bonus_scale?: number;
  /** 饱和曲线尺度：分数 = 100 ×（1 − e^(−raw / 尺度)），100 是渐近上限。 */
  saturation_scale?: number;
}

/** 逐条命中明细：权重 × 新鲜度 = 贡献，可对照复算总分。 */
export interface HitDetail {
  tactic_id: string;
  tactic_name: string;
  direction: TacticSignal["direction"];
  category: string;
  weight: number;
  date: string;
  age_bars: number;
  freshness: number;
  contribution: number;
  min_bars: number;
  /* —— v2 同族合并 —— */
  /** 是否计入总分（同族只计最高一条，其余 false）。 */
  counted?: boolean;
  /** 被同族合并（命中了但不计分）——UI 需标注，不能让用户以为没命中。 */
  family_merged?: boolean;
  /** 未合并前的原始贡献（保留可复算性）。 */
  raw_contribution?: number;
  /* —— v27 信号语义（方案 §4.2）：触发价 + 失效/确认条件，可直接在图上核对 —— */
  /** 触发价（信号当日收盘，展示精度 2 位）；null = 该信号未带价位。 */
  trigger_price?: number | null;
  /** 同一形态多少根 K 线内视为同一次触发。 */
  cooldown_bars?: number;
  confirmation_rule?: string;
  invalidation_rule?: string;
  /* —— v28 信号状态机（二次方案 §3.2）：确定性四态，不由模型判断 —— */
  /** new=触发于最新一根尚无后续观察；pending=未确认也未失效；confirmed=站稳且量能未萎缩；invalidated=跌回触发日平台且放量。 */
  signal_state?: SignalState;
  signal_state_label?: string;
  /** 完整判定细节（观察根数 / 原因），用作悬浮说明。 */
  signal_state_detail?: SignalStateDetail;
}

/** v28 信号四态（与后端 tactics_score.SIGNAL_STATE_VERSION 同源）。 */
export type SignalState = "new" | "pending" | "confirmed" | "invalidated" | string;

/** v28 状态判定明细（全部可复算；缺 volume 时量能条件按不可判定处理 → 保守不给 confirmed）。 */
export interface SignalStateDetail {
  state: SignalState;
  label: string;
  reason: string;
  /** v29：该状态对应的触发日——前端据此把状态挂到「该战法最近一次触发」那条信号上，不误标历史信号。 */
  signal_date?: string;
  observed_bars: number;
  window_bars: number;
  min_bars: number;
  volume_ratio_threshold: number;
  invalidation_volume_ratio: number;
  version: string;
}

export const SIGNAL_STATE_LABEL: Record<string, string> = {
  new: "新触发",
  pending: "待确认",
  confirmed: "已确认",
  invalidated: "已失效",
};

/** 状态 → 展示色调（§五 战法雷达颜色联动 2026-09-13：与研报同一「信息状态」语义——
 *  新触发=靛蓝、待确认=琥珀、已确认=青绿、已失效=珊瑚红；不使用单一涨跌颜色，
 *  行情红涨绿跌只在行情数字处使用，两者并存但不互代）。 */
export const SIGNAL_STATE_TONE: Record<string, string> = {
  new: "blue",
  pending: "warn",
  confirmed: "mint",
  invalidated: "coral",
};

/** v27 数据质量标记（方案 §4.4）：分数该不该被信任，与分数本身同等重要。 */
export interface DataQuality {
  bars_count: number;
  /** 命中战法中最大的 min_bars（样本门槛）。 */
  required_bars: number;
  sample_sufficient: boolean;
  insufficient_tactics: string[];
  latest_bar_date: string;
  /** 近 60 根里缺失或不可解析的 OHLCV 字段。 */
  missing_fields: string[];
  level: "ok" | "thin" | "insufficient";
}

export const DATA_QUALITY_LABEL: Record<DataQuality["level"], string> = {
  ok: "数据充足",
  thin: "样本偏薄",
  insufficient: "样本不足",
};

/** 方向偏向：bullish/bearish 为占优方向，neutral 为无方向信号，conflict 为多空同时存在。 */
export type DirectionBias = "bullish" | "bearish" | "neutral" | "conflict";

export interface TacticScore {
  tactic_score: number;
  score_parts: ScoreParts;
  direction_bias: DirectionBias;
  hit_tactics: string[];
  hit_detail: HitDetail[];
  /** K 线根数不足该战法自身最小样本的战法 id（如实标注，不参与扣分）。 */
  insufficient_tactics: string[];
  weight_version: string;
  /** 新鲜度半衰期（K 线根数）：信号每老这么多根，贡献折半。分数因此与窗口长度无关。 */
  freshness_half_life_bars: number;
  /** 参与计算的 K 线总根数。 */
  bars_count: number;
  /** 命中窗口：只决定「哪些信号还算数」，不参与归一化。 */
  signal_window_from: string | null;
  signal_window_bars: number;
  /* —— v2 —— */
  /** 评分公式版本（v1 = 线性截断会贴顶，v2 = 饱和曲线）。 */
  score_formula_version?: string;
  /** v2：实际参与计分的战法（hit_tactics 含被同族合并的项，此项不含）。 */
  counted_tactics?: string[];
  /** v2：占优方向最强信号当日的量比（vs 近 20 根均量）。 */
  volume_ratio?: number | null;
  /** v2：最新收盘在近 N 根区间的位置（0=最低，1=最高）。 */
  position?: number | null;
  /** v2：被同族合并的族（如 ma）。 */
  merged_families?: string[];
  /* —— v27 排序口径公开（方案 §4.1/§4.4）—— */
  /** 图形证据强度（= tactic_score 的显式命名，与后验指标并列展示）。 */
  signal_score?: number;
  /** 历史超额：后验链路（第二期）指标，本期恒为 null —— 不用 0 冒充。 */
  historical_edge?: number | null;
  historical_edge_note?: string;
  final_rank_score?: number;
  rank_score_note?: string;
  /** 数据质量：样本充足度 / 最新 K 线日期 / 缺失字段。 */
  data_quality?: DataQuality;
  /** 最近一条**计分**信号的日期（而非窗口下界）。 */
  latest_signal_date?: string;
  latest_signal_age_bars?: number | null;
  tactic_semantics_version?: string;
  /* —— v28 质量闸门 + 信号状态机（二次方案 §3.2/§3.6）—— */
  /** 综合分 = 图形分 × 数据质量因子（后验不可用则不参与，见 rank_score_note）。 */
  rank_score?: number;
  /** 是否允许进入默认榜单（false = 被 §3.6 硬门挡下，原因见 gating_reasons）。 */
  eligible?: boolean;
  /** K 线字段缺失 / 日期异常等硬性不可排名标记。 */
  hard_blocked?: boolean;
  /** 被挡下 / 移出默认榜单的可读原因（空数组 = 无）。 */
  gating_reasons?: string[];
  /** 方向展示文本（多空并存时后端直接给「冲突」，前端不再自行拼词）。 */
  direction_display?: string;
  /** 板块共振：available=false 时说明原因（不可用 ≠ 0 分）。 */
  sector_resonance?: { available: boolean; reason?: string; ratio?: number; bonus?: number };
  /** 后验因子显式声明（本期 unavailable，不用 0/1 冒充）。 */
  posterior?: { available: boolean; note: string };
  /** 逐战法信号状态（tactic_id → 判定明细）。 */
  signal_states?: Record<string, SignalStateDetail>;
  /** 主信号（占优方向贡献最高者）的状态。 */
  primary_signal_state?: SignalStateDetail;
  signal_state_version?: string;
  /** 同战法被冷却窗口合并掉的信号条数（如实标注，不悄悄丢）。 */
  cooldown_merged?: Record<string, number>;
}
export const BIAS_LABEL: Record<DirectionBias, string> = {
  bullish: "偏多",
  bearish: "偏空",
  neutral: "中性",
  conflict: "多空冲突",
};

export interface ScanRow {
  symbol: string;
  name: string;
  ok: boolean;
  error?: string;
  provider?: string;
  hit_tactics: string[];
  latest_signals: TacticSignal[];
  indicators?: TacticIndicators;
  bars_count?: number;
  /** mode=all 快照趋势预筛分（0-100）：涨幅 40 + 换手 30 + 成交额分位 30；仅作候选粗筛参考。 */
  trend_score?: number;
  trend_parts?: { cp: number; tr: number; amt: number };
  /* v24 质量分（ok=true 时由后端给出；旧数据可能缺失，缺失时如实降级显示） */
  tactic_score?: number;
  score_parts?: ScoreParts;
  direction_bias?: DirectionBias;
  hit_detail?: HitDetail[];
  insufficient_tactics?: string[];
  /** 命中窗口下界（bars[-recent_bars] 的日期）；null = 窗口未知。 */
  signal_window_from?: string | null;
  signal_window_bars?: number;
  /* —— v2 评分 —— */
  score_formula_version?: string;
  counted_tactics?: string[];
  volume_ratio?: number | null;
  position?: number | null;
  merged_families?: string[];
  /** 板块归属（仅 mode=sectors 扫描有）。 */
  sector_code?: string;
  sector_name?: string;
  /* —— v27 排序口径公开（与 TacticScore 同源）—— */
  signal_score?: number;
  historical_edge?: number | null;
  final_rank_score?: number;
  data_quality?: DataQuality;
  latest_signal_date?: string;
  latest_signal_age_bars?: number | null;
  tactic_semantics_version?: string;
  /* —— v28 质量闸门 + 信号状态机（与 TacticScore 同源；旧数据缺失时如实降级不渲染）—— */
  rank_score?: number;
  eligible?: boolean;
  hard_blocked?: boolean;
  gating_reasons?: string[];
  direction_display?: string;
  sector_resonance?: { available: boolean; reason?: string; ratio?: number; bonus?: number };
  posterior?: { available: boolean; note: string };
  signal_states?: Record<string, SignalStateDetail>;
  primary_signal_state?: SignalStateDetail;
  signal_state_version?: string;
  cooldown_merged?: Record<string, number>;
  /* —— JV05：扫描批量去误报（仅在 Jev 可用时由后端写入；关闭时这几个字段不存在）—— */
  /** 语义复核判定：成立 / 存疑 / 不成立；null = 未取得判定。 */
  jev_verdict?: "valid" | "doubtful" | "invalid" | null;
  /** 中文标签（形态成立 / 形态存疑 / 疑似误报 / 未取得判定 / 未送评）。 */
  jev_label?: string;
  /** choice 的置信度；`noul` 无此字段，缺答时为 null。 */
  jev_confidence?: number | null;
  /**
   * `reviewed`（送评且通过）/ `pending_verification`（送评未通过或低置信）/
   * null 或缺失（**根本没送出去评**：分片失败 / 超单轮上限 / 单票超预算）。
   * 后两者必须区分——前者是「模型认为可疑」，后者是「这轮压根没核」。
   */
  jev_review_state?: "reviewed" | "pending_verification" | null;
  /** 人话说明；null 分支必须写明原因。 */
  jev_note?: string;
}

export interface ScanResult {
  results: ScanRow[];
  scanned: number;
  /** v24：命中窗口口径——最近 N 根 K 线（不是最近 N 条信号）。 */
  recent_bars?: number;
  weight_version?: string;
  score_formula_version?: string;
  /** v28：质量闸门版本（§3.6）。 */
  gate_version?: string;
  generated_at: string;
  /** D03：实际耗时（毫秒）；仅任务化的 /tactics/scan-market summary 携带，单票扫描无此字段。 */
  elapsed_ms?: number;
  /* —— JV05（2026-09-22 补接）—— */
  /** 扫描批量语义复核汇总。**日常扫描（观察清单+持仓+手动）同样有**：`/tactics/scan` 走的是
   *  自己的扫描循环（不是 `_scan_run_engine`），真机验证时才发现漏接，已补上。 */
  jev_review?: JevReview;
}

export interface MarketScanResult {
  results: ScanRow[];
  scanned: number;
  boards?: string[];
  mode?: string;
  market_rows?: number;
  complete?: boolean;
  recent_bars?: number;
  weight_version?: string;
  score_formula_version?: string;
  generated_at: string;
  /* —— v26 mode=sectors —— */
  /** 按板块分组的汇总块（含板块共振信息）。 */
  sectors?: SectorBlock[];
  /** 本轮是否启用了板块共振加分。 */
  sector_resonance?: boolean;
  /* —— D03（桌面端升级路线图 2026-09-18）—— */
  /** 实际耗时（毫秒，含礼貌间隔）。 */
  elapsed_ms?: number;
  /** 失败票清单（逐票 symbol/name/error），供任务详情展示失败原因分布。 */
  failures?: Array<{ symbol: string; name: string; error: string }>;
  /** v28：质量闸门版本（§3.6）。 */
  gate_version?: string;
  /* —— JV05（2026-09-21）：扫描批量语义复核汇总（仅 Jev 可用时 enabled=true）—— */
  jev_review?: JevReview;
}

/**
 * JV05：扫描批量去误报的汇总块。
 *
 * `enabled=false` 表示整层没跑（Jev 未启用或出网总闸关闭）——界面据此**不渲染任何复核信息**，
 * 与接入前逐字节一致。`enabled=true` 时三种「没标注」必须分开显示：
 * `reviewed` 已复核通过 / `pending` 送评未通过（或低置信）/ `unannotated` **根本没送出去评**。
 */
export interface JevReview {
  enabled: boolean;
  /** 响应回的真实模型版本号（多分片不同版本用 `/` 连接）。 */
  model?: string | null;
  total: number;
  reviewed: number;
  pending: number;
  /** 没送出去评的票数（分片失败 / 超单轮上限 / 单票超预算）——**不是**「通过」。 */
  unannotated: number;
  verdicts: { valid: number; doubtful: number; invalid: number };
  /** 被分片策略跳过的票及原因（逐票如实标注，不静默丢）。 */
  skipped: Array<{ symbol: string; reason: string; note: string }>;
  shards?: number;
  /** 复核段耗时（毫秒）；与 `elapsed_ms`（扫描段）分开，便于判断是哪一段慢。 */
  elapsed_ms?: number;
  note: string;
}

/* ---------- v26 行业板块（GET /tactics/sectors 与 mode=sectors 结果） ---------- */

/** 行业板块选项（新浪行业分类，按当日涨跌幅降序）。 */
export interface SectorMeta {
  code: string;
  name: string;
  member_count?: number;
  avg_price?: number | null;
  change_pct?: number | null;
  amount?: number | null;
  leader_symbol?: string;
  leader_name?: string;
  leader_change_pct?: number | null;
}

/** 板块内横截面共振上下文（同向占比达阈值才产出）。 */
export interface SectorResonance {
  direction: DirectionBias;
  ratio: number;
  count: number;
  sample: number;
  min_sample: number;
  threshold: number;
}

/** 板块扫描结果里的分组块。 */
export interface SectorBlock {
  code: string;
  name: string;
  ok: boolean;
  error?: string;
  scanned: number;
  failed?: number;
  change_pct?: number | null;
  member_count?: number;
  leader_name?: string;
  leader_symbol?: string;
  bullish_count?: number;
  bearish_count?: number;
  /** 板块共振上下文；null/缺失 = 无方向占优或样本不足（不给任何票加分）。 */
  resonance?: SectorResonance | null;
}

/* ---------- v26 战法 AI 复核（手动触发，落库留痕） ---------- */

/** AI 复核的结构化结论（tactics_ai.normalize_review 输出，字段可能缺失）。 */
export interface AiReviewContent {
  ai_score: number | null;
  verdict: "bullish" | "neutral" | "bearish";
  agreement: "agree" | "partial" | "disagree";
  summary: string;
  strengths: string[];
  concerns: string[];
  fake_breakout_risk: "low" | "medium" | "high";
  key_levels: { support: number | null; resistance: number | null };
  watch_points: string[];
  rule_score_comment: string;
}

/** 落库的复核记录（GET /tactics/ai-reviews 直接返回）。 */
export interface AiReviewRecord {
  review_id: string;
  symbol: string;
  name: string;
  /** 形如「方案名／模型名」。 */
  model: string;
  /** 规则分（确定性排序依据，与 AI 分并列不互相覆盖）。 */
  rule_score: number | null;
  ai_score: number | null;
  verdict: string;
  agreement: string;
  summary: string;
  payload: {
    review: AiReviewContent;
    rule_score?: TacticScore;
    provider?: string;
    latency_ms?: number;
    prompt_version?: string;
    score_formula_version?: string;
    sector?: SectorMeta | null;
    question?: string;
    bars_count?: number;
    /** JV03：Jev 判定层的原始值（未启用 Jev 时为 null）。原始分与等级数都要留痕，
     * 否则事后无法复核「这个展示分是按几级 rubric 算出来的」。 */
    judgement?: {
      ai_score: number | null;
      ai_score_raw: number | null;
      ai_score_levels: number;
      agreement: string;
      fake_breakout_risk: string;
      confidence: number | null;
      noul_value: number | null;
      noul_verdict: string | null;
    } | null;
    /** JV03：Jev 调用元信息（响应版本号、延迟、token）；未参与为 null。 */
    jev?: {
      model: string;
      requested_model: string;
      latency_ms: number;
      input_tokens: number | null;
      output_tokens: number | null;
      schema_version: string;
      question_schema_version: string;
    } | null;
    /** Jev 未参与的原因（未启用 / 不可用回落 chat）。 */
    jev_note?: string;
    /** 叙述层状态：ok / unparsable / model_unavailable / no_profile / error。 */
    narrative_status?: string;
    narrative_error?: string;
    narrative_model?: string;
  };
  /** JV03：判定层来源（jev / chat）；JV03 之前的旧记录为空串。 */
  engine?: string;
  /** JV03：认同度那题的 confidence（`noul` 题没有 confidence，见契约 C1）。 */
  confidence?: number | null;
  ai_score_raw?: number | null;
  created_at: string;
}

export const MARKET_BOARD_LABEL: Record<string, string> = {
  turnover: "成交额榜",
  gainers: "涨幅榜",
  losers: "跌幅榜",
  turnover_rate: "换手榜",
};

export interface WatchEntry {
  symbol: string;
  name: string;
  note: string;
  created_at: string;
  updated_at: string;
}

export interface NoteEntry {
  note_id: string;
  symbol: string;
  content: string;
  created_at: string;
}

export interface SignalsResult {
  symbol: string;
  provider: string;
  generated_at: string;
  bars: TacticBar[];
  sufficient: boolean;
  bars_count: number;
  indicators: TacticIndicators;
  signals: TacticSignal[];
  /** v24：本票整段 K 线的质量分（horizon = 全部 K 线），解释「形态强度与新鲜度」。 */
  score?: TacticScore;
}

interface Props {
  isDemo: boolean;
  pluginEnabled: boolean;
  onEnablePlugin: () => void;
  /** B2：当前视图是否为战法雷达页（KeepAlive 常驻挂载，进入页拉目录/观察/复核历史的依据）。 */
  active: boolean;
  /** B2：自选入库复用 AppShell 的 createHolding（与投资页共用同一份清单）。 */
  onCreateHolding: (input: { instrument: string; label: string; status: "watchlist"; strategy_note: string }) => Promise<boolean>;
  /** 方向研判标的池（投资页「送战法雷达」下发，null = 无）。 */
  /** v33 D03：来自研究工作台的结构化交接（唯一 requestId + 来源上下文）。 */
  tacticsHandoff: TacticsHandoff | null;
  /** 跳转投资页 AI 研究工作台并预填该标的。 */
  /** v33 D08：交接带来源与研究问题（信号上下文随预填进入个股研报）。 */
  onOpenStockReport: (symbol: string, context?: { question?: string; sourceLabel?: string; scanId?: string }) => void;
  /* —— v26 行业板块扫描 —— */
  /** 行业板块清单（GET /tactics/sectors）；空数组 = 尚未加载。 */
  /* —— v26 AI 复核（手动触发） —— */
  /** AI 是否接通（模型方案有「使用中」）；未接通时复核按钮禁用并提示去配置。 */
  aiReady: boolean;
  /** 复核历史（已按时间倒序）。 */
  /* —— v26 加入投资模块自选 —— */
  /** 已在投资模块持仓/自选中的标的代码。 */
  trackedInstruments: string[];
}

const DIRECTION_LABEL: Record<TacticSignal["direction"], string> = {
  bullish: "看多形态",
  bearish: "看空形态",
  neutral: "中性形态",
};

const tacticNameOf = (catalog: TacticMeta[], id: string) =>
  catalog.find((item) => item.id === id)?.name ?? id;

/* ---------- v24 质量分呈现（分项可复算，不隐藏口径） ---------- */

/** 分数悬浮说明：把公式与分项摊开，用户可自行用命中明细复算。 */
function scoreTitleText(
  score: number,
  parts: ScoreParts,
  windowBars?: number | null,
  windowFrom?: string | null,
  formulaVersion?: string,
): string {
  const v2 = formulaVersion === "v2" || typeof parts.saturation_scale === "number";
  const head = v2
    ? `质量分 ${score} = 100 ×（1 − e^(−原始分 ${parts.raw} ÷ ${parts.saturation_scale ?? parts.norm_scale})）`
    : `质量分 ${score} = clamp（原始分 ${parts.raw} ÷ ${parts.norm_scale} × 100, 0, 100）`;
  const lines = [
    head,
    `原始分 = max（看多 ${parts.bullish}，看空 ${parts.bearish}） + 跨类共振 ${parts.resonance} + 中性 ${parts.neutral} × 0.2 − 多空对冲 ${parts.conflict}`,
  ];
  if (v2) {
    lines.push(`＋ 量能确认 ${parts.volume_bonus ?? 0} ＋ 位置 ${parts.position_bonus ?? 0} ＋ 板块共振 ${parts.sector_bonus ?? 0}`);
    lines.push(`量价/位置/板块三项都随信号新鲜度缩放（缩放系数 ${parts.bonus_scale ?? 0}）：没有新鲜信号的票不会靠它们拿分。`);
    lines.push("饱和曲线：100 分是渐近上限，高分不贴顶，强势股之间仍保留区分度。");
  }
  if (typeof windowBars === "number") {
    lines.push(`命中窗口：最近 ${windowBars} 根 K 线${windowFrom ? `（${windowFrom} 起）` : ""}（窗口只决定哪些信号还算数）`);
  }
  lines.push("新鲜度 = 0.5 ^（信号距最新 K 线根数 ÷ 10），与窗口长度无关，故分数可跨标的横向比较。");
  lines.push("纯规则计算（同输入同结果），只描述图形证据的强度与新鲜度，不预测收益。");
  return lines.join("\n");
}

/** v26 AI 复核结论的中文标签（后端枚举值 → 展示文案，未知值如实回退）。 */
export const AI_VERDICT_LABEL: Record<string, string> = {
  bullish: "技术面偏多",
  neutral: "中性",
  bearish: "技术面偏空",
};

export const AI_AGREEMENT_LABEL: Record<string, string> = {
  agree: "认同规则分",
  partial: "部分认同",
  disagree: "不认同规则分",
};

export const AI_RISK_LABEL: Record<string, string> = {
  low: "假突破风险低",
  medium: "假突破风险中",
  high: "假突破风险高",
};

/** JV03（Jev 决策模型接入路线图 2026-09-21）：叙述层状态的中文标签。
 *
 * Jev 只回「是/否、选哪个、打几分」，生成不了 summary/可疑点/接下来盯什么这类文字；
 * 叙述部分仍由 chat 出。chat 没出成时**判定层照样有效**，但要如实告诉用户缺的是哪一块。 */
export const NARRATIVE_STATUS_LABEL: Record<string, string> = {
  ok: "正常",
  unparsable: "模型没按约定格式输出",
  model_unavailable: "模型不可用",
  no_profile: "没有「使用中」的模型方案",
  error: "调用出错",
  skipped: "本轮未调用",
};

/** v2 评分新维度的悬浮说明（量比 / 位置 / 同族合并）。
 * 只依赖这三个字段，故用结构化类型——扫描行与单票详情的 TacticScore 都能直接传入，无需类型硬转。 */
export function scoreExtraTitleText(row: {
  volume_ratio?: number | null;
  position?: number | null;
  merged_families?: string[];
}): string {
  const lines: string[] = [];
  lines.push(
    row.volume_ratio == null
      ? "量能确认：无成交量数据，该项记 0（如实标注，不猜）。"
      : `量能确认：占优方向最强信号当日量比 ${row.volume_ratio.toFixed(2)}（对比近 20 根均量），≤0.7 不加分，≥1.5 加满。`,
  );
  lines.push(
    row.position == null
      ? "位置：区间位置未知，该项记 0。"
      : `位置：最新收盘处于近 60 根区间的 ${(row.position * 100).toFixed(0)}%（0=最低，1=最高）；≤50% 加分、≥90% 扣分（高位追涨风险）。`,
  );
  if (row.merged_families?.length) {
    lines.push(`同族合并：${row.merged_families.join("、")} 族内只按最高一条计分，其余照常展示但不重复计分。`);
  }
  return lines.join("\n");
}

function ScoreBadge({
  score,
  parts,
  bias,
  windowBars,
  windowFrom,
  formulaVersion,
  extraTitle,
}: {
  score: number | undefined;
  parts: ScoreParts | undefined;
  bias: DirectionBias | undefined;
  windowBars?: number | null;
  windowFrom?: string | null;
  /** v2 评分公式版本：决定悬浮说明按饱和曲线还是旧的线性截断解释。 */
  formulaVersion?: string;
  /** 量价/位置/板块的补充说明（v2）。 */
  extraTitle?: string;
}) {
  if (score == null || !parts) return <span className="soft-tag gray">质量分 —</span>;
  const resolved = bias ?? "neutral";
  const title = [scoreTitleText(score, parts, windowBars, windowFrom, formulaVersion), extraTitle]
    .filter(Boolean)
    .join("\n\n");
  return (
    <span className={`tactic-score ${resolved}`} title={title}>
      质量分 {score}
      <i>{BIAS_LABEL[resolved]}</i>
    </span>
  );
}

/** v27 数据质量徽标：把「分数该不该被信任」与分数本身并排展示（方案 §4.4）。 */
function DataQualityTag({ quality }: { quality: DataQuality | undefined }) {
  if (!quality) return null;
  const title = [
    `数据质量：${DATA_QUALITY_LABEL[quality.level]}`,
    `参与计算 ${quality.bars_count} 根 K 线，命中战法最高样本门槛 ${quality.required_bars} 根`,
    `最新 K 线 ${quality.latest_bar_date || "未知"}`,
    quality.missing_fields.length ? `近 60 根缺失字段：${quality.missing_fields.join("、")}` : "近 60 根字段完整",
  ].join("\n");
  return (
    <small className={`data-quality ${quality.level}`} title={title}>
      {DATA_QUALITY_LABEL[quality.level]}
      {quality.missing_fields.length > 0 && " · 缺字段"}
    </small>
  );
}

/** v27：最近一条计分信号（日期 + 距最新几根），回答「这个排名是多久前的信号撑起来的」。 */
function latestSignalText(row: { latest_signal_date?: string; latest_signal_age_bars?: number | null }): string {
  if (!row.latest_signal_date) return "";
  const age = row.latest_signal_age_bars;
  return age == null ? `最近信号 ${row.latest_signal_date}` : `最近信号 ${row.latest_signal_date}（${age} 根前）`;
}

/** v28：单条信号的状态标签（确定性四态；reason 用作悬浮说明，不做二次解读）。 */
function SignalStateTag({ detail }: { detail?: SignalStateDetail }) {
  if (!detail || !detail.state) return null;
  return (
    <span
      className={`soft-tag ${SIGNAL_STATE_TONE[detail.state] ?? "gray"}`}
      title={detail.reason || undefined}
    >
      {SIGNAL_STATE_LABEL[detail.state] ?? detail.state}
    </span>
  );
}

/**
 * v28 §3.6：默认榜单门槛标记。`eligible=false` 时给出可读原因——
 * 行本身仍随结果返回（不静默丢弃），但排序沉底，用户能看出「这条为什么不进榜」。
 */
function GateTag({ row }: { row: ScanRow }) {
  if (!row.ok || row.eligible === undefined) return null;
  if (row.eligible) {
    return (
      <span
        className="soft-tag gray"
        title={`综合分 = 图形分 ${row.tactic_score ?? "—"} × 数据质量因子（${DATA_QUALITY_LABEL[row.data_quality?.level ?? "ok"]}）；rank_score = ${row.rank_score ?? "—"}。后验因子当前不可用，未参与合成。`}
      >
        在榜 · 综合 {row.rank_score ?? row.tactic_score ?? "—"}
      </span>
    );
  }
  const reasons = (row.gating_reasons ?? []).join("；") || "未通过默认榜单门槛";
  return (
    <span
      className={`soft-tag ${row.hard_blocked ? "coral" : "warn"}`}
      title={reasons}
    >
      {row.hard_blocked ? "不可排名" : "未进默认榜"}
    </span>
  );
}

/**
 * JV05：逐票语义复核标签。
 *
 * 三种「没标注」必须视觉可分（《契约》§F1 末表）：
 * - `reviewed` → 绿调「已复核」：送评了、判成立、置信度达标；
 * - `pending_verification` → 警告调：**送评了但没通过**（判存疑/误报，或判成立但置信度低于地板）；
 * - 其余（字段缺失）→ 灰调「未核」：**根本没送出去评**（分片失败 / 超单轮上限 / 单票超预算）。
 *
 * 最后一种尤其不能省：把「没核」渲染成空白，用户会默认它跟「已复核」一样可信。
 * 只在后端确实写过标注（`jev_review_state` 字段存在）时才渲染——`undefined` 与 `null`
 * 要分开处理，前者是「这轮压根没接 Jev」，后者是「接了但这票没送出去」。
 */
export function JevTag({ row }: { row: ScanRow }) {
  if (!row.ok || row.jev_review_state === undefined) return null;
  const title = row.jev_note || undefined;
  if (row.jev_review_state === "reviewed") {
    return (
      <span className="soft-tag mint" title={title}>
        语义已复核{row.jev_confidence != null && ` · 置信 ${row.jev_confidence.toFixed(2)}`}
      </span>
    );
  }
  if (row.jev_review_state === "pending_verification") {
    return (
      <span className="soft-tag warn" title={title}>
        待核验{row.jev_label ? ` · ${row.jev_label}` : ""}
        {row.jev_confidence != null && ` ${row.jev_confidence.toFixed(2)}`}
      </span>
    );
  }
  return (
    <span
      className="soft-tag gray"
      title={title ?? "这一票没有送出去评（不是「通过」）"}
    >
      语义未核
    </span>
  );
}

/**
 * JV05：复核统计行。只在 `enabled=true` 时渲染——关闭时界面与接入前逐字节一致。
 *
 * 三个数字分开列：`已复核 / 待核验 / 未核`。「未核」不是「通过」，所以不给它一个
 * 让人误读成中性的措辞（如「其它」）。
 */
export function JevReviewSummary({ review }: { review: JevReview }) {
  if (!review.enabled) return null;
  const { valid, doubtful, invalid } = review.verdicts;
  return (
    <p className="sort-note jev-review-note">
      <b>Jev 语义复核</b>
      ：已复核 <b>{review.reviewed}</b> · 待核验 <b>{review.pending}</b> · 未核 <b>{review.unannotated}</b>
      {review.total > 0 && (
        <>
          （判定 成立 {valid} / 存疑 {doubtful} / 疑似误报 {invalid}）
        </>
      )}
      {review.shards != null && review.shards > 1 && <> · {review.shards} 个分片</>}
      {review.model && <> · 模型 {review.model}</>}
      {typeof review.elapsed_ms === "number" && <> · 复核耗时 {(review.elapsed_ms / 1000).toFixed(1)}s</>}
      {review.skipped.length > 0 && (
        <> · 未送评 {review.skipped.length} 票（{review.skipped[0]?.note ?? "原因见逐票标注"}）</>
      )}
      <br />
      <span className="filter-hint">
        复核只做标注与分组：不改判定、不参与排序、不删除候选。「未核」表示这一票没有送出去评
        （分片失败 / 超单轮上限 / 单票超预算），<b>不等于形态成立</b>。
      </span>
    </p>
  );
}

/** 逐条命中明细：权重 × 新鲜度 = 贡献，与分项一一对应。
 * v27：追加触发价与失效条件 —— 用户可据此在 K 线图上直接核对「这条信号说了什么」。 */
function HitDetailTable({ detail }: { detail: HitDetail[] }) {
  if (detail.length === 0) return null;
  return (
    <table className="score-detail-table">
      <thead>
        <tr>
          <th>战法</th><th>方向</th><th>信号日</th><th className="num">距最新</th><th>新鲜度</th><th className="num">权重</th><th className="num">贡献</th>
          <th className="num" title="信号当日收盘价（展示精度 2 位）；「—」= 该信号未带价位">触发价</th>
          <th title="v28 信号状态机（确定性判定）：new=尚无后续 K 线可观察；pending=未确认也未失效；confirmed=站稳且量能未萎缩；invalidated=跌回触发日平台且放量">状态</th>
          <th title="什么条件下该信号作废（v27 信号语义，详见 /tactics/catalog）">失效条件</th>
        </tr>
      </thead>
      <tbody>
        {detail.map((item) => (
          <tr key={`${item.tactic_id}-${item.date}`} className={item.family_merged ? "family-merged" : undefined}>
            <td>
              {item.tactic_name}
              <small className="family-tag">{TACTIC_CATEGORY_LABEL[item.category] ?? item.category}</small>
              {item.family_merged && (
                <small
                  className="merged-note"
                  title={`同族合并：${TACTIC_CATEGORY_LABEL[item.category] ?? item.category} 族内已按最高一条计分，本条命中属实但不再重复计分（原始贡献 ${item.raw_contribution ?? "—"}）`}
                >
                  同族合并·不计分
                </small>
              )}
            </td>
            <td className={item.direction}>{DIRECTION_LABEL[item.direction]}</td>
            <td><time>{item.date}</time></td>
            <td className="num">{item.age_bars} 根</td>
            <td>{item.freshness}</td>
            <td className="num">{item.weight}</td>
            <td className="num">{item.contribution}</td>
            <td className="num">{item.trigger_price == null ? "—" : item.trigger_price}</td>
            <td className="signal-state-cell">
              <SignalStateTag detail={item.signal_state_detail} />
            </td>
            <td className="invalidation-cell">
              {item.invalidation_rule || "—"}
              {item.cooldown_bars != null && (
                <small className="family-tag" title="该形态在这么多根 K 线内视为同一次触发（不重复计分）">
                  冷却 {item.cooldown_bars} 根
                </small>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ---------- 信号标注 K 线图（lightweight-charts，与 KLineCard 同库同口径） ---------- */

function readMarketColors(): { up: string; down: string } {
  const style = getComputedStyle(document.body);
  return {
    up: style.getPropertyValue("--mkt-up").trim() || "#e2726a",
    down: style.getPropertyValue("--mkt-down").trim() || "#5fb389",
  };
}

function movingAverage(bars: TacticBar[], window: number): { time: Time; value: number }[] {
  const out: { time: Time; value: number }[] = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i]!.c;
    if (i >= window - 1) {
      if (i >= window) sum -= bars[i - window]!.c;
      out.push({ time: bars[i]!.time as Time, value: sum / window });
    }
  }
  return out;
}

function TacticsChart({ bars, signals }: { bars: TacticBar[]; signals: TacticSignal[] }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const prefs = useUiPrefs();

  useEffect(() => {
    const host = hostRef.current;
    if (!host || bars.length === 0) return;
    const colors = readMarketColors();
    const chart = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "rgba(220,232,228,0.6)",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.04)" },
        horzLines: { color: "rgba(255,255,255,0.04)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { borderColor: "rgba(255,255,255,0.08)" },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.08)" },
    });
    chartRef.current = chart;

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: colors.up,
      downColor: colors.down,
      borderVisible: false,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    });
    candleRef.current = candle;
    candle.setData(bars.map((bar) => ({ time: bar.time as Time, open: bar.o, high: bar.h, low: bar.l, close: bar.c })));

    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData(bars.map((bar) => ({
      time: bar.time as Time,
      value: bar.v,
      color: bar.c >= bar.o ? colors.up : colors.down,
    })));

    const ma5 = chart.addSeries(LineSeries, { color: "#7dc4ff", lineWidth: 1 });
    const ma20 = chart.addSeries(LineSeries, { color: "#b9e6ca", lineWidth: 1 });
    ma5.setData(movingAverage(bars, 5));
    ma20.setData(movingAverage(bars, 20));

    // 信号标注：金叉等看多 ▲ 下方红；死叉等看空 ▼ 上方绿；中性 ● 灰。细节看下方信号列表。
    const byDate = new Map<string, TacticSignal[]>();
    for (const signal of signals) {
      const list = byDate.get(signal.date) ?? [];
      list.push(signal);
      byDate.set(signal.date, list);
    }
    const markers: SeriesMarker<Time>[] = [];
    for (const [date, group] of byDate) {
      const bullish = group.some((item) => item.direction === "bullish");
      const bearish = group.some((item) => item.direction === "bearish");
      const names = group.map((item) => item.tactic_name).join("、");
      if (bullish) markers.push({ time: date as Time, position: "belowBar", color: colors.up, shape: "arrowUp", text: names });
      else if (bearish) markers.push({ time: date as Time, position: "aboveBar", color: colors.down, shape: "arrowDown", text: names });
      else markers.push({ time: date as Time, position: "inBar", color: "#9aa8a4", shape: "circle", text: names });
    }
    markers.sort((a, b) => String(a.time).localeCompare(String(b.time)));
    createSeriesMarkers(candle, markers);

    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
    };
  }, [bars, signals, prefs.marketColors]);

  return <div ref={hostRef} className="tactics-chart" />;
}

/* ---------- 主页面 ---------- */

const SOURCE_LABEL: Record<string, string> = {
  watchlist: "观察清单",
  holdings: "我的持仓",
  manual: "手动列表",
};

/** 结果行的公共操作与展示（扫描 / 市场雷达 / 板块分组共用，口径一致）。 */
export interface RowActions {
  catalog: TacticMeta[];
  busy: boolean;
  onLoadSignals: (symbol: string) => void;
  onSaveWatch: (symbol: string, name: string, note: string) => Promise<boolean>;
  onOpenStockReport?: (symbol: string, context?: { question?: string; sourceLabel?: string; scanId?: string }) => void;
  /** v26：加入投资模块自选（POST /holdings status=watchlist）；已在自选中时禁用。 */
  onAddToWatchlist?: (symbol: string, name: string) => Promise<boolean>;
  /** 已在持仓或自选中的标的代码（用于「已加入」态，避免重复登记）。 */
  trackedInstruments?: string[];
  /** v26：手动触发 AI 复核（点按钮才调模型）。 */
  onRunAiReview?: (symbol: string, sectorCode: string | null) => void;
  /** 正在复核的标的代码（null = 空闲）。 */
  aiReviewBusy?: string | null;
  /** AI 是否接通（未接通时禁用按钮并说明）。 */
  aiReady?: boolean;
}

function ScanRowsTable({
  rows,
  startRank = 1,
  showSector = false,
  actions,
}: {
  rows: ScanRow[];
  /** 分组展示时的组内排名起始值（跨板块排序时从 1 开始）。 */
  startRank?: number;
  /** 是否展示板块归属（板块扫描的扁平视图用）。 */
  showSector?: boolean;
  actions: RowActions;
}) {
  const {
    catalog, busy, onLoadSignals, onSaveWatch, onOpenStockReport,
    onAddToWatchlist, trackedInstruments, onRunAiReview, aiReviewBusy, aiReady,
  } = actions;
  return (
    <table className="kv-table">
      <tbody>
        {rows.map((row, index) => {
          const tracked = trackedInstruments?.includes(row.symbol) ?? false;
          return (
            <tr key={`${row.symbol}-${index}`} className={row.ok && row.hit_tactics.length ? "hit" : ""}>
              <td>
                <span className="rank-no">#{startRank + index}</span>
                <button className="text-button" onClick={() => onLoadSignals(row.symbol)}>{row.symbol}</button>
                <small>{row.name}</small>
                <ScoreBadge
                  score={row.tactic_score}
                  parts={row.score_parts}
                  bias={row.direction_bias}
                  windowBars={row.signal_window_bars}
                  windowFrom={row.signal_window_from}
                  formulaVersion={row.score_formula_version}
                  extraTitle={scoreExtraTitleText(row)}
                />
                {row.trend_score != null && (
                  <small className="trend-score" title={row.trend_parts ? `快照预筛（候选粗筛参考，不参与排名）：涨幅 ${row.trend_parts.cp} / 换手 ${row.trend_parts.tr} / 成交额分位 ${row.trend_parts.amt}` : undefined}>
                    趋势分 {row.trend_score}
                  </small>
                )}
                {/* v27：最新信号日期/年龄 + 数据质量 —— 排序口径必须公开，用户才能判断该不该信 */}
                {row.ok && latestSignalText(row) && (
                  <small
                    className="latest-signal"
                    title="最近一条**计分**信号的日期（不是窗口下界）；括号内为距最新 K 线的根数"
                  >
                    {latestSignalText(row)}
                  </small>
                )}
                {row.ok && <DataQualityTag quality={row.data_quality} />}
                {/* v28：主信号状态 + 默认榜单门槛（原因悬浮可读；不静默隐藏被挡下的行） */}
                {row.ok && <SignalStateTag detail={row.primary_signal_state} />}
                {row.ok && <GateTag row={row} />}
                {/* JV05：语义复核标签（Jev 关闭时后端不写该字段 → 此处不渲染任何东西） */}
                {row.ok && <JevTag row={row} />}
                {showSector && row.sector_name && <small className="sector-tag">{row.sector_name}</small>}
              </td>
              <td>
                {!row.ok ? (
                  <span className="probe-note err">{row.error}</span>
                ) : row.hit_tactics.length === 0 ? (
                  <span className="soft-tag gray">窗口内无信号</span>
                ) : (
                  <span className="tactic-badges">
                    {row.hit_detail && row.hit_detail.length > 0
                      ? row.hit_detail.map((item) => (
                          <span
                            key={`${item.tactic_id}-${item.date}`}
                            className={`tactic-badge ${item.direction}${item.family_merged ? " merged" : ""}`}
                            title={
                              item.family_merged
                                ? `${item.tactic_name} · 信号日 ${item.date} · 同族合并，本条不计分（原始贡献 ${item.raw_contribution ?? "—"}）`
                                : `${item.tactic_name} · 信号日 ${item.date} · 权重 ${item.weight} × 新鲜度 ${item.freshness} = 贡献 ${item.contribution}`
                            }
                          >
                            {item.tactic_name}
                            {item.family_merged && <i className="merged-dot" aria-hidden="true" />}
                          </span>
                        ))
                      : row.hit_tactics.map((id) => (
                          <span key={id} className="tactic-badge">{tacticNameOf(catalog, id)}</span>
                        ))}
                  </span>
                )}
                {row.ok && row.insufficient_tactics && row.insufficient_tactics.length > 0 && (
                  <small className="insufficient-note">
                    历史不足（K 线 &lt; 该战法最小样本）：{row.insufficient_tactics.map((id) => tacticNameOf(catalog, id)).join("、")}
                  </small>
                )}
              </td>
              <td>
                {row.ok && row.hit_detail && row.hit_detail.length > 0 ? (
                  <details className="score-detail">
                    <summary>分项</summary>
                    <HitDetailTable detail={row.hit_detail} />
                  </details>
                ) : (
                  <span className="filter-hint">—</span>
                )}
              </td>
              <td>
                {onOpenStockReport && (
                  <button className="ghost-btn" title="跳转研究工作台并预填该标的（带入当前扫描上下文）" onClick={() => onOpenStockReport(row.symbol, { sourceLabel: "战法雷达", question: "该标的当前战法信号状态如何？信号与技术状态能否支持短期确认？（技术信号不替代基本面与长期价值判断）" })}>AI 报告</button>
                )}
                <button className="ghost-btn" onClick={() => onLoadSignals(row.symbol)}>详情</button>
                <button
                  className="ghost-btn"
                  disabled={busy}
                  title="加入战法雷达的观察清单（本机记录，参与下次扫描）"
                  onClick={() => void onSaveWatch(row.symbol, row.name, "")}
                >
                  +观察
                </button>
                {onAddToWatchlist && (
                  <button
                    className="ghost-btn"
                    disabled={busy || tracked}
                    title={tracked ? "该标的已在投资模块的持仓/自选中" : "加入投资模块的自选（与投资页共用同一份清单，便于长期研究）"}
                    onClick={() => void onAddToWatchlist(row.symbol, row.name)}
                  >
                    {tracked ? "已自选" : "+自选"}
                  </button>
                )}
                {onRunAiReview && (
                  <button
                    className="ghost-btn"
                    disabled={busy || !aiReady || aiReviewBusy === row.symbol}
                    title={aiReady ? "手动触发一次 AI 复核：让模型判断形态可靠度与可疑点（规则分与 AI 分并列展示，不互相覆盖）" : "AI 未接通：请先在 设置 → 模型配置 添加方案并设为「使用中」"}
                    onClick={() => onRunAiReview(row.symbol, row.sector_code ?? null)}
                  >
                    {aiReviewBusy === row.symbol ? "复核中…" : "AI 复核"}
                  </button>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** v27 排序键（方案 §4.4）：不再只按图形分，用户可按「新鲜信号 / 数据质量」重排。
 * 注意：切换的是**前端展示顺序**，不改变后端返回的排名口径（每行仍标注 rank-no）。 */
type ScanSortKey = "rank" | "fresh" | "quality";

const SCAN_SORT_LABEL: Record<ScanSortKey, string> = {
  rank: "图形分",
  fresh: "新鲜信号",
  quality: "数据质量",
};

function sortScanRows(rows: ScanRow[], key: ScanSortKey): ScanRow[] {
  const copy = [...rows];
  /** v28：并列时按综合分（图形分 × 数据质量因子）排；旧数据没有 rank_score 时回落图形分。 */
  const rankOf = (row: ScanRow): number => row.rank_score ?? row.tactic_score ?? -1;
  if (key === "fresh") {
    copy.sort((a, b) => {
      const av = a.latest_signal_date ?? "";
      const bv = b.latest_signal_date ?? "";
      if (av !== bv) return bv.localeCompare(av);       // 日期越新越靠前
      return rankOf(b) - rankOf(a);
    });
    return copy;
  }
  if (key === "quality") {
    const levelRank: Record<string, number> = { ok: 3, thin: 2, insufficient: 1 };
    copy.sort((a, b) => {
      const av = levelRank[a.data_quality?.level ?? ""] ?? 0;
      const bv = levelRank[b.data_quality?.level ?? ""] ?? 0;
      if (av !== bv) return bv - av;
      return rankOf(b) - rankOf(a);
    });
    return copy;
  }
  return copy;                                          // rank：保持后端顺序（rank_score → eligible 门槛）
}

export function ScanResultTable({
  result,
  actions,
}: {
  /** 两条扫描路径都带 `jev_review`：任务化的市场扫描（`/tactics/scan-market`）与日常扫描
   *  （`/tactics/scan`，2026-09-22 补接——它走自己的扫描循环，原先漏了）。 */
  result: ScanResult & { jev_review?: JevReview };
  actions: RowActions;
}) {
  const windowBars = typeof result.recent_bars === "number" ? result.recent_bars : null;
  const [sortKey, setSortKey] = useState<ScanSortKey>("rank");
  const rows = useMemo(() => sortScanRows(result.results, sortKey), [result.results, sortKey]);
  /**
   * JV05：「待核验」折叠。只在复核**确实跑过**（`enabled=true`）时才折叠——
   * 关闭时 `main` 就是原样全部行，界面与接入前逐字节一致。
   * 折叠的只有 `pending_verification`（送评未通过）；`unannotated`（没送出去评）
   * **留在主列表**并挂灰标签——它既不是「通过」也不该被藏起来。
   */
  const jevReview = result.jev_review;
  const { mainRows, pendingRows } = useMemo(() => {
    if (!jevReview?.enabled) return { mainRows: rows, pendingRows: [] as ScanRow[] };
    const pending = rows.filter((row) => row.jev_review_state === "pending_verification");
    const pendingSet = new Set(pending);
    return { mainRows: rows.filter((row) => !pendingSet.has(row)), pendingRows: pending };
  }, [rows, jevReview?.enabled]);
  // D03：失败票分布（多少票取数失败 + 原因分布），与 summary.failures 同源（results 的 ok=false 行）。
  const failureSummary = useMemo(() => {
    const failed = result.results.filter((row) => row.ok === false);
    const byReason = new Map<string, number>();
    for (const row of failed) {
      const reason = row.error ?? "未知";
      byReason.set(reason, (byReason.get(reason) ?? 0) + 1);
    }
    return { count: failed.length, reasons: [...byReason.entries()] };
  }, [result.results]);
  return (
    <div className="tactics-scan-result">
      <p className="sort-note">
        扫描 {result.scanned} 只 · {formatDate(result.generated_at)}
        {typeof result.elapsed_ms === "number" && <> · 耗时 {(result.elapsed_ms / 1000).toFixed(1)}s</>}
        {windowBars != null && (
          <> · 命中窗口：最近 <b>{windowBars}</b> 根 K 线（按 K 线日期计，不是信号条数）</>
        )}
        {result.weight_version && <> · 权重版本 {result.weight_version}</>}
        {result.score_formula_version && <> · 评分 {result.score_formula_version}</>}
        {failureSummary.count > 0 && (
          <>
            {" "}· <b>取数失败 {failureSummary.count} 票</b>
            {failureSummary.reasons.map(([reason, count], index) => (
              <span key={index}> · {reason}×{count}</span>
            ))}
          </>
        )}
        {result.gate_version && <> · 榜单门槛 {result.gate_version}</>}
      </p>
      {jevReview && <JevReviewSummary review={jevReview} />}
      <p className="sort-basis">
        排名口径：<b>综合分 rank_score</b> = 图形质量分 × 数据质量因子（图形分 = 形态权重 × 新鲜度（同族只计最高一条）
        ＋ 同向跨类共振 − 多空对冲 ＋ 量能确认 ＋ 位置 ＋ 板块共振，0–100 饱和曲线不贴顶，纯规则可复算）；
        数据齐全时因子为 1，与图形分同序。K 线字段缺失 / 日期异常的行<b>硬性不可排名</b>，样本不足或主信号已失效的不进默认榜单（仍随结果返回，原因见行内标注）。
        新鲜度按固定半衰期 10 根 K 线折半，分数与窗口长度无关，可跨标的横向比较。
        点每行「分项」看逐条命中如何累加成总分（含触发价、失效条件与 v28 信号状态）。
        趋势分仅是 mode=all 的候选粗筛参考，不参与最终排名。
        <b>历史后验</b>（同市场状态下的历史超额）属第二期，本期不出数，因此不显示 0 分冒充。
      </p>
      <div className="sort-switch" role="group" aria-label="展示排序">
        <span className="filter-hint">展示排序：</span>
        {(Object.keys(SCAN_SORT_LABEL) as ScanSortKey[]).map((key) => (
          <button
            key={key}
            type="button"
            className={`tag-button ${sortKey === key ? "active" : ""}`}
            title={
              key === "rank"
                ? "按后端返回的图形质量分排名（默认；也是 score_parts 的可复算口径）"
                : key === "fresh"
                  ? "按最近一条计分信号的日期由新到旧排（回答「这个分数是不是老信号撑的」）"
                  : "按数据质量由好到差排（样本充足 → 偏薄 → 不足），并列时按图形分"
            }
            onClick={() => setSortKey(key)}
          >
            {SCAN_SORT_LABEL[key]}
          </button>
        ))}
      </div>
      <ScanRowsTable rows={mainRows} actions={actions} />
      {pendingRows.length > 0 && (
        <details className="score-detail jev-pending-fold">
          <summary>
            待核验（{pendingRows.length} 只，默认折叠）——语义复核认为形态存疑或疑似误报；
            <b>它们仍在本轮扫描结果里</b>，只是被折叠，不参与默认榜单展示
          </summary>
          <ScanRowsTable rows={pendingRows} actions={actions} />
        </details>
      )}
    </div>
  );
}

/** v26：板块扫描结果——先按板块分组（含板块共振），再给一份跨板块总排名。 */
function SectorScanView({
  result,
  actions,
}: {
  result: MarketScanResult;
  actions: RowActions;
}) {
  const blocks = result.sectors ?? [];
  const bySector = new Map<string, ScanRow[]>();
  for (const row of result.results) {
    const key = row.sector_code ?? "";
    const list = bySector.get(key) ?? [];
    list.push(row);
    bySector.set(key, list);
  }
  return (
    <div className="tactics-sector-result">
      {blocks.map((block) => {
        const rows = (bySector.get(block.code) ?? []).filter((row) => row.ok);
        // v28：板块内按综合分（图形分 × 数据质量因子）排；旧数据回落图形分。
        rows.sort((a, b) => (b.rank_score ?? b.tactic_score ?? -1) - (a.rank_score ?? a.tactic_score ?? -1));
        return (
          <div key={block.code} className={`sector-block ${block.ok ? "" : "failed"}`}>
            <div className="sector-block-head">
              <b>{block.name}</b>
              <span className="filter-hint">{block.code}</span>
              {block.change_pct != null && (
                <span className={`sector-change ${block.change_pct >= 0 ? "up" : "down"}`}>
                  {block.change_pct >= 0 ? "+" : ""}{Number(block.change_pct).toFixed(2)}%
                </span>
              )}
              {block.member_count != null && <span className="filter-hint">{block.member_count} 只成分</span>}
              {!block.ok ? (
                <span className="probe-note err">{block.error}</span>
              ) : (
                <>
                  <span className="filter-hint">扫描 {block.scanned} 只{block.failed ? ` · 失败 ${block.failed}` : ""}</span>
                  <span className="filter-hint">
                    偏多 {block.bullish_count ?? 0} · 偏空 {block.bearish_count ?? 0}
                  </span>
                  {block.resonance ? (
                    <span
                      className={`sector-resonance ${block.resonance.direction}`}
                      title={`板块内同向占比 ${(block.resonance.ratio * 100).toFixed(0)}%（${block.resonance.count}/${block.resonance.sample}），达阈值 ${block.resonance.threshold} 且样本 ≥${block.resonance.min_sample} 才成立；成立时给板块内同向个股加板块共振分，反向个股不加`}
                    >
                      板块共振 {BIAS_LABEL[block.resonance.direction]} {(block.resonance.ratio * 100).toFixed(0)}%
                    </span>
                  ) : (
                    <span className="soft-tag gray" title="板块内同向占比未达阈值或样本不足 4 只，故不给任何个股加板块分（宁可不判，也不凭空加分）">
                      无板块共振
                    </span>
                  )}
                  {block.leader_name && <span className="filter-hint">领涨 {block.leader_name}</span>}
                </>
              )}
            </div>
            {rows.length > 0 ? (
              <ScanRowsTable rows={rows} startRank={1} actions={actions} />
            ) : (
              <p className="scheme-empty">{block.ok ? "该板块没有成功取到成分股行情。" : "该板块取数失败，未影响其他板块。"}</p>
            )}
          </div>
        );
      })}
      <details className="score-detail">
        <summary>跨板块总排名（{result.results.length} 只）</summary>
        <ScanRowsTable rows={result.results} showSector actions={actions} />
      </details>
    </div>
  );
}

/* ---------- v26 AI 复核面板（手动触发，规则分与 AI 分并列） ---------- */

export function AiReviewCard({
  record,
  onDelete,
}: {
  record: AiReviewRecord;
  onDelete?: (reviewId: string) => Promise<void>;
}) {
  const review = record.payload?.review;
  const delta = record.rule_score != null && record.ai_score != null
    ? record.ai_score - record.rule_score
    : null;
  // JV03：`verdict`（技术面倾向）只能来自叙述层（chat）——Jev 的三题里没有这一题。
  // 叙述缺席时 review.verdict 是合并时的中性默认值，**不是任何模型的结论**，不能当结论展示。
  const hasNarrative = !record.payload?.narrative_status || record.payload.narrative_status === "ok";
  const narrativeMissing = record.payload?.narrative_status != null && record.payload.narrative_status !== "ok";
  return (
    <div className="ai-review-card">
      <div className="ai-review-head">
        <b>{record.symbol}</b>
        <small>{record.name}</small>
        <span className="soft-tag" title="规则分（确定性排序依据） vs AI 分（可靠度复核）——两者口径不同，分歧本身是有价值的信号">
          规则 {record.rule_score ?? "—"} ／ AI {record.ai_score ?? "—"}
          {delta != null && (
            <i className={delta >= 0 ? "up" : "down"}>
              {delta >= 0 ? "+" : ""}{delta.toFixed(0)}
            </i>
          )}
        </span>
        {record.engine && (
          <span
            className="soft-tag"
            title={
              record.engine === "jev"
                ? "判定层由 Jev（TypeSafe System One）给出：三题类型化答案，可复算、无 JSON 解析风险"
                : "判定层由 chat 模型给出（Jev 未启用或不可用）"
            }
          >
            判定 {record.engine === "jev" ? "Jev" : "chat"}
            {record.engine === "jev" && record.confidence != null && ` · 置信 ${record.confidence.toFixed(2)}`}
          </span>
        )}
        {review && (
          <>
            {hasNarrative && (
              <span className={`tactic-badge ${review.verdict === "bullish" ? "bullish" : review.verdict === "bearish" ? "bearish" : "neutral"}`}>
                {AI_VERDICT_LABEL[review.verdict] ?? review.verdict}
              </span>
            )}
            <span className="soft-tag">{AI_AGREEMENT_LABEL[review.agreement] ?? review.agreement}</span>
            <span className={`soft-tag ${review.fake_breakout_risk === "high" ? "warn" : ""}`}>
              {AI_RISK_LABEL[review.fake_breakout_risk] ?? review.fake_breakout_risk}
            </span>
          </>
        )}
        <time>{formatDate(record.created_at)}</time>
        {onDelete && (
          <button className="ghost-btn danger" onClick={() => void onDelete(record.review_id)}>删除</button>
        )}
      </div>
      {review && hasNarrative && (
        <>
          <p className="ai-review-summary">{review.summary}</p>
          {review.rule_score_comment && (
            <p className="filter-hint">规则分评价：{review.rule_score_comment}</p>
          )}
          <div className="ai-review-lists">
            {review.strengths.length > 0 && (
              <div>
                <div className="ai-review-list-label">站得住的地方</div>
                <ul>{review.strengths.map((item) => <li key={item}>{item}</li>)}</ul>
              </div>
            )}
            {review.concerns.length > 0 && (
              <div>
                <div className="ai-review-list-label">可疑点</div>
                <ul>{review.concerns.map((item) => <li key={item}>{item}</li>)}</ul>
              </div>
            )}
            {review.watch_points.length > 0 && (
              <div>
                <div className="ai-review-list-label">接下来盯什么</div>
                <ul>{review.watch_points.map((item) => <li key={item}>{item}</li>)}</ul>
              </div>
            )}
          </div>
          {(review.key_levels?.support != null || review.key_levels?.resistance != null) && (
            <p className="filter-hint">
              关键价位：支撑 {review.key_levels.support ?? "—"} · 压力 {review.key_levels.resistance ?? "—"}（模型由给定 K 线推出，非投资建议）
            </p>
          )}
        </>
      )}
      {narrativeMissing && (
        <p className="filter-hint">
          本轮没有叙述部分（{NARRATIVE_STATUS_LABEL[record.payload?.narrative_status ?? ""] ?? record.payload?.narrative_status}）：
          判定层结论仍然有效，但缺少「站得住的地方／可疑点／接下来盯什么」这类文字说明。
        </p>
      )}
      <p className="filter-hint">
        模型 {record.model}
        {record.payload?.latency_ms != null && ` · ${record.payload.latency_ms} ms`}
        {record.payload?.prompt_version && ` · 提示词 v${record.payload.prompt_version}`}
      </p>
    </div>
  );
}

export function TacticsPage({
  isDemo,
  pluginEnabled,
  onEnablePlugin,
  active,
  onCreateHolding,
  tacticsHandoff,
  onOpenStockReport,
  trackedInstruments,
  aiReady,
}: Props) {
  // B2：战法领域数据自取（回调 props 已清零；别名对齐既有局部命名，页面主体零改动）。
  const client = useMemo(() => createCoreClient(), []);
  const {
    tacticsCatalog: catalog, tacticsScan: scanResult, tacticsScanBusy: scanBusy,
    tacticsMarketScan: marketResult, tacticsMarketBusy: marketBusy, tacticsMarketProgress: marketProgress, stopMarketScan, tacticsWatchlist: watchlist,
    tacticsSignals: signals, tacticsSignalsLoading: signalsLoading, tacticsNotes: notes,
    tacticsSectors: sectors, tacticsSectorsLoading: sectorsLoading, tacticsAiReviews: aiReviews,
    tacticsAiReviewBusy: aiReviewBusy,
    runTacticsScan: doScan, runTacticsMarketScan: doMarketScan, runTacticsSectorScan: doSectorScan,
    saveTacticsWatch: onSaveWatch, deleteTacticsWatch: onDeleteWatch, loadTacticsSignals: doLoadSignals,
    addTacticsNote: doAddNote, loadTacticsSectors: doLoadSectors,
    runTacticsAiReview: doRunAiReview, deleteTacticsAiReview: doDeleteAiReview, addToWatchlist: doAddToWatchlist,
  } = useTactics(client, active, aiReady, onCreateHolding);
  const onScan = (sources: string[], symbols: string[]) => void doScan(sources, symbols);
  const onScanMarket = (boards: string[], perBoard: number, maxSymbols: number) => void doMarketScan(boards, perBoard, maxSymbols);
  const onScanMarketAll = (topSymbols: number, segments: string[], priceMin: number | null, priceMax: number | null, tacticIds: string[] | null) => void doMarketScan([], 0, 0, "all", topSymbols, segments, priceMin, priceMax, tacticIds);
  const onLoadSignals = (symbol: string) => void doLoadSignals(symbol);
  const onAddNote = (symbol: string, content: string) => doAddNote(symbol, content);
  const onLoadSectors = () => void doLoadSectors();
  const onScanMarketSectors = (codes: string[], perSector: number, resonance: boolean) => void doSectorScan(codes, perSector, resonance);
  const onRunAiReview = (symbol: string, sectorCode: string | null, question: string) => doRunAiReview(symbol, sectorCode, question);
  const onDeleteAiReview = (reviewId: string) => doDeleteAiReview(reviewId);
  const onAddToWatchlist = (symbol: string, name: string) => doAddToWatchlist(symbol, name);
  const [sources, setSources] = useState<string[]>(["watchlist", "holdings"]);
  const [manualInput, setManualInput] = useState("");
  const [marketBoards, setMarketBoards] = useState<string[]>(["turnover", "gainers"]);
  const [allTopSymbols, setAllTopSymbols] = useState(40);
  const [marketSegments, setMarketSegments] = useState<string[]>(["main", "chinext", "star"]);
  const [priceMinInput, setPriceMinInput] = useState("");
  const [priceMaxInput, setPriceMaxInput] = useState("");
  // 战法选择：null = 全部；展开面板按 tag 多选（市场雷达跑引擎的战法子集）。
  const [tacticsOpen, setTacticsOpen] = useState(false);
  const [selectedTacticIds, setSelectedTacticIds] = useState<string[] | null>(null);
  // 页内子视图：战法太长拆三个子页（扫描 / 市场雷达 / 观察清单），单票详情只在扫描页渲染。
  const [subView, setSubView] = useState<"scan" | "market" | "watch">("scan");
  // v33 D04/D06：新交接到达 → 强制切到「战法扫描」并聚焦接收面板；
  // 以 requestId 为依赖（同一代码重复跳转也生成新 id），消费后保留回执不反复抢焦点。
  const [consumedHandoff, setConsumedHandoff] = useState<{ requestId: string; receivedAt: string } | null>(null);
  const handoffPanelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!tacticsHandoff) return;
    setSubView("scan");
    setConsumedHandoff({ requestId: tacticsHandoff.requestId, receivedAt: new Date().toLocaleTimeString() });
    const timer = window.setTimeout(() => {
      handoffPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [tacticsHandoff?.requestId]);
  const [addSymbol, setAddSymbol] = useState("");
  const [addName, setAddName] = useState("");
  const [addNote, setAddNote] = useState("");
  const [noteDraft, setNoteDraft] = useState("");
  const [busy, setBusy] = useState(false);
  /* —— v26 板块扫描 / AI 复核 —— */
  const [selectedSectorCodes, setSelectedSectorCodes] = useState<string[]>([]);
  const [perSector, setPerSector] = useState(20);
  const [sectorResonance, setSectorResonance] = useState(true);
  const [sectorPickOpen, setSectorPickOpen] = useState(false);
  const [sectorQuery, setSectorQuery] = useState("");
  // AI 复核：手动触发，弹面板填「关注点」后才调用模型（不做任何自动复核）。
  const [aiReviewSymbol, setAiReviewSymbol] = useState<string | null>(null);
  const [aiReviewSector, setAiReviewSector] = useState<string | null>(null);
  const [aiReviewQuestion, setAiReviewQuestion] = useState("");

  const selectedSymbol = signals?.symbol ?? null;

  /** 板块清单按关键字过滤（名称或代码），便于在 ~49 个行业里快速定位。 */
  const filteredSectors = useMemo(() => {
    const keyword = sectorQuery.trim();
    if (!keyword) return sectors;
    return sectors.filter(
      (item) => item.name.includes(keyword) || item.code.toLowerCase().includes(keyword.toLowerCase()),
    );
  }, [sectors, sectorQuery]);

  const aiReviewsForSymbol = useMemo(
    () => (aiReviewSymbol ? aiReviews.filter((item) => item.symbol === aiReviewSymbol) : []),
    [aiReviews, aiReviewSymbol],
  );

  /** 结果行公共操作（扫描 / 市场雷达 / 板块分组三处共用同一口径）。 */
  const rowActions: RowActions = {
    catalog,
    busy,
    onLoadSignals: (symbol) => { onLoadSignals(symbol); setSubView("scan"); },
    onSaveWatch,
    onOpenStockReport,
    onAddToWatchlist,
    trackedInstruments,
    onRunAiReview: (symbol, sectorCode) => {
      setAiReviewSymbol(symbol);
      setAiReviewQuestion("");
      setAiReviewSector(sectorCode);
    },
    aiReviewBusy,
    aiReady,
  };

  /** 价格输入 → null（不限）或非负数；非法输入如实按不限处理。 */
  function parsePriceInput(value: string): number | null {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
  }

  // 未选票时默认聚焦观察清单第一只。
  useEffect(() => {
    if (!signals && !signalsLoading && watchlist.length > 0) onLoadSignals(watchlist[0]!.symbol);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlist]);

  function runScan() {
    const symbols = manualInput.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean);
    onScan(sources, symbols);
  }

  async function submitWatch() {
    const symbol = addSymbol.trim();
    if (!symbol || busy) return;
    setBusy(true);
    const ok = await onSaveWatch(symbol, addName.trim(), addNote.trim());
    setBusy(false);
    if (ok) {
      setAddSymbol("");
      setAddName("");
      setAddNote("");
    }
  }

  async function submitNote() {
    if (!selectedSymbol || !noteDraft.trim() || busy) return;
    setBusy(true);
    const ok = await onAddNote(selectedSymbol, noteDraft.trim());
    setBusy(false);
    if (ok) setNoteDraft("");
  }

  const sortedSignals = useMemo(
    () => [...(signals?.signals ?? [])].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 60),
    [signals],
  );

  /** 目录按分类分桶（TACTIC_CATEGORY_LABEL 顺序，未知分类归「其他」）。 */
  const categorizedCatalog = useMemo(() => {
    const order = [...Object.keys(TACTIC_CATEGORY_LABEL), "other"];
    const buckets = new Map<string, TacticMeta[]>();
    for (const tactic of catalog) {
      const key = tactic.category && TACTIC_CATEGORY_LABEL[tactic.category] ? tactic.category : "other";
      const bucket = buckets.get(key) ?? [];
      bucket.push(tactic);
      buckets.set(key, bucket);
    }
    return order.filter((key) => buckets.has(key)).map((key) => ({
      key,
      label: TACTIC_CATEGORY_LABEL[key] ?? "其他",
      tactics: buckets.get(key) ?? [],
    }));
  }, [catalog]);

  if (isDemo) {
    return (
      <section className="page-view tactics-page">
        <header className="settings-heading"><div><span className="kicker">独立应用 · 战法插件</span><h2>战法雷达</h2></div></header>
        <div className="chart-pending">
          <strong>演示模式不可用</strong>
          <span>战法识别基于真实 K 线计算，演示数据不参与。连接本地 Core 后使用。</span>
        </div>
      </section>
    );
  }

  return (
    <section className="page-view tactics-page">
      <header className="settings-heading">
        <div><span className="kicker">独立应用 · 战法插件</span><h2>战法雷达</h2></div>
        <span>{catalog.length} 个战法 · 信号只描述图形事实，不构成买卖建议</span>
      </header>

      {!pluginEnabled ? (
        <div className="chart-pending">
          <strong>战法插件未启用</strong>
          <span>启用 official.stock-tactics 后开始扫描与观察。</span>
          <button className="primary-button mt-10" onClick={onEnablePlugin}>启用战法雷达 <span>→</span></button>
          {/* D05：交接数据保留——启用后回到本页即可按同一交接开始扫描 */}
          {tacticsHandoff && tacticsHandoff.pool.length > 0 && (
            <div className="key-note mt-10" data-testid="handoff-held">
              已接收交接「{tacticsHandoff.sourceLabel}」· {tacticsHandoff.pool.length} 只候选待扫描（插件启用后生效）
            </div>
          )}
        </div>
      ) : (
        <div className="tactics-layout">
          {/* 子页切换（页面太长拆三个子视图） */}
          <div className="pref-row" role="tablist" aria-label="战法雷达子页">
            {([["scan", "战法扫描"], ["market", "市场雷达"], ["watch", "观察清单"]] as const).map(([key, label]) => (
              <button
                key={key}
                role="tab"
                aria-selected={subView === key}
                className={`tag-button ${subView === key ? "active" : ""}`}
                onClick={() => setSubView(key)}
              >
                {label}
              </button>
            ))}
          </div>

          {/* 扫描面板 */}
          {subView === "scan" && (
          <div className="dl-block">
            <h4>找股票 · 战法扫描</h4>
            <p>按观察清单 / 持仓 / 手动列表逐票识别战法信号（逐票拉取日 K，公开接口限速，扫描需要几秒）。</p>
            {tacticsHandoff && tacticsHandoff.pool.length > 0 && (
              <div className="key-note wb-handoff" ref={handoffPanelRef} data-testid="tactics-handoff">
                <b>{tacticsHandoff.sourceLabel}（{tacticsHandoff.pool.length} 只候选）</b>
                {consumedHandoff?.requestId === tacticsHandoff.requestId && (
                  <span className="soft-tag mint" title="本次交接已接收（按唯一 requestId 回执，重复跳转会生成新交接）">
                    已接收 · {consumedHandoff.receivedAt}
                  </span>
                )}
                {tacticsHandoff.topic && <p className="tactics-handoff-meta">主题：{tacticsHandoff.topic}</p>}
                {tacticsHandoff.question && <p className="tactics-handoff-meta">研究问题：{tacticsHandoff.question}</p>}
                <div className="pref-row mt-10">
                  {tacticsHandoff.pool.map((symbol) => <span key={symbol} className="soft-tag">{symbol}</span>)}
                </div>
                {(tacticsHandoff.excluded?.length ?? 0) > 0 && (
                  <p className="tactics-handoff-meta warn">
                    未随本次发送 {tacticsHandoff.excluded.length} 只：
                    {tacticsHandoff.excluded.map((item) => `${item.symbol}（${item.reason}）`).join("、")}
                  </p>
                )}
                <button className="primary-button mt-10" disabled={scanBusy} onClick={() => onScan(["manual"], tacticsHandoff.pool)}>
                  {scanBusy ? "扫描中…" : "开始扫描（按此标的池） →"}
                </button>
              </div>
            )}
            <div className="pref-row" role="group" aria-label="扫描范围">
              {Object.entries(SOURCE_LABEL).map(([key, label]) => (
                <button
                  key={key}
                  className={`tag-button ${sources.includes(key) ? "active" : ""}`}
                  aria-pressed={sources.includes(key)}
                  disabled={scanBusy || (key === "manual" && false)}
                  onClick={() => setSources((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])}
                >
                  {label}
                </button>
              ))}
            </div>
            <input
              className="tactics-manual"
              value={manualInput}
              onChange={(event) => setManualInput(event.target.value)}
              placeholder="手动代码列表：000060, 510300, 600519（勾选「手动列表」时参与扫描）"
              disabled={scanBusy}
            />
            <button className="primary-button mt-10" disabled={scanBusy || sources.length === 0} onClick={runScan}>
              {scanBusy ? "扫描中…" : "开始扫描"}
            </button>
            {scanResult && (
              <ScanResultTable
                result={scanResult}
                actions={{
                  ...rowActions,
                  // 扫描子页内点「详情」留在本页（详情块就在下方），不跳子页。
                  onLoadSignals,
                }}
              />
            )}
          </div>
          )}

          {/* 市场雷达 · 全市场批量扫描（独立子页）：三组扫描方式 + 结果区 */}
          {subView === "market" && (
          <div className="dl-block">
            <h4>市场雷达 · 批量扫描</h4>
            <p>三种找股方式：行业板块扫描（先选板块再从成分股里挑）、榜单快筛（快，单请求粗筛）与全市场趋势扫描（慢，遍历沪深 A 股后按<b>战法质量分</b>排名）。都是纯规则识别，不消耗模型。</p>

            {/* 方式一（v26）：行业板块扫描——先选行业，再从成分股里挑 */}
            <div className="scan-group">
              <div className="scan-group-title">方式一 · 行业板块扫描（先选板块，再从成分股里挑）</div>
              <div className="filter-row">
                <span className="filter-label">行业板块</span>
                <div className="pref-row">
                  <button
                    className="ghost-btn"
                    disabled={sectorsLoading}
                    onClick={onLoadSectors}
                    title="拉取新浪行业板块清单（按当日涨跌幅排序，缓存 5 分钟）"
                  >
                    {sectorsLoading ? "加载中…" : sectors.length ? "刷新板块" : "加载行业板块"}
                  </button>
                  <button
                    className={`tag-button ${sectorPickOpen ? "active" : ""}`}
                    aria-pressed={sectorPickOpen}
                    disabled={sectors.length === 0}
                    onClick={() => setSectorPickOpen((open) => !open)}
                  >
                    选择板块 · 已选 {selectedSectorCodes.length}／8
                  </button>
                  {selectedSectorCodes.length > 0 && (
                    <button className="ghost-btn" disabled={marketBusy} onClick={() => setSelectedSectorCodes([])}>清空</button>
                  )}
                </div>
              </div>
              {sectors.length === 0 && !sectorsLoading && (
                <p className="filter-hint">点「加载行业板块」获取清单（约 49 个行业，按当日涨跌幅降序）。</p>
              )}
              {selectedSectorCodes.length > 0 && (
                <div className="pref-row mt-10">
                  {selectedSectorCodes.map((code) => {
                    const meta = sectors.find((item) => item.code === code);
                    return (
                      <span key={code} className="soft-tag">
                        {meta?.name ?? code}
                        <button
                          className="tag-remove"
                          aria-label={`移除 ${meta?.name ?? code}`}
                          disabled={marketBusy}
                          onClick={() => setSelectedSectorCodes((current) => current.filter((item) => item !== code))}
                        >
                          ×
                        </button>
                      </span>
                    );
                  })}
                </div>
              )}
              {sectorPickOpen && sectors.length > 0 && (
                <div className="sector-pick">
                  <input
                    className="price-input"
                    value={sectorQuery}
                    onChange={(event) => setSectorQuery(event.target.value)}
                    placeholder="过滤：行业名称或代码，如 玻璃"
                    aria-label="过滤行业板块"
                    disabled={marketBusy}
                  />
                  <div className="sector-pick-grid" role="group" aria-label="选择行业板块">
                    {filteredSectors.map((sector) => {
                      const active = selectedSectorCodes.includes(sector.code);
                      const full = !active && selectedSectorCodes.length >= 8;
                      return (
                        <button
                          key={sector.code}
                          className={`tag-button ${active ? "active" : ""}`}
                          aria-pressed={active}
                          disabled={marketBusy || full}
                          title={
                            `${sector.name}（${sector.code}）` +
                            (sector.member_count != null ? ` · ${sector.member_count} 只成分` : "") +
                            (sector.leader_name ? ` · 领涨 ${sector.leader_name}` : "") +
                            (full ? " · 已选满 8 个板块" : "")
                          }
                          onClick={() => setSelectedSectorCodes((current) =>
                            current.includes(sector.code)
                              ? current.filter((item) => item !== sector.code)
                              : [...current, sector.code],
                          )}
                        >
                          {sector.name}
                          {sector.change_pct != null && (
                            <i className={sector.change_pct >= 0 ? "up" : "down"}>
                              {sector.change_pct >= 0 ? "+" : ""}{Number(sector.change_pct).toFixed(2)}%
                            </i>
                          )}
                        </button>
                      );
                    })}
                    {filteredSectors.length === 0 && <span className="filter-hint">没有匹配的行业。</span>}
                  </div>
                </div>
              )}
              <div className="filter-row">
                <span className="filter-label">每板块</span>
                <div className="pref-row">
                  {[10, 20, 40].map((value) => (
                    <button
                      key={value}
                      className={`tag-button ${perSector === value ? "active" : ""}`}
                      aria-pressed={perSector === value}
                      disabled={marketBusy}
                      onClick={() => setPerSector(value)}
                    >
                      前 {value} 只
                    </button>
                  ))}
                </div>
              </div>
              <div className="filter-row">
                <span className="filter-label">板块共振</span>
                <div className="pref-row">
                  <button
                    className={`tag-button ${sectorResonance ? "active" : ""}`}
                    aria-pressed={sectorResonance}
                    disabled={marketBusy}
                    title="板块内同向占比 ≥60% 且样本 ≥4 只时，给板块内同向个股加板块共振分；不满足则不加（宁可不判，也不凭空加分）"
                    onClick={() => setSectorResonance(true)}
                  >
                    启用
                  </button>
                  <button
                    className={`tag-button ${!sectorResonance ? "active" : ""}`}
                    aria-pressed={!sectorResonance}
                    disabled={marketBusy}
                    title="只用个股自身形态评分，不引入板块横截面因子（便于与单票扫描口径对照）"
                    onClick={() => setSectorResonance(false)}
                  >
                    关闭
                  </button>
                </div>
              </div>
              <button
                className="primary-button"
                disabled={marketBusy || selectedSectorCodes.length === 0}
                onClick={() => onScanMarketSectors(selectedSectorCodes, perSector, sectorResonance)}
              >
                {marketBusy
                  ? marketProgress
                    ? `板块扫描中（第 ${marketProgress.done}/${marketProgress.total} 只）…`
                    : "板块扫描中…"
                  : `扫描选中板块（${selectedSectorCodes.length} 个 · 每板块前 ${perSector} 只）`}
              </button>
              {marketBusy && (
                <button className="ghost-btn" onClick={stopMarketScan} title="标记取消：当前票完成后不再发新取数（D01）">
                  停止扫描
                </button>
              )}
              <p className="filter-hint">
                逐票拉日 K 跑战法引擎，再按板块内横截面同向占比决定是否加板块共振分；
                结果按板块分组返回，便于「先看板块、再挑个股」。
              </p>
            </div>

            {/* 方式二：榜单快筛 */}
            <div className="scan-group">
              <div className="scan-group-title">方式二 · 榜单快筛（约 20–40 秒）</div>
              <div className="filter-row">
                <span className="filter-label">粗筛榜单</span>
                <div className="pref-row">
                  {Object.entries(MARKET_BOARD_LABEL).map(([key, label]) => (
                    <button
                      key={key}
                      className={`tag-button ${marketBoards.includes(key) ? "active" : ""}`}
                      aria-pressed={marketBoards.includes(key)}
                      disabled={marketBusy}
                      onClick={() => setMarketBoards((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <button
                className="primary-button"
                disabled={marketBusy || marketBoards.length === 0}
                onClick={() => onScanMarket(marketBoards, 30, 60)}
              >
                {marketBusy
                  ? marketProgress
                    ? `市场扫描中（第 ${marketProgress.done}/${marketProgress.total} 只）…`
                    : "市场扫描中…"
                  : "按榜单扫描候选（每榜前 30 · 上限 60 只）"}
              </button>
              {marketBusy && (
                <button className="ghost-btn" onClick={stopMarketScan} title="标记取消：当前票完成后不再发新取数（D01）">
                  停止扫描
                </button>
              )}
            </div>

            {/* 方式二：全市场趋势扫描 */}
            <div className="scan-group">
              <div className="scan-group-title">方式三 · 全市场趋势扫描（约 1–2 分钟，按战法质量分排名）</div>
              <div className="filter-row">
                <span className="filter-label">板块</span>
                <div className="pref-row">
                  {([["main", "主板"], ["chinext", "创业板"], ["star", "科创板"]] as const).map(([key, label]) => (
                    <button
                      key={key}
                      className={`tag-button ${marketSegments.includes(key) ? "active" : ""}`}
                      aria-pressed={marketSegments.includes(key)}
                      disabled={marketBusy}
                      onClick={() => setMarketSegments((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key])}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="filter-row">
                <span className="filter-label">候选数</span>
                <div className="pref-row">
                  {[20, 40, 80].map((value) => (
                    <button
                      key={value}
                      className={`tag-button ${allTopSymbols === value ? "active" : ""}`}
                      aria-pressed={allTopSymbols === value}
                      disabled={marketBusy}
                      onClick={() => setAllTopSymbols(value)}
                    >
                      前 {value}
                    </button>
                  ))}
                </div>
              </div>
              <div className="filter-row">
                <span className="filter-label">价格区间</span>
                <div className="pref-row">
                  <input
                    className="price-input"
                    value={priceMinInput}
                    onChange={(event) => setPriceMinInput(event.target.value)}
                    placeholder="最低价"
                    aria-label="最低价"
                    inputMode="decimal"
                    disabled={marketBusy}
                  />
                  <span className="filter-label" style={{ width: "auto" }}>—</span>
                  <input
                    className="price-input"
                    value={priceMaxInput}
                    onChange={(event) => setPriceMaxInput(event.target.value)}
                    placeholder="最高价"
                    aria-label="最高价"
                    inputMode="decimal"
                    disabled={marketBusy}
                  />
                  <span className="filter-hint">元 · 留空不限</span>
                </div>
              </div>
              <div className="filter-row">
                <span className="filter-label">战法</span>
                <div className="pref-row">
                  <button
                    className={`tag-button ${selectedTacticIds === null ? "active" : ""}`}
                    aria-pressed={selectedTacticIds === null}
                    disabled={marketBusy}
                    onClick={() => { setSelectedTacticIds(null); setTacticsOpen(false); }}
                  >
                    全部（{catalog.length}）
                  </button>
                  <button
                    className={`tag-button ${selectedTacticIds !== null ? "active" : ""}`}
                    aria-pressed={selectedTacticIds !== null}
                    disabled={marketBusy}
                    onClick={() => {
                      setTacticsOpen((open) => !open);
                      if (selectedTacticIds === null) setSelectedTacticIds([]);
                    }}
                  >
                    {selectedTacticIds !== null ? `自定义 · 已选 ${selectedTacticIds.length}` : "自定义选择…"}
                  </button>
                </div>
              </div>
              {tacticsOpen && (
                <div className="tactics-pick-grid" role="group" aria-label="选择参与扫描的战法">
                  {categorizedCatalog.map((group) => (
                    <div key={group.key} className="tactics-pick-cat">
                      <div className="tactics-pick-cat-label">{group.label}</div>
                      <div className="tactics-pick-cat-tags">
                        {group.tactics.map((tactic) => (
                          <button
                            key={tactic.id}
                            className={`tag-button ${selectedTacticIds?.includes(tactic.id) ? "active" : ""}`}
                            aria-pressed={selectedTacticIds?.includes(tactic.id) ?? false}
                            disabled={marketBusy}
                            title={tactic.description}
                            onClick={() => setSelectedTacticIds((current) => {
                              const base = current ?? [];
                              return base.includes(tactic.id) ? base.filter((id) => id !== tactic.id) : [...base, tactic.id];
                            })}
                          >
                            {tactic.name}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                  {selectedTacticIds !== null && selectedTacticIds.length === 0 && (
                    <span className="filter-hint">未选任何战法时按全部战法扫描</span>
                  )}
                </div>
              )}
              <button
                className="primary-button"
                disabled={marketBusy || marketSegments.length === 0}
                onClick={() => onScanMarketAll(allTopSymbols, marketSegments, parsePriceInput(priceMinInput), parsePriceInput(priceMaxInput), selectedTacticIds && selectedTacticIds.length > 0 ? selectedTacticIds : null)}
              >
                {marketBusy
                  ? marketProgress
                    ? `全市场遍历中（第 ${marketProgress.done}/${marketProgress.total} 只）…`
                    : "全市场遍历中…（约 1–2 分钟）"
                  : `开始全市场趋势扫描（${marketSegments.length === 3 ? "全部板块" : "限 " + marketSegments.length + " 个板块"}）`}
              </button>
              {marketBusy && (
                <button className="ghost-btn" onClick={stopMarketScan} title="标记取消：当前票完成后不再发新取数（D01）">
                  停止扫描
                </button>
              )}
              {marketResult?.mode === "all" && (
                <p className="filter-hint" style={{ color: marketResult.complete ? "var(--muted)" : "var(--warn, #d9a05b)" }}>
                  全市场快照 {marketResult.market_rows ?? 0} 只 ·
                  {marketResult.complete ? "完整遍历" : "未完整遍历（页数预算内先覆盖最活跃股票）"} ·
                  最终排名按<b>战法质量分</b>；当日涨幅 + 换手率 + 成交额分位仅用于挑候选（纯快照粗筛，与战法形态无关）。
                </p>
              )}
            </div>

            {/* 扫描结果：板块模式按板块分组，其余按总排名 */}
            {marketResult && (
              <div className="scan-group">
                <div className="scan-group-title">
                  扫描结果
                  {marketResult.mode === "sectors" && marketResult.sectors && (
                    <>
                      {" · "}
                      {marketResult.sectors.filter((block) => block.ok).length} 个板块 ·
                      共振{marketResult.sector_resonance ? "已启用" : "已关闭"}
                    </>
                  )}
                </div>
              <div className="tactics-market-scroll">
                {marketResult.mode === "sectors" ? (
                  <SectorScanView result={marketResult} actions={rowActions} />
                ) : (
                  <ScanResultTable result={marketResult} actions={rowActions} />
                )}
              </div>
              </div>
            )}
          </div>
          )}

          {/* 观察清单（独立子页） */}
          {subView === "watch" && (
          <div className="dl-block">
            <h4>观察清单</h4>
            <p>记录你正在跟踪的标的与一句话备注；勾选「观察清单」参与扫描。</p>
            <div className="tactics-watch-form">
              <input value={addSymbol} onChange={(event) => setAddSymbol(event.target.value)} placeholder="代码，如 000060" style={{ maxWidth: 130 }} />
              <input value={addName} onChange={(event) => setAddName(event.target.value)} placeholder="名称（可选）" style={{ maxWidth: 130 }} />
              <input value={addNote} onChange={(event) => setAddNote(event.target.value)} placeholder="备注（可选）" />
              <button className="ghost-btn" disabled={busy || !addSymbol.trim()} onClick={() => void submitWatch()}>加入观察</button>
            </div>
            {watchlist.length === 0 ? (
              <p className="scheme-empty">观察清单为空：扫描结果里点「+观察」，或在上方手动添加。</p>
            ) : (
              <table className="kv-table">
                <tbody>
                  {watchlist.map((entry) => (
                    <tr key={entry.symbol} className={entry.symbol === selectedSymbol ? "hit" : ""}>
                      <td>
                        <button className="text-button" onClick={() => onLoadSignals(entry.symbol)}>{entry.symbol}</button>
                        <small>{entry.name}{entry.note ? ` · ${entry.note}` : ""}</small>
                      </td>
                      <td>
                        <button className="ghost-btn" onClick={() => { onLoadSignals(entry.symbol); setSubView("scan"); }}>详情</button>
                        <button className="ghost-btn danger" onClick={() => void onDeleteWatch(entry.symbol)}>移除</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          )}

          {/* 单票详情：只放在战法扫描子页（观察清单/市场雷达点详情都会跳转过来，避免同一块内容两处渲染） */}
          {subView === "scan" && signalsLoading && <div className="chart-pending" role="status"><span className="booting-spinner" aria-hidden="true" /><strong>正在识别 {signalsLoading} 的战法信号…</strong></div>}
          {subView === "scan" && signals && !signalsLoading && (
            <div className="dl-block">
              <h4>{signals.symbol} · 战法详情</h4>
              <p>
                数据源 {signals.provider} · {signals.bars_count} 根日 K
                {!signals.sufficient && <span className="probe-note err"> · 历史数据不足，指标仅供参考</span>}
              </p>
              {signals.score && (
                <div className="score-panel">
                  <div className="score-panel-head">
                    <ScoreBadge
                      score={signals.score.tactic_score}
                      parts={signals.score.score_parts}
                      bias={signals.score.direction_bias}
                      windowBars={signals.score.signal_window_bars}
                      windowFrom={signals.score.signal_window_from}
                      formulaVersion={signals.score.score_formula_version}
                      extraTitle={scoreExtraTitleText(signals.score)}
                    />
                    <span className="filter-hint">
                      窗口：最近 {signals.score.signal_window_bars} 根 K 线
                      {signals.score.signal_window_from ? `（${signals.score.signal_window_from} 起）` : ""} ·
                      新鲜度半衰期 {signals.score.freshness_half_life_bars} 根 · 权重版本 {signals.score.weight_version}
                      {signals.score.score_formula_version && ` · 评分 ${signals.score.score_formula_version}`}
                    </span>
                  </div>
                  <p className="sort-basis">
                    分项：看多 {signals.score.score_parts.bullish} · 看空 {signals.score.score_parts.bearish} · 中性 {signals.score.score_parts.neutral} ·
                    跨类共振 {signals.score.score_parts.resonance} · 多空对冲 −{signals.score.score_parts.conflict} ·
                    量能 {signals.score.score_parts.volume_bonus ?? 0} · 位置 {signals.score.score_parts.position_bonus ?? 0} ·
                    板块 {signals.score.score_parts.sector_bonus ?? 0}（缩放 {signals.score.score_parts.bonus_scale ?? 0}）=
                    原始分 {signals.score.score_parts.raw}
                    {signals.score.score_formula_version === "v2"
                      ? ` → 饱和曲线 100×（1−e^(−raw÷${signals.score.score_parts.saturation_scale ?? signals.score.score_parts.norm_scale})）= 质量分 ${signals.score.tactic_score}`
                      : ` ÷ ${signals.score.score_parts.norm_scale} × 100 = 质量分 ${signals.score.tactic_score}`}
                  </p>
                  {signals.score.merged_families && signals.score.merged_families.length > 0 && (
                    <p className="filter-hint">
                      同族合并：{signals.score.merged_families.map((family) => TACTIC_CATEGORY_LABEL[family] ?? family).join("、")}
                      族内只按最高一条计分，其余照常展示（标记「同族合并·不计分」）但不再重复计分。
                    </p>
                  )}
                  {signals.score.insufficient_tactics.length > 0 && (
                    <p className="report-meta amber" style={{ margin: "6px 0" }}>
                      历史 K 线不足以下战法的最小样本（已如实标注，不参与扣分）：
                      {signals.score.insufficient_tactics.map((id) => tacticNameOf(catalog, id)).join("、")}
                    </p>
                  )}
                  {signals.score.hit_detail.length > 0 && (
                    <details className="score-detail">
                      <summary>逐条命中明细（{signals.score.hit_detail.length}）</summary>
                      <HitDetailTable detail={signals.score.hit_detail} />
                    </details>
                  )}
                </div>
              )}
              <div className="tactic-chips" role="list" aria-label="指标现值">
                <span className="soft-tag">收盘 {signals.indicators.close?.toFixed(2) ?? "—"}</span>
                <span className="soft-tag">MA5 {signals.indicators.ma5?.toFixed(2) ?? "—"}</span>
                <span className="soft-tag">MA10 {signals.indicators.ma10?.toFixed(2) ?? "—"}</span>
                <span className="soft-tag">MA20 {signals.indicators.ma20?.toFixed(2) ?? "—"}</span>
                <span className="soft-tag">MACD DIF {signals.indicators.macd_dif?.toFixed(3) ?? "—"}</span>
                <span className="soft-tag">KDJ K/D {signals.indicators.kdj_k?.toFixed(1) ?? "—"}/{signals.indicators.kdj_d?.toFixed(1) ?? "—"}</span>
                <span className="soft-tag">RSI14 {signals.indicators.rsi14?.toFixed(1) ?? "—"}</span>
              </div>
              <TacticsChart bars={signals.bars} signals={signals.signals} />
              <p className="chart-limit">▲红=看多形态（金叉等），▼绿=看空形态（死叉等），●灰=中性形态；悬停查看当日全部战法说明。</p>

              <div className="tactics-signal-list">
                {/* v29 信号时间线：状态（仅挂到该战法最近一次触发那条，不误标历史信号）+ 权重 + 失效条件。
                    60 条历史信号里的冷却期合并条目没有独立状态，如实留空 —— 不假装它们也有状态。 */}
                <h4>近期信号时间线（按日期倒序，前 20 条展开状态与失效条件）</h4>
                {sortedSignals.length === 0 ? (
                  <p className="scheme-empty">该标的近期没有战法信号。</p>
                ) : (
                  <table className="kv-table tactics-signal-timeline">
                    <thead>
                      <tr>
                        <th>日期</th>
                        <th>战法</th>
                        <th>状态</th>
                        <th title="形态权重（战法目录口径，同族只计最高一条）">权重</th>
                        <th title="什么条件下该信号作废（战法目录口径）">失效条件</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedSignals.slice(0, 20).map((signal, index) => {
                        const meta = catalog.find((tactic) => tactic.id === signal.tactic_id);
                        const stateDetail = signals?.score?.signal_states?.[signal.tactic_id];
                        // 状态只属于该战法「最近一次触发」的那条信号；日期不匹配（更早的历史信号）不显示状态。
                        const showState = stateDetail && stateDetail.signal_date === signal.date ? stateDetail : undefined;
                        return (
                          <tr key={`${signal.tactic_id}-${signal.date}-${index}`}>
                            <td><time>{signal.date}</time></td>
                            <td>
                              <span className={`tactic-badge ${signal.direction}`}>{signal.tactic_name}</span>
                              <small style={{ marginLeft: 8 }}>{signal.detail}</small>
                            </td>
                            <td>{showState ? <SignalStateTag detail={showState} /> : <span className="signal-state-none">—</span>}</td>
                            <td>{meta?.weight ?? "—"}</td>
                            <td className="signal-timeline-invalidation">{meta?.invalidation_rule || "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>

              <div className="tactics-notes">
                <h4>观察笔记 · {selectedSymbol}</h4>
                <div className="tactics-watch-form">
                  <input
                    value={noteDraft}
                    onChange={(event) => setNoteDraft(event.target.value)}
                    placeholder="记录你的观察与判断依据（仅保存在本机）"
                    disabled={busy}
                  />
                  <button className="ghost-btn" disabled={busy || !noteDraft.trim()} onClick={() => void submitNote()}>记录</button>
                </div>
                {notes.length === 0 ? (
                  <p className="scheme-empty">还没有笔记。</p>
                ) : (
                  <ul className="tactics-note-list">
                    {notes.map((note) => (
                      <li key={note.note_id}>
                        <time>{formatDate(note.created_at)}</time>
                        <span>{note.content}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}

          {/* 战法目录（扫描子页底部，参考信息，按分类分组） */}
          {subView === "scan" && (
          <div className="dl-block">
            <h4>战法目录（{catalog.length}）</h4>
            {categorizedCatalog.map((group) => (
              <div key={group.key} className="tactics-catalog-cat">
                <div className="tactics-pick-cat-label">{group.label}</div>
                <table className="kv-table">
                  <tbody>
                    {group.tactics.map((tactic) => (
                      <tr key={tactic.id}>
                        <td><span className={`tactic-badge ${tactic.direction}`}>{tactic.name}</span></td>
                        <td>{tactic.description}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
            <div className="key-note">
              <b>口径说明</b>
              所有战法都是对 K 线的确定性计算（同输入同结果）：MACD(12,26,9)、KDJ(9,3,3)、RSI(14) Wilder 平滑，与主流行情软件口径一致。信号描述的是「图形上发生了什么」，历史命中不代表未来表现，不构成任何买卖建议。
              <br /><br />
              <b>按人群的入门建议与未接入声明</b>
              纯新手可重点关注：波段低点抬高、突破平台、N 字上涨、均线辅助回踩（先建立买卖纪律）；上班族适合波段 + 回踩支撑类信号；短线老手可研究弱转强、反核修复、阳吞阴（反包）。以下战法<b>未接入</b>，本系统不做伪信号：龙头 / 龙空龙（需涨停梯队与连板高度数据）、埋伏（计划型策略，无图形定义）、产业趋势 / 价值投机（需基本面与产业数据，见投资页研究证据流）。
            </div>
          </div>
          )}

          {/* v26 AI 复核面板：手动触发，规则分与 AI 分并列展示（不互相覆盖） */}
          {aiReviewSymbol && (
            <div className="dl-block ai-review-panel">
              <div className="ai-review-panel-head">
                <h4>AI 复核 · {aiReviewSymbol}</h4>
                <button className="ghost-btn" onClick={() => setAiReviewSymbol(null)}>收起</button>
              </div>
              <p className="filter-hint">
                规则分衡量「图形证据的强度」，AI 分衡量「这些证据可不可信」——口径不同，<b>并列展示、不互相覆盖</b>，
                两者分歧恰恰是最值得看的地方。AI 只做质检：不写研报、不给买卖建议、不预测涨跌。
                每次点击才调用一次模型，不做任何自动复核。
              </p>
              {!aiReady && (
                <p className="probe-note err">
                  AI 未接通：请先在 设置 → 模型配置 添加方案、粘贴密钥并设为「使用中」。
                </p>
              )}
              <div className="tactics-watch-form">
                <input
                  value={aiReviewQuestion}
                  onChange={(event) => setAiReviewQuestion(event.target.value)}
                  placeholder="关注点（可选）：如「这个突破能不能追」"
                  maxLength={300}
                  disabled={aiReviewBusy === aiReviewSymbol}
                />
                <button
                  className="primary-button"
                  disabled={!aiReady || aiReviewBusy === aiReviewSymbol}
                  onClick={() => void onRunAiReview(aiReviewSymbol, aiReviewSector, aiReviewQuestion.trim())}
                >
                  {aiReviewBusy === aiReviewSymbol ? "复核中…" : "开始复核"}
                </button>
              </div>
              {aiReviewSector && (
                <p className="filter-hint">
                  所属板块：{sectors.find((item) => item.code === aiReviewSector)?.name ?? aiReviewSector}（一并送模型，板块强弱会影响形态可靠度判断）
                </p>
              )}
              {aiReviewBusy === aiReviewSymbol && (
                <div className="chart-pending" role="status"><span className="booting-spinner" aria-hidden="true" /><strong>正在复核 {aiReviewSymbol}…</strong></div>
              )}
              {aiReviewsForSymbol.length === 0 ? (
                !aiReviewBusy || aiReviewBusy !== aiReviewSymbol ? (
                  <p className="scheme-empty">还没有该标的的复核记录。点「开始复核」生成一份并留痕，便于长期回看。</p>
                ) : null
              ) : (
                <div className="ai-review-list">
                  {aiReviewsForSymbol.map((record) => (
                    <AiReviewCard key={record.review_id} record={record} onDelete={onDeleteAiReview} />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
