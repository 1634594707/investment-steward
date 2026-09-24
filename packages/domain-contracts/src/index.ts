export const CORE_VERSION = "0.1.0" as const;
export const SCHEMA_VERSION = "1.0" as const;

export type PolicyStatus = "draft" | "active" | "superseded" | "archived";
export type ConfirmationMethod = "explicit_ui" | "imported";
export type EvidenceType = "quote" | "financial" | "announcement" | "macro" | "analysis" | "learning";
export type EvidenceRelation = "supporting" | "contradicting" | "unknown";
export type EvidenceStatus = "active" | "stale" | "retracted";
export type ActionMode = "observe" | "research" | "review_plan" | "no_action";
export type PluginInstallationState = "available" | "installed" | "enabled" | "disabled" | "revoked";

/** 插件 UI 输出的呈现级别（与插槽仲裁规则一致：级别越高越靠近主视图） */
export type PluginUiSlotLevel = "L0" | "L1" | "L2" | "L3";

export interface PluginUiSlot {
  /** 目标插槽 id，如 "today.brief"、"notification.global" */
  slot: string;
  level: PluginUiSlotLevel;
}

export interface PluginManifest {
  publisher: string;
  plugin_id: string;
  release_version: string;
  display_name: string;
  description: string;
  plugin_type: string;
  license: string;
  sdk_min: string;
  sdk_max: string;
  capabilities: string[];
  /** 插件声明要注入的插槽集合；无 UI 输出的插件可省略 */
  ui_slots?: PluginUiSlot[];
  /** 挂载方式：in_page=产出插槽卡（默认）；own_page=独立应用，占 app.library 槽，主视图进侧栏「应用」区 */
  mount?: "in_page" | "own_page";
  schema_versions: Record<string, string>;
  entrypoint: string;
  artifact_sha256: string;
  signature: string;
  network_allowlist: string[];
  side_effects: string[];
  requires_confirmation: boolean;
  supports_markets: string[];
}

