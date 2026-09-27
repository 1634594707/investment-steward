/* eslint-disable */
// 本文件由 scripts/export_contracts.py 生成，请勿手工修改。
//
// 生成源：apps/core-api 的 Pydantic 模型 + 真实路由 response_model 声明。
// 校验：scripts/export_contracts.py --check（差异即失败），以及
//       apps/core-api/tests/test_a03_contract_parity.py 与手写声明的逐字段比对。
//
// 注意：本模块与 src/index.ts 目前并存——index.ts 是待迁移的手写副本，
// 两者的一致性由上面那个测试强制，不存在「生成物只是摆设」的情况。

export type ActionMode = "observe" | "research" | "review_plan" | "no_action";
export interface AgentResponse {
  response_id: string;
  run_id: string;
  user_id: string;
  created_at?: string;
  user_question: string;
  context_snapshot_refs?: string[];
  summary: string;
  reasoning_outline?: string[];
  confidence_description: string;
  evidence_refs?: string[];
  supporting_refs?: string[];
  contradicting_refs?: string[];
  missing_information?: string[];
  freshness_warning?: string[];
  limitations?: string[];
  action_mode: ActionMode;
  supported_actions?: string[];
  learning_link?: string | null;
  suggested_activity?: Record<string, unknown> | null;
  user_confirmation_required?: boolean;
  proposed_changes?: Record<string, unknown> | null;
  model_provider?: string | null;
  model_name?: string | null;
  prompt_policy_version?: string;
  plugin_runs?: string[];
  tool_calls?: string[];
  audit_refs?: string[];
  jev_route?: JevRouteDecision | null;
}
export interface AnnouncementBatch {
  instrument: string;
  available: boolean;
  entries: Evidence[];
  source_name: string;
  limitations?: string[];
}
export interface ArtifactCard {
  id: string;
  name: string;
  type?: string;
  author?: string;
  ver?: string;
  hash?: string;
  style?: string;
  instruments?: string;
  sample_months?: number | null;
  desc?: string;
  updated?: string;
  license?: string;
  forks?: number;
  mine?: boolean;
  locked?: boolean;
  caliber_score?: string;
  sample_out?: string;
  repro?: string;
  metrics?: ArtifactMetric[];
  live?: ArtifactMetric | null;
  consistency?: ArtifactConsistency | null;
  derived_from?: string | null;
  lineage?: Record<string, string>[];
  runs?: RunNote[];
  params?: ArtifactParam[];
}
export interface ArtifactConsistency {
  author?: string | null;
  local?: string | null;
  diff?: string | null;
  status?: string | null;
  note?: string;
}
export interface ArtifactLineageView {
  available: boolean;
  artifact_id: string;
  lineage?: Record<string, string>[];
}
export interface ArtifactMetric {
  label: string;
  value: string;
  caliber: string;
}
export interface ArtifactParam {
  k: string;
  v: string;
  range: string;
  note?: string;
}
export interface ArtifactPoolView {
  available: boolean;
  stage: string;
  stage_label: string;
  artifacts?: ArtifactCard[];
  degraded_reason?: string | null;
  notice?: string;
}
export interface AuditEvent {
  event_id: string;
  tenant_id: string;
  actor_id?: string | null;
  action: string;
  resource_type: string;
  resource_id?: string | null;
  payload?: Record<string, unknown>;
  created_at?: string;
  schema_version?: string;
}
export interface Book {
  book_id: string;
  title: string;
  author: string;
  progress?: number;
  notes?: string[];
  isbn?: string | null;
  status?: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
  data_leaves_device?: boolean;
}
export interface BriefItem {
  display_order: number;
  title: string;
  summary: string;
  related_instrument?: string | null;
  signal: string;
  evidence_refs?: string[];
  data_time?: string | null;
}
export interface CandleSeries {
  instrument: string;
  timeframe?: string;
  bars: OHLCVBar[];
  source_plugin_id: string;
  source_plugin_release: string;
  source_name: string;
  as_of: string;
  is_demo?: boolean;
  limitations?: string[];
}
export type CardSeverity = "info" | "attention" | "warning";
export type ConfirmationMethod = "explicit_ui" | "imported";
export interface CredentialRecord {
  key_id: string;
  last4: string;
  updated_at?: string;
  backend?: CredentialStoreBackend;
}
export type CredentialStoreBackend = "os" | "file";
export interface DecisionEntry {
  decision_id: string;
  user_id: string;
  made_at?: string;
  theme: string;
  decision_summary: string;
  rationale?: string;
  outcome?: string;
  retrospective?: string;
  linked_evidence_ids?: string[];
  plan_id?: string | null;
  inaction_reason?: string | null;
  schema_version?: string;
}
export interface Evidence {
  evidence_id: string;
  tenant_id: string;
  subject_refs: string[];
  evidence_type: EvidenceType;
  source_name: string;
  source_uri?: string | null;
  license_status?: string;
  source_trust_note?: string | null;
  published_at?: string | null;
  observed_at?: string | null;
  collected_at?: string;
  valid_until?: string | null;
  source_dataset_version?: string | null;
  producer_plugin_id?: string | null;
  producer_release?: string | null;
  schema_version?: string;
  summary: string;
  raw_locator?: string | null;
  content_hash: string;
  relation?: EvidenceRelation;
  freshness?: string;
  quality_flags?: string[];
  limitations?: string[];
  status?: EvidenceStatus;
}
export type EvidenceRelation = "supporting" | "contradicting" | "unknown";
export type EvidenceStatus = "active" | "stale" | "retracted";
export type EvidenceType = "quote" | "financial" | "announcement" | "macro" | "analysis" | "learning";
export interface FreshnessPatrolResult {
  checked: number;
  stale: number;
  stale_ids?: string[];
}
export interface Holding {
  holding_id: string;
  user_id: string;
  instrument: string;
  label: string;
  status?: HoldingStatus;
  strategy_note?: string;
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export type HoldingStatus = "holding" | "watchlist";
export interface InvestmentPolicyVersion {
  policy_id: string;
  user_id: string;
  version: number;
  status?: PolicyStatus;
  investment_goal: string;
  horizon_years?: number | null;
  liquidity_needs?: string;
  allowed_markets?: string[];
  allowed_asset_classes?: string[];
  risk_boundaries?: Record<string, unknown>;
  preferred_methods?: string[];
  excluded_methods?: string[];
  observation_conditions?: string[];
  invalidation_conditions?: string[];
  review_due_at?: string | null;
  change_reason?: string;
  source_learning_activity_ids?: string[];
  supersedes_id?: string | null;
  confirmed_at?: string | null;
  confirmation_method?: ConfirmationMethod | null;
  confirmation_summary?: string | null;
  schema_version?: string;
  created_at?: string;
  updated_at?: string;
  revision?: number;
  device_id?: string | null;
  change_event_id?: string | null;
  conflict_policy?: string;
}
export interface InvestorProfile {
  user_id: string;
  investment_goal?: string;
  horizon_years?: number | null;
  liquidity_needs?: string;
  knowledge_self_assessment?: string;
  markets_and_assets?: string[];
  consent?: Record<string, boolean>;
  privacy_settings?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export interface JevRouteDecision {
  mode?: string | null;
  mode_label?: string;
  confidence?: number | null;
  probabilities?: Record<string, number>;
  bypassed_model?: boolean;
  route_state?: string;
  question_id?: string;
  options?: string[];
  note?: string;
  model?: string;
  schema_version?: string;
}
export interface LearningActivity {
  activity_id: string;
  user_id: string;
  unit_id: string;
  unit_type?: LearningUnitType;
  bound_instrument?: string | null;
  bound_instrument_label?: string | null;
  objective: string;
  user_answer?: string;
  reflection?: string;
  completed_at?: string;
  created_at?: string;
  schema_version?: string;
}
export interface LearningGoal {
  goal_id: string;
  user_id: string;
  period_label: string;
  target_count: number;
  completed_count: number;
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export interface LearningUnit {
  unit_id: string;
  user_id: string;
  unit_type?: LearningUnitType;
  title: string;
  objective: string;
  content: string;
  guidance?: string;
  bound_instrument?: string | null;
  bound_instrument_label?: string | null;
  related_conditions?: string[];
  source_plugin_id?: string;
  source_plugin_release?: string;
  data_time?: string;
  schema_version?: string;
}
export type LearningUnitType = "lesson" | "exercise" | "reflection";
export interface LibraryPlan {
  plan_id: string;
  book_ref?: string | null;
  daily_task: string;
  source_chapter: string;
  rationale: string;
  created_at?: string;
  slot?: string;
  renderer?: string;
}
export interface MacroBackgroundRow {
  key: string;
  label: string;
  status?: string;
  latest?: number | null;
  obs_date?: string | null;
  as_of?: string | null;
  source?: string;
  dataset_version?: string | null;
  note?: string;
}
export interface MacroCalendarEvent {
  region: string;
  event_key: string;
  label: string;
  kind: string;
  date_type: string;
  date: string;
  window_end?: string | null;
  note?: string;
  phase?: string;
  linked_series?: string | null;
  actual_value?: number | null;
  actual_unit?: string | null;
  actual_obs_date?: string | null;
  actual_source?: string | null;
  actual_frequency?: string | null;
  annual_background?: Record<string, unknown>[];
  actual_variants?: Record<string, unknown>[];
  actual_variant_label?: string | null;
}
export interface MacroCalendarSnapshot {
  events?: MacroCalendarEvent[];
  days: number;
  generated_at?: string;
}
export interface MacroDimScore {
  dim: string;
  score?: number | null;
  rules_fired?: string[];
  indicators_used?: string[];
}
export interface MacroIndicatorReading {
  indicator: string;
  label: string;
  dim: string;
  status?: string;
  latest?: number | null;
  obs_date?: string | null;
  unit?: string;
  as_of?: string | null;
  source?: string;
  dataset_version?: string | null;
  note?: string;
  frequency?: string | null;
}
export interface MacroPositioning {
  region: string;
  weight_version?: string;
  composite: number;
  band: string;
  near_boundary_note?: string | null;
  dims?: MacroDimScore[];
  limitations?: string[];
  generated_at?: string;
}
export interface MacroPricingRow {
  key: string;
  label: string;
  kind: string;
  status?: string;
  latest?: number | null;
  obs_date?: string | null;
  unit?: string;
  trend_5d?: string | null;
  ref_value?: number | null;
  ref_date?: string | null;
  as_of?: string | null;
  source?: string;
  dataset_version?: string | null;
  note?: string;
}
export interface MacroPricingSnapshot {
  region: string;
  label: string;
  rows?: MacroPricingRow[];
  degraded_reason?: string | null;
  generated_at?: string;
}
export interface MacroSnapshot {
  region: string;
  label: string;
  indicators?: MacroIndicatorReading[];
  background?: MacroBackgroundRow[];
  positioning?: MacroPositioning | null;
  degraded_reason?: string | null;
  generated_at?: string;
}
export interface MacroUserView {
  region: string;
  direction: string;
  horizon: string;
  confidence: string;
  text?: string;
  updated_at?: string;
}
export interface ModelProfile {
  profile_id: string;
  name: string;
  base_url: string;
  model: string;
  credential_ref: string;
  status?: ModelProfileStatus;
  timeout_secs?: number | null;
  created_at?: string;
  updated_at?: string;
}
export type ModelProfileStatus = "未启用" | "使用中";
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
  evidence_refs?: string[];
  created_at?: string;
  data_time?: string | null;
  read?: boolean;
  delivery_status?: string;
  delivery_attempts?: number;
  next_retry_at?: string | null;
  last_delivery_error?: string | null;
  triage?: NotificationTriage | null;
  schema_version?: string;
}
export interface NotificationPendingPage {
  items?: Notification[];
  muted?: boolean;
  mute_reason?: string;
  hidden_count?: number;
}
export interface NotificationTriage {
  impact?: string | null;
  impact_label?: string;
  priority?: number | null;
  priority_label?: string;
  priority_percent?: number | null;
  confidence?: number | null;
  suppressed?: boolean;
  triage_state?: string;
  note?: string;
  model?: string | null;
  schema_version?: string;
}
export interface NotificationTriageReport {
  enabled?: boolean;
  model?: string | null;
  total?: number;
  notified?: number;
  suppressed?: number;
  unannotated?: number;
  impacts?: Record<string, number>;
  priorities?: Record<string, number>;
  suppressed_items?: Notification[];
  note?: string;
  schema_version?: string;
}
export interface OHLCVBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}
export interface PersonalNotifyPrefs {
  in_app_enabled?: boolean;
  external_enabled?: boolean;
  quiet_hours_enabled?: boolean;
  quiet_start?: string;
  quiet_end?: string;
  frequency?: string;
}
export interface PersonalSettings {
  user_id: string;
  display_name?: string;
  avatar_data?: string | null;
  avatar_color?: string;
  default_view?: string;
  notify?: PersonalNotifyPrefs;
  risk_profile?: string;
  style_tags?: string[];
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export interface Plan {
  plan_id: string;
  user_id: string;
  title: string;
  status?: PlanStatus;
  decision_id?: string | null;
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export type PlanStatus = "planned" | "active" | "completed" | "cancelled";
export interface PluginCapability {
  capability_id: string;
  capability_version?: string;
  stability?: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  allowed_resource_types?: string[];
  scope?: string;
  private_namespace_write?: boolean;
  model_access?: boolean;
  notification_proposal?: boolean;
  scheduled_task?: boolean;
  network_allowlist?: string[];
  rate_limit_per_minute?: number;
  max_concurrency?: number;
  max_cpu_seconds?: number;
  max_memory_mb?: number;
  max_execution_seconds?: number;
  max_output_bytes?: number;
  data_leaves_device?: boolean;
  retention?: string;
  deletion_policy?: string;
  side_effects?: string[];
  deterministic?: boolean;
  requires_confirmation?: boolean;
}
export interface PluginCatalogEntry {
  manifest: PluginManifest;
  installation?: PluginInstallation | null;
  resolved_outputs?: Record<string, string>[];
}
export interface PluginInstallation {
  plugin_id: string;
  release_version: string;
  state: PluginInstallationState;
  granted_capabilities?: string[];
  artifact_sha256: string;
  source?: string;
  installed_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export type PluginInstallationState = "available" | "installed" | "enabled" | "disabled" | "revoked";
export interface PluginManifest {
  publisher: string;
  plugin_id: string;
  release_version: string;
  display_name: string;
  description: string;
  plugin_type: string;
  license: string;
  sdk_min?: string;
  sdk_max?: string;
  capabilities?: string[];
  ui_slots?: PluginUiSlot[];
  mount?: string;
  schema_versions?: Record<string, string>;
  entrypoint: string;
  artifact_sha256: string;
  signature: string;
  network_allowlist?: string[];
  side_effects?: string[];
  requires_confirmation?: boolean;
  supports_markets?: string[];
}
export interface PluginUiSlot {
  slot: string;
  level: string;
  schema_version?: string;
}
export type PolicyStatus = "draft" | "active" | "superseded" | "archived";
export interface ResearchRun {
  run_id: string;
  user_id: string;
  status?: RunStatus;
  user_question: string;
  evidence_refs?: string[];
  supporting_refs?: string[];
  contradicting_refs?: string[];
  response_id?: string | null;
  created_at?: string;
  updated_at?: string;
  error_code?: string | null;
}
export interface RunNote {
  id: string;
  who?: string;
  env?: string;
  result?: string;
  status?: string;
}
export type RunStatus = "created" | "planning" | "collecting_evidence" | "analyzing" | "waiting_confirmation" | "composing" | "completed" | "failed" | "cancelled" | "stale";
export interface SpeakerSignal {
  signal_id: string;
  region: string;
  speaker: string;
  event_type?: string;
  event_date: string;
  source_name: string;
  source_url?: string;
  excerpt: string;
  direction?: string | null;
  ai_rationale?: string | null;
  focus_shift?: string | null;
  model?: string | null;
  interpreted_at?: string | null;
  created_at?: string;
}
export interface Thesis {
  thesis_id: string;
  user_id: string;
  instrument: string;
  original_statement: string;
  core_assumptions?: string[];
  supporting_conditions?: string[];
  invalidation_conditions?: string[];
  observation_metrics?: string[];
  status?: ThesisStatus;
  created_at?: string;
  updated_at?: string;
  schema_version?: string;
}
export type ThesisStatus = "active" | "archived";
export interface TodayBrief {
  brief_id: string;
  user_id: string;
  generated_at?: string;
  has_personalization?: boolean;
  headline?: string;
  items?: BriefItem[];
  empty_reason?: string | null;
  policy_version?: string;
  schema_version?: string;
}
export interface UICard {
  card_id: string;
  title: string;
  summary: string;
  severity?: CardSeverity;
  evidence_refs?: string[];
  source_plugin?: string | null;
  slot?: string | null;
  renderer?: UiCardRenderer | null;
  created_at?: string;
  valid_until?: string | null;
  action_mode?: ActionMode;
  supported_actions?: string[];
  limitations?: string[];
}
export type UiCardRenderer = "card" | "summary_row" | "inline" | "silent";
export interface WeeklyReview {
  review_id: string;
  user_id: string;
  generated_at?: string;
  period_label: string;
  thesis_changes?: string[];
  open_research?: string[];
  decisions_summary?: string[];
  next_week_suggestions?: string[];
  principle_change_reason?: string | null;
  sections?: WeeklySection[];
  schema_version?: string;
}
export interface WeeklySection {
  key: string;
  title: string;
  lines?: string[];
  change_to_process?: boolean;
}