export interface PluginInstallation {
  plugin_id: string;
  release_version: string;
  state: PluginInstallationState;
  granted_capabilities: string[];
  artifact_sha256: string;
  source: string;
  installed_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

/** 服务端插槽注册表解析出的输出去向（G2-3：前端不再维护 slotPageOf 兜底）。 */
export interface PluginResolvedOutput {
  slot: string;
  page: string;
  level: string;
}

export interface PluginCatalogEntry {
  manifest: PluginManifest;
  installation?: PluginInstallation | null;
  resolved_outputs?: PluginResolvedOutput[];
}

export interface OHLCVBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandleSeries {
  instrument: string;
  timeframe: string;
  bars: OHLCVBar[];
  source_plugin_id: string;
  source_plugin_release: string;
  source_name: string;
  as_of: string;
  is_demo: boolean;
  limitations: string[];
}

export interface InvestmentPolicyVersion {
  policy_id: string;
  user_id: string;
  version: number;
  status: PolicyStatus;
  investment_goal: string;
  horizon_years?: number | null;
  liquidity_needs: string;
  allowed_markets: string[];
  allowed_asset_classes: string[];
  risk_boundaries: Record<string, unknown>;
  preferred_methods: string[];
  excluded_methods: string[];
  observation_conditions: string[];
  invalidation_conditions: string[];
  review_due_at?: string | null;
  change_reason: string;
  source_learning_activity_ids: string[];
  supersedes_id?: string | null;
  confirmed_at?: string | null;
  confirmation_method?: ConfirmationMethod | null;
  confirmation_summary?: string | null;
  schema_version: typeof SCHEMA_VERSION;
  created_at: string;
  updated_at: string;
  revision: number;
  device_id?: string | null;
  change_event_id?: string | null;
  conflict_policy: string;
}

export interface Evidence {
  evidence_id: string;
  tenant_id: string;
  subject_refs: string[];
  evidence_type: EvidenceType;
  source_name: string;
  source_uri?: string | null;
  license_status: string;
  source_trust_note?: string | null;
  published_at?: string | null;
  observed_at?: string | null;
  collected_at: string;
  valid_until?: string | null;
  source_dataset_version?: string | null;
  producer_plugin_id?: string | null;
  producer_release?: string | null;
  schema_version: typeof SCHEMA_VERSION;
  summary: string;
  raw_locator?: string | null;
  content_hash: string;
  relation: EvidenceRelation;
  freshness: string;
  quality_flags: string[];
  limitations: string[];
  status: EvidenceStatus;
}

export interface PluginCapability {
  capability_id: string;
  capability_version: typeof SCHEMA_VERSION;
  stability: "stable" | "experimental";
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  allowed_resource_types: string[];
  scope: "run" | "user" | "organization";
  private_namespace_write: boolean;
  model_access: boolean;
  notification_proposal: boolean;
  scheduled_task: boolean;
  network_allowlist: string[];
  rate_limit_per_minute: number;
  max_concurrency: number;
  max_cpu_seconds: number;
  max_memory_mb: number;
  max_execution_seconds: number;
  max_output_bytes: number;
  data_leaves_device: boolean;
  retention: string;
  deletion_policy: string;
  side_effects: string[];
  deterministic: boolean;
  requires_confirmation: boolean;
}

export type RunStatus = "created" | "planning" | "collecting_evidence" | "analyzing" | "waiting_confirmation" | "composing" | "completed" | "failed" | "cancelled" | "stale";

export interface ResearchRun {
  run_id: string;
  user_id: string;
  status: RunStatus;
  user_question: string;
  evidence_refs: string[];
  supporting_refs: string[];
  contradicting_refs: string[];
  response_id?: string | null;
  created_at: string;
  updated_at: string;
  error_code?: string | null;
}

export interface AgentResponse {
  response_id: string;
  run_id: string;
  user_id: string;
  created_at: string;
  user_question: string;
  context_snapshot_refs: string[];
  summary: string;
  reasoning_outline: string[];
  confidence_description: string;
  evidence_refs: string[];
  supporting_refs: string[];
  contradicting_refs: string[];
  missing_information: string[];
  freshness_warning: string[];
  limitations: string[];
  action_mode: ActionMode;
  supported_actions?: Array<"open_evidence" | "create_plan" | "start_learning" | "review_policy">;
  learning_link?: string | null;
  suggested_activity?: Record<string, unknown> | null;
  user_confirmation_required: boolean;
  proposed_changes?: Record<string, unknown> | null;
  model_provider?: string | null;
  model_name?: string | null;
  prompt_policy_version: string;
  plugin_runs: string[];
  tool_calls: string[];
  audit_refs: string[];
  /**
   * JV07：行动模式预判路由的结论。**`null`/`undefined` = 本轮没跑预判**
   * （总闸关闭 / 未启用 / 模型出网关闭），与「跑过但维持完整链路」是两件事。
   * 它只说明「跑了哪条链路」，**不参与也不改写** `action_mode`。
   */
  jev_route?: JevRouteDecision | null;
}

/** JV07：行动模式预判路由的结论（只描述「跑了哪条链路」，不描述结论本身）。 */
export interface JevRouteDecision {
  mode: "research" | "observe" | "review_plan" | "no_action" | null;
  mode_label: string;
  /** `choice` 应答自带的置信度；没有时为 `null`（**不伪造**）。 */
  confidence: number | null;
  probabilities: Record<string, number>;
  /** 是否据此**跳过了**完整研究链路（只有判为 `no_action` 且置信度达标才为真）。 */
  bypassed_model: boolean;
  /** `routed`（按预判走了）/ `full`（拿到预判但维持完整链路）/ `unannotated`（没拿到）。 */
  route_state: "routed" | "full" | "unannotated";
  question_id: string;
  options: string[];
  note: string;
  model: string;
  schema_version: string;
}

export type HoldingStatus = "holding" | "watchlist";

export interface Holding {
  holding_id: string;
  user_id: string;
  instrument: string;
  label: string;
  status: HoldingStatus;
  strategy_note: string;
  created_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

export type ThesisStatus = "active" | "archived";

export interface Thesis {
  thesis_id: string;
  user_id: string;
  instrument: string;
  original_statement: string;
  core_assumptions: string[];
  supporting_conditions: string[];
  invalidation_conditions: string[];
  observation_metrics: string[];
  status: ThesisStatus;
  created_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

export type PlanStatus = "planned" | "active" | "completed" | "cancelled";

export interface Plan {
  plan_id: string;
  user_id: string;
  title: string;
  status: PlanStatus;
  decision_id?: string | null;
  created_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

export interface DecisionEntry {
  decision_id: string;
  user_id: string;
  made_at: string;
  theme: string;
  decision_summary: string;
  rationale: string;
  outcome: string;
  retrospective: string;
  linked_evidence_ids: string[];
  plan_id?: string | null;
  schema_version: typeof SCHEMA_VERSION;
}

export interface BriefItem {
  display_order: number;
  title: string;
  summary: string;
  related_instrument?: string | null;
  signal: string;
  evidence_refs: string[];
  data_time?: string | null;
}

export interface TodayBrief {
  brief_id: string;
  user_id: string;
  generated_at: string;
  has_personalization: boolean;
  headline: string;
  items: BriefItem[];
  empty_reason?: string | null;
  policy_version: string;
  schema_version: typeof SCHEMA_VERSION;
}

export interface WeeklySection {
  key: string;
  title: string;
  lines: string[];
  change_to_process: boolean;
}

export interface WeeklyReview {
  review_id: string;
  user_id: string;
  generated_at: string;
  period_label: string;
  thesis_changes: string[];
  open_research: string[];
  decisions_summary: string[];
  next_week_suggestions: string[];
  principle_change_reason?: string | null;
  sections: WeeklySection[];
  schema_version: typeof SCHEMA_VERSION;
}

export type LearningUnitType = "lesson" | "exercise" | "reflection";

export interface LearningUnit {
  unit_id: string;
  user_id: string;
  unit_type: LearningUnitType;
  title: string;
  objective: string;
  content: string;
  guidance: string;
  bound_instrument?: string | null;
  bound_instrument_label?: string | null;
  related_conditions: string[];
  source_plugin_id: string;
  source_plugin_release: string;
  data_time: string;
  schema_version: typeof SCHEMA_VERSION;
}

export interface LearningGoal {
  goal_id: string;
  user_id: string;
  period_label: string;
  target_count: number;
  completed_count: number;
  created_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

export interface LearningActivity {
  activity_id: string;
  user_id: string;
  unit_id: string;
  unit_type: LearningUnitType;
  bound_instrument?: string | null;
  bound_instrument_label?: string | null;
  objective: string;
  user_answer: string;
  reflection: string;
  completed_at: string;
  created_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

/**
 * JV08：通知的语义分诊结论（相关性 choice + 优先级 score）。
 *
 * 与 `last_delivery_error` 严格分开：「被有意压制」与「想发但没发出去」是两件事。
 * `suppressed` 只影响呈现与投递，**不影响落库**——被压制的通知仍可在
 * 「已分诊未通知」里翻到（软校验铁律：不静默吞）。
 */
export interface NotificationTriage {
  /** material | uncertain | immaterial */
  impact?: "material" | "uncertain" | "immaterial" | null;
  impact_label: string;
  /** 0–2（3 级描述性 rubric 的等级号） */
  priority?: number | null;
  priority_label: string;
  priority_percent?: number | null;
  /** 两题 confidence 的较小值（取小是保守方向） */
  confidence?: number | null;
  suppressed: boolean;
  /** suppressed | notified | unannotated */
  triage_state: "suppressed" | "notified" | "unannotated";
  note: string;
  model?: string | null;
  schema_version: typeof SCHEMA_VERSION;
}

export interface Notification {
  notification_id: string;
  user_id: string;
  thesis_id?: string | null;
  instrument?: string | null;
  triggered_by: string;
  condition_kind: string;
  title: string;
  summary: string;
  action_mode: ActionMode;
  evidence_refs: string[];
  created_at: string;
  data_time?: string | null;
  read: boolean;
  delivery_status?: "pending" | "delivered" | "failed";
  delivery_attempts?: number;
  next_retry_at?: string | null;
  last_delivery_error?: string | null;
  /**
   * JV08：语义分诊结论。**`undefined`/`null` = 这轮没分诊**（Jev 未启用 / 总闸关闭 /
   * 超上限），与「分诊过但判为放行」是两件事——前者不代表任何语义判断，界面不能渲染成
   * 「已通过」。
   */
  triage?: NotificationTriage | null;
  schema_version: typeof SCHEMA_VERSION;
}

/** JV08：`GET /notifications/triage` 的响应——「已分诊未通知」必须可翻到。 */
export interface NotificationTriageReport {
  /** false = 整层没跑（Jev 未启用 / 总闸关闭），此时不渲染任何分诊信息。 */
  enabled: boolean;
  model?: string | null;
  total: number;
  notified: number;
  suppressed: number;
  /** 没送出去评（分片失败 / 超单轮上限 / 单条超预算）。**不等于「通过」**。 */
  unannotated: number;
  impacts: Record<string, number>;
  priorities: Record<string, number>;
  suppressed_items: Notification[];
  note: string;
  schema_version: typeof SCHEMA_VERSION;
}

export type CredentialStoreBackend = "os" | "file";

/** 凭据的可展示摘要：永不携带明文，只回 last4 + updated_at + 存储后端（G3-1/G3-4）。 */
export interface CredentialRecord {
  key_id: string;
  last4: string;
  updated_at: string;
  backend: CredentialStoreBackend;
}

export type ModelProfileStatus = "使用中" | "未启用";

/** 模型服务多方案（CC Switch 式，G3-3）：同一时刻恰好一个「使用中」。 */
export interface ModelProfile {
  profile_id: string;
  name: string;
  base_url: string;
  model: string;
  credential_ref: string;
  status: ModelProfileStatus;
  /** 方案级模型调用超时（秒）：null/缺省 = 内置默认；推理型模型可放宽。 */
  timeout_secs?: number | null;
  created_at: string;
  updated_at: string;
}

/** Jev 决策模型（System One）连接参数。
 *  **刻意不复用 ModelProfile**：那是 chat 方案表（「同一时刻恰好一个使用中」），
 *  Jev 走 POST {base_url}/systemone，是并列的另一套协议，配置独立成节。
 *  密钥**不在本类型里**——只有 credential_ref（凭据库引用）。 */
export interface JevSettings {
  user_id: string;
  /** 场景级开关：默认关闭。开启后 state 会出网到 TypeSafe（美国托管、默认非零留存）。 */
  enabled: boolean;
  base_url: string;
  model: string;
  credential_ref: string;
  timeout_secs: number;
  created_at: string;
  updated_at: string;
  schema_version: string;
}

/** Jev 配置读取视图：GET /jev/config 的完整回执（含官方上限与数据出网说明）。 */
export interface JevConfigView {
  ok: boolean;
  config: JevSettings;
  /** false = 当前值来自 STEWARD_JEV_* 环境变量或内置默认，尚未在设置页保存过。 */
  saved: boolean;
  /** 全局出网总闸（STEWARD_MODEL_ACCESS）：false 时所有 Jev 按钮必须如实降级。 */
  access_enabled: boolean;
  schema_version: string;
  defaults: { base_url: string; model: string; timeout_secs: number };
  limits: { choice_max_options: number; score_min_levels: number; score_max_levels: number };
  /** `noul` 题没有 confidence：阈值在代码里，这里是当前口径（中文场景须重新标定）。 */
  noul_thresholds: { yes: number; no: number };
  data_handling: {
    trains_on_input: boolean;
    zero_data_retention: string;
    /** 本项目是否已申请企业版 ZDR：**恒为 false（用户 2026-09-21 拍板不申请）**，故白名单按最严口径执行。 */
    zdr_applied: boolean;
    retention: string;
    hosted_in: string;
  };
  notes: string[];
}

/** Jev 配置保存负载：不含密钥（明文密钥走 /credentials 通道加密入库）。 */
export type JevSettingsInput = Pick<
  JevSettings,
  "enabled" | "base_url" | "model" | "credential_ref" | "timeout_secs"
>;

/** 个人中心 · 通知偏好：只决定提醒的呈现与节流，通道是否可用仍以 /notifications/channels 为准。 */
export interface PersonalNotifyPrefs {
  /** 站内提醒（顶部通知铃） */
  in_app_enabled: boolean;
  /** 外部通道（钉钉等已配置通道） */
  external_enabled: boolean;
  quiet_hours_enabled: boolean;
  /** 免打扰开始（HH:MM，24 小时制） */
  quiet_start: string;
  /** 免打扰结束（HH:MM，24 小时制） */
  quiet_end: string;
  /** 提醒节奏：realtime 实时 / daily 每日汇总 / weekly 每周汇总 */
  frequency: "realtime" | "daily" | "weekly";
}

/** 个人中心设置：显示身份 + 默认落地页 + 通知偏好 + 风险偏好。
 *  与 InvestorProfile 分工——画像回答「你是怎样的投资者」（研究引擎消费），
 *  个人中心回答「界面怎么为你服务」（不参与任何计算）。字段与后端 PersonalSettings 同源。 */
export interface PersonalSettings {
  user_id: string;
  display_name: string;
  /** 头像 dataURL（96px 方图，PNG）；无图片时按 avatar_color 取首字 */
  avatar_data: string | null;
  /** 头像底色（#rrggbb） */
  avatar_color: string;
  /** 启动后默认落地的视图 id（AppView） */
  default_view: string;
  notify: PersonalNotifyPrefs;
  /** 风险偏好：conservative / balanced / aggressive，未填写为空串 */
  risk_profile: string;
  /** 投资风格标签（最多 12 个） */
  style_tags: string[];
  created_at: string;
  updated_at: string;
  schema_version: string;
}

/** 个人中心保存负载：user_id / created_at / updated_at / schema_version 由 Core 归属与生成。 */
export type PersonalSettingsInput = Omit<PersonalSettings, "user_id" | "created_at" | "updated_at" | "schema_version">;



export interface InvestorProfile {
  user_id: string;
  /** 投资目标（如「长期稳健增值，跑赢通胀」） */
  investment_goal: string;
  /** 投资年限（年），gt 0 */
  horizon_years?: number | null;
  /** 流动性需求 */
  liquidity_needs: string;
  /** 投资知识自评 */
  knowledge_self_assessment: string;
  /** 关注的市场/资产类别 */
  markets_and_assets: string[];
  /** 授权/知悉（隐私、数据使用等开关） */
  consent: Record<string, boolean>;
  privacy_settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  schema_version: typeof SCHEMA_VERSION;
}

export interface AuditEvent {
  event_id: string;
  tenant_id: string;
  actor_id?: string | null;
  action: string;
  resource_type: string;
  resource_id?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  schema_version: string;
}

/** `/channels` 的通道快照项：host / plugin / content + 当前版本摘要。 */
export type UpdateChannelView = Record<string, string>;

/** 量化分享池制品类型（阶段 A 参数集先行，B/D 未开放）。 */
export type ArtifactType = "parameter_set" | "strategy_pack" | "model_weights";
/** 制品指标口径：一个数字必须挂口径才能展示。 */
export type ArtifactCaliber = "backtest" | "paper" | "simulated" | "self_reported" | "broker_verified";

export interface ArtifactMetric {
  label: string;
  value: string;
  caliber: ArtifactCaliber;
}

export interface ArtifactConsistency {
  author?: string | null;
  local?: string | null;
  diff?: string | null;
  status?: string | null;
  note: string;
}

export interface ArtifactParam {
  k: string;
  v: string;
  range: string;
  note: string;
}

export interface QuantRunNote {
  id: string;
  who: string;
  env: string;
  result: string;
  status: string;
}

export interface ArtifactCard {
  id: string;
  name: string;
  type: ArtifactType;
  author: string;
  ver: string;
  hash: string;
  style: string;
  instruments: string;
  sample_months?: number | null;
  desc: string;
  updated: string;
  license: string;
  forks: number;
  mine: boolean;
  locked: boolean;
  caliber_score: string;
  sample_out: string;
  repro: string;
  metrics: ArtifactMetric[];
  live?: ArtifactMetric | null;
  consistency?: ArtifactConsistency | null;
  derived_from?: string | null;
  lineage: Array<Record<string, string>>;
  runs: QuantRunNote[];
  params: ArtifactParam[];
}

/** `GET /quant/artifacts` 响应：available=False 表示阶段未开放，不编造制品。 */
export interface ArtifactPoolView {
  available: boolean;
  stage: string;
  stage_label: string;
  artifacts: ArtifactCard[];
  degraded_reason?: string | null;
  notice: string;
}

/** `GET /quant/lineage/{id}` 响应：谱系（阶段未开放时为空）。 */
export interface ArtifactLineageView {
  available: boolean;
  artifact_id: string;
  lineage: Array<Record<string, string>>;
}

/** 研读图书馆书目（M4 扩展后）：全本机存储，data_leaves_device=false。 */
export type BookShelfStatus = "reading" | "finished" | "wishlist";
export type BookSource = "ai" | "user" | "manual";

export interface Book {
  book_id: string;
  title: string;
  author: string;
  /** 0.0–1.0 的阅读进度 */
  progress: number;
  notes: string[];
  /** ISBN 元数据（M4：可选，兼容旧 payload） */
  isbn?: string | null;
  /** 三态书架：在读 / 已读 / 想读 */
  status: BookShelfStatus;
  /** 书源通道：AI 推荐 / 用户上传 / 手动录入 */
  source: BookSource;
  created_at: string;
  updated_at: string;
  data_leaves_device: boolean;
}

/** AI 荐读计划（G5-2）：只进 today.learning 学习流，永不写投资原则。 */
export interface LibraryPlan {
  plan_id: string;
  book_ref?: string | null;
  daily_task: string;
  source_chapter: string;
  rationale: string;
  created_at: string;
  slot: string;
  renderer: string;
}

/** 宏观雷达单指标读数：pending=未接入/无数据，绝不编造数值（ADR-0006）。 */
export interface MacroIndicatorReading {
  indicator: string;
  label: string;
  /** growth / employment / inflation / monetary / other */
  dim: string;
  status: "ok" | "pending" | "degraded";
  latest?: number | null;
  obs_date?: string | null;
  unit: string;
  as_of?: string | null;
  source: string;
  /** 数据集版本锚，如 "fred:CPIAUCSL:2025-08-01" */
  dataset_version?: string | null;
  note: string;
}

/** 背景层行（货币指数 / 油价 / 金价）：单列展示，不进国别四维打分。 */
export interface MacroBackgroundRow {
  key: string;
  label: string;
  status: "ok" | "pending" | "degraded";
  latest?: number | null;
  obs_date?: string | null;
  as_of?: string | null;
  source: string;
  dataset_version?: string | null;
  note: string;
}

/** 单维打分（M5）：score=null 表示该维无 ok 指标、未参与加权；rules_fired 逐条记录命中规则与实际值。 */
export interface MacroDimScore {
  dim: string;
  score?: number | null;
  rules_fired: string[];
  indicators_used: string[];
}

/** 规则基线定位（M5）：确定性规则可复算；weight_version 固定 v1（用户自定义 M5.5 接入）。 */
export interface MacroPositioning {
  region: string;
  weight_version: string;
  composite: number;
  /** 扩张 / 放缓 / 承压 / 衰退风险 */
  band: string;
  near_boundary_note?: string | null;
  dims: MacroDimScore[];
  limitations: string[];
  generated_at: string;
}

/** `GET /evidence/macro/{region}` 响应：region=国别（us/cn/eu/jp/in）或 global（背景层）。 */
export interface MacroSnapshot {
  region: string;
  label: string;
  indicators: MacroIndicatorReading[];
  background: MacroBackgroundRow[];
  positioning?: MacroPositioning | null;
  degraded_reason?: string | null;
  generated_at: string;
}
