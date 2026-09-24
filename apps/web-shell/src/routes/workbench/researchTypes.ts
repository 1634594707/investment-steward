/**
 * A04（前端设计与架构优化任务路线图 2026-09-19）：研究领域的类型契约自 `routes/ResearchWorkbenchPage.tsx` 原样迁出。
 * 迁移前 `hooks/useResearch` 与 `routes/workbench/` 各子模块都要从页面入口导入这些类型（方案 E03）；
 * 现在页面与子模块同依赖本模块，改子模块不再牵动页面入口。类型定义未作任何修改。
 */

/** v24 结构化判断：情景（多空触发条件）/ 关键价位 / 验证点。模型未输出时为空结构，前端不编造。 */
export interface ReportScenario {
  name: string;
  trigger: string;
  source: string;
  /* —— v27 可证伪化（方案 §3.3）：情景必须能被推翻，否则只是态度声明 —— */
  /** 什么条件下该情景作废。模型未输出时为空串，绝不代拟。 */
  invalidates?: string;
  /** 有效窗口（如「1–3 个月」「三季报披露后」）。 */
  horizon?: string;
  /* —— v28 二次方案 §2.4：无历史基准 → 数值概率一律置 null，只用等级表达（原值留档可审计） —— */
  /** 主观概率 0–1。v28 起无后验基准时恒为 null（后验闭环属 P1），展示口径改用 probability_level。 */
  probability?: number | null;
  /** 概率等级（低/中/高）。模型自报在词表内则采信，否则由原值换算。 */
  probability_level?: string;
  /** 概率依据：本期恒为 "none"（无历史基准）。将来接入后验统计才会变成 "posterior"。 */
  probability_basis?: string;
  /** 概率的原文数值（如 0.4 / 百分数 35 归一后的 0.35），仅供审计，不在主界面展示。 */
  probability_raw?: number | null;
  /** 无历史基准时的说明文案（后端生成，前端如实展示）。 */
  probability_note?: string;
  /** 预期区间；两端都可能为 null（模型未给或用文本描述），前端逐端降级显示。 */
  expected_range?: { low: number | null; high: number | null };
  /** v28：价格区间的依据说明（模型自述，前端并列展示）。 */
  range_basis?: string;
  /* —— v30 四次升级 §3：区间五态（服务端确定性推导，前端只搬运不重判） —— */
  /** estimated=双端有值 | technical_range=仅一端 | unavailable=无区间无依据 | not_applicable=区间由事件验证 | invalid=区间倒挂。 */
  range_status?: string;
  range_status_label?: string;
  /** invalid / not_applicable 时的说明（如「区间倒挂：下限高于上限」）。 */
  range_status_note?: string;
}

export interface ReportLevel {
  price: number | null;
  basis: string;
}

export interface ReportWatchpoint {
  signal: string;
  verify_by: string;
  expected_if_true: string;
  /** v27：能从 verify_by 解析出的确切到期日（ISO）；锚定事件（如「三季报披露后」）留空，不猜。 */
  due_on?: string;
  /** v27：研报内嵌验证点**恒为** pending_review——它是落库原文的一部分（append-only，不回写）。
   * G01 起回填状态不在这里：服务工作台「待复盘」队列把带确切日期的验证点幂等落成判断，
   * 回填结果与状态看队列返回的 `status` / `verification`。 */
  status?: string;
  /* —— Q07（2026-09-19 路线图）：验证点日期的依据与口径（缺失=旧记录，不补不猜） —— */
  /** trading_calendar|disclosed_schedule|input_evidence；空串=模型没交代日期从哪来。 */
  date_basis?: string;
  date_basis_label?: string;
  /** factual=事实时间 | estimated=预计时间 | event=事件锚定 | demoted=因缺依据被降级。 */
  date_kind?: string;
  /** 日期口径说明（如「2026-11-30 无日期依据，已降级为事件锚定表述」）。 */
  date_note?: string;
  /** 被降级的原值（留痕：样例里凭空出现的 2026-11-30）。 */
  date_demoted_from?: string;
  /** Q07：与前一条同文本的信号标为重复（值=被合并条目序号，0/缺失=不重复）；条目本身不删。 */
  duplicate_of?: number;
  /** Q07：重复信号 / 同时点相反预期 / 触发与预期矛盾的核对说明（服务端判定，空串=无问题）。 */
  direction_note?: string;
  /** 事件锚定表述（日期不可信时改用它）。 */
  event_anchor?: string;
  /** 从信号/预期里拆出的假设（Q07：量价齐升 ≠ 量增价稳，各自成条）。 */
  assumptions?: string[];
  /**
   * JV06：优先级分诊结论（`score` 一道题：值不值得现在花一次取数 + 研报额度去查）。
   * **`undefined` / `null` = 这一轮没有分诊**（Jev 未启用 / 总闸关闭 / 未送评），
   * 与「分诊过但没被折叠」是两件事——前者不代表任何语义判断，界面不能渲染成「不急」。
   */
  triage?: WatchpointTriage | null;
}

/** JV06：单条验证点的分诊结论。**三者都留痕**：原始分 / 归一化展示分 / 置信度。 */
export interface WatchpointTriage {
  /** 等级轴上的原始取值（3 级量表为 0–2），**不是 0–100**。 */
  score: number | null;
  /** 归一化展示分 = score/(等级数-1)×100（换算在代码里，非模型输出）。 */
  score_percent: number | null;
  confidence: number | null;
  /** 档位短标签：暂缓 | 可稍后 | 优先。 */
  level_label: string;
  /** 是否判为「暂缓」档且置信度达标 → 折叠进「暂缓」（**仍可展开，不删除**）。 */
  deferred: boolean;
  /** priority | deferred | unannotated。 */
  triage_state: string;
  /** 展示顺序（0 起）。 */
  rank: number;
  /** 判定说明（含为什么没折叠 / 为什么折叠）。 */
  note: string;
  model?: string | null;
  schema_version?: string;
}

/** JV06：验证点分诊回执（`report.jev_followup` / `turn.jev_followup`）。 */
export interface JevFollowupTriage {
  available: boolean;
  total: number;
  /** 判为「暂缓」档且置信度达标的条数（折叠进「暂缓」）。 */
  deferred: number;
  /** 本轮没送出去评的条数。**不等于「不值得查」**。 */
  unannotated: number;
  /** 展示顺序（值为验证点序号，从 1 起）。前端照抄即可，不必各写一份排序逻辑。 */
  order: number[];
  skipped?: { index: number; reason: string; note: string }[];
  model?: string | null;
  levels?: string[];
  confidence_floor?: number;
  note?: string;
  schema_version?: string;
}

/** 单个来源的证据分级：kind 是接入形态，quality 是 A/B/C/D 等级，coverage 是它能覆盖的字段。 */
export interface EvidenceSource {
  id: string;
  kind: string;
  quality: "A" | "B" | "C" | "D" | string;
  label: string;
  coverage: string[];
}

export interface EvidenceQuality {
  version: string;
  sources: EvidenceSource[];
  /** 按等级归类的来源 id，如 {A: [], B: ["S1","S3","S4"], C: ["S2","S5"], D: []}。 */
  tiers: Record<string, string[]>;
  /** 可作为 requires 取值的字段词表（闸门与提示词同源）。 */
  coverage_vocab: string[];
  /** 能支撑核心结论的最低等级（当前为 B）。 */
  core_support_qualities: string[];
  /** 如实说明「当前无任何来源达 A 级」及其原因。 */
  note?: string;
}

/**
 * v30 四次升级 §4：claim 三层拆分（事实/比较/判断）。每层独立走覆盖校验——
 * 事实层只要求来源真实（C 级即可成立），判断层才要求高等级支撑。
 * `support` 由服务端确定性判定，前端只搬运不重判。
 */
export interface ClaimAspect {
  kind: "fact" | "comparison" | "judgement" | string;
  kind_label?: string;
  text: string;
  sources: string[];
  dropped_sources: string[];
  requires: string[];
  missing_coverage: string[];
  evidence_quality?: string | null;
  status?: string;
  support?: "full" | "partial" | "none" | string;
  support_label?: string;
  note?: string;
}

/** 单条核心判断的覆盖校验结论。status 三态决定它能不能作为可发布的结论。 */
export interface ClaimFinding {  claim_id: string;
  text: string;
  /** 经校验真实存在的引用（已剔除不属于本次证据包的编号）。 */
  sources: string[];
  dropped_sources: string[];
  requires: string[];
  unknown_requires: string[];
  missing_coverage: string[];
  /** 所引来源中的最高等级。 */
  evidence_quality: string | null;
  status: "supported" | "downgraded" | "no_source" | string;
  note: string;
  /* —— v28 证据支撑链（二次方案 §2.3） —— */
  /** 模型自述重要度（high/medium/low），用于挑「核心判断」；只透传不判定。 */
  importance?: string;
  /** 模型自评置信度（只透传）。 */
  confidence?: string;
  /** 服务端确定性判定的支撑强度：full / partial / none（不采信模型自评）。 */
  support?: "full" | "partial" | "none" | string;
  support_label?: string;
  /** 服务端算出的可读缺口（与模型自述缺口并列，不互相覆盖）。 */
  missing?: string[];
  /** 模型自述的证据缺口。 */
  model_missing?: string[];
  /** 所引来源数据是否超过该来源类别的时效阈值（缺日期/当天时为 false 且 staleness_checked=false）。 */
  stale?: boolean;
  staleness_checked?: boolean;
  /** 口径层跨度信号（等级跨度 / 单一来源 / 未登记来源），不做内容矛盾检测。 */
  conflict_signals?: { kind: string; detail: string }[];
  /* —— v30 三层拆分：事实/比较/判断逐层校验结果（模型未输出 aspects 时为空数组）。 —— */
  aspects?: ClaimAspect[];
  /* —— JV04：语义支撑层（Jev 逐 claim 判定「所引证据的内容是否真的支撑该判断」）—— */
  /** 该条判断的语义判定回执；未进该层（没引用 / 超预算 / 该层未运行）时缺失。 */
  jev?: ClaimJevFinding;
  /** 支撑强度被哪一层改写过：`jev_semantic` = 语义层降级。确定性口径降级时不带该字段。 */
  support_source?: string;
}

/** JV04 单条判断的语义判定回执（Jev 只回类型化答案，不做文本生成）。 */
export interface ClaimJevFinding {
  /** 语义支撑：support / partial / unsupport / irrelevant；未取得时为 `""`。 */
  support: string;
  support_label: string;
  /** choice 题才有 confidence；noul 题没有（官方口径）。 */
  confidence?: number | null;
  /** 编造风险三路：yes / no / uncertain（noul 中间地带不硬二值化）。 */
  invented?: "yes" | "no" | "uncertain" | null;
  invented_value?: number | null;
  /** 本次实际比对了哪些来源的内容。 */
  sources_checked: string[];
}

/** JV04 语义支撑层整体回执（未启用/不可用时 available=false，如实标注而不是静默）。 */
export interface ClaimJevLayer {
  available: boolean;
  evaluated: number;
  /** 没进该层的判断及原因：no_cited_source / over_claim_limit / over_state_budget。 */
  skipped: { claim_id: string; reason: string }[];
  /** 被语义层降级的判断 id。 */
  downgraded: string[];
  /** 语义认为强度不足但按当前口径**只标注不降级**的判断 id（R6 未标定前不改判定）。 */
  weakened?: string[];
  /** 语义判定「含所引证据中找不到的具体事实」的判断 id。 */
  invented?: string[];
  /** 响应回的真实模型版本号（不是请求别名）。 */
  model?: string;
  schema_version?: string;
  partial_downgrades?: boolean;
  note?: string;
}

export interface ClaimFindings {
  claims: ClaimFinding[];
  total: number;
  supported: number;
  downgraded: string[];
  no_source: string[];
  /** 无 claim 时为 null —— 不假装通过（方案 §3.1 硬边界）。 */
  core_conclusion_supported: boolean | null;
  /** J03（桌面端升级路线图 2026-09-18）：三态 supported/partial/unsupported；旧档案缺省。 */
  core_conclusion_state?: "supported" | "partial" | "unsupported" | null;
  /** J03：核心判断的支撑分母（N/M）。 */
  core_support_counts?: { supported: number; total: number };
  core_support_note?: string;
  warnings: string[];
  /* —— v28 汇总（口径可复算） —— */
  version?: string;
  support_counts?: { full: number; partial: number; none: number };
  core_claims?: string[];
  core_downgraded?: string[];
  stale_claims?: string[];
  staleness_checked?: boolean;
  /* —— JV04：语义支撑层整体回执（未接入/未启用时缺失）。 —— */
  jev?: ClaimJevLayer;
  /** 语义层产出的告警（已并入 `warnings`，此处保留分栏便于前端区分来源）。 */
  jev_warnings?: string[];
}

/** v28 首屏结论卡（二次方案 §2.1/§2.2）：纯聚合既有结构化字段，零新判断；旧记录缺失时整块不渲染。 */
export interface ReportDecisionCard {
  version: string;
  symbol: string;
  conclusion: {
    statement: string;
    direction: string;
    core_conflict: string;
    confidence: string;
  };
  evidence_strength: {
    counts: Record<string, number>;
    total_sources: number;
    core_min_quality: string | null;
    support_counts?: { full: number; partial: number; none: number };
    core_conclusion_supported?: boolean | null;
    /** J03：首屏结论卡带三态与分母（屏显与导出统一「部分支撑（N/M）」形态）。 */
    core_conclusion_state?: "supported" | "partial" | "unsupported" | null;
    core_support_counts?: { supported: number; total: number };
    note?: string;
  };
  valuation_label: string;
  /** 同业样本数缺失等降级说明（结论卡如实标注，不冒充完整）。 */
  valuation_label_note?: string;
  max_support: { text: string; source: string; as_of: string } | null;
  max_counter: { text: string; source: string; as_of: string } | null;
  /** 反方检查未执行/失败时的原因说明。 */
  counter_check_reason: string;
  next_action: {
    signal: string;
    verify_by: string;
    due_on: string;
    expected_if_true: string;
    /** 无确切日期 = 锚定事件（由事件触发复盘，不猜日期）。 */
    event_anchored: boolean;
    status: string;
  } | null;
  downgrade_notes?: string[];
  /* —— v30 四次升级 §3：三行分项（基本面/估值/技术面，按 requires 字段确定性归类）。 —— */
  /** 模型未输出 claims 时为空数组 → 分项区整体不渲染（不冒充有分项）。 */
  pillar_rows?: DecisionPillarRow[];
  /* —— v32 价值投资升级（2026-09-13 方案 §二）：驾驶舱三行结论词 + 正常化估值状态。 —— */
  cockpit?: ReportCockpit;
  /** 四态估值状态（undervalued/fair/expensive/undetermined；服务端确定性推导，旧记录为空串）。 */
  valuation_status?: string;
  valuation_status_label?: string;
  /** 状态被服务端降级（如低 PE 自称低估）时的原因说明。 */
  valuation_status_note?: string;
  normalized_earnings?: NormalizedEarnings;
}

/** v32 首屏驾驶舱三行结论词（§二.1）：三行判断 + 适用期限；模型未输出时各行为空串。 */
export interface ReportCockpit {
  fundamental?: string;
  valuation?: string;
  technical?: string;
  horizon?: string[];
}

/** v32 正常化盈利（§二.3）：剔除一次性/周期因素后的可持续口径；value=null 表示未验证。 */
export interface NormalizedEarnings {
  value: number | null;
  basis: string;
  confidence: string;
}

/** v30 驾驶舱单行分项：该柱的判断数与支撑强度计数 + 代表判断。 */
export interface DecisionPillarRow {
  pillar: string;
  label: string;
  total: number;
  full: number;
  partial: number;
  none: number;
  /** importance 最高的判断原文（截断 60 字）。 */
  representative: string;
}

/** v30 告警四级分组（服务端确定性分组，前端只渲染；handled 恒 false，处理留痕属后续闭环）。 */
export interface CategorizedWarning {
  category: "blocking" | "evidence" | "computation" | "format" | string;
  category_label?: string;
  message: string;
  impact?: string;
  action?: string;
  handled?: boolean;
}

/** v30 摘要预算重写留痕：needed=true 表示原摘要超 180 字硬上限；rewritten=false 时保留原文交人工复核。 */
export interface SummaryRewrite {
  needed?: boolean;
  rewritten?: boolean;
  original_chars?: number;
  rewritten_chars?: number;
  hard_limit?: number;
  detail?: string;
}

/** 三态质量状态：complete=完整 / needs_review=待复核（仅软告警）/ incomplete=不完整（有阻断项）。 */
export type QualityStatus = "complete" | "needs_review" | "incomplete" | string;

/** 阻断项：缺必需小节 / 无正文 / 无真实引用 / 全部核心判断无来源支撑（不含字数阈值，§八.4）。 */
export interface QualityBlocker {
  code: "missing_section" | "report_empty" | "no_citations" | "claims_all_no_source" | string;
  section?: string;
  message: string;
}

/** 必需小节实质状态：present=false 表示「未生成或只有标题没有内容」，前端显示红色「未生成」。 */
export interface SectionState {
  section: string;
  chars: number;
  present: boolean;
}

/** 生成留痕（§2.2）：排查「新报告对应旧提示词」「模式在哪一层回落」一类问题的可追责快照。 */
export interface GenerationTrace {
  prompt_version?: string;
  model?: string;
  stage_models?: Record<string, string>;
  stages_completed?: string[];
  report_mode?: string;
  source_keys?: string[];
  summary_rewrite?: SummaryRewrite;
  generated_at?: string;
}

/** v31 §2.4 修复留痕：按失败类型定向修复一轮的执行结果（attempted/repaired + 策略与原因）。 */
export interface ReportRepair {
  attempted?: boolean;
  repaired?: boolean;
  strategy?: "full_rerun" | "section_only" | "summary_only" | string;
  reason?: string;
  detail?: string;
}

/** v31 §2.4 修订历史条目：每次修复升 report_revision，保存原因、策略与结果。 */
export interface RevisionHistoryEntry {
  revision: number;
  reason: string;
  strategy: string;
  strategy_label?: string;
  changes?: string;
  result?: string;
  result_label?: string;
  repaired_at?: string;
}

/** v28 一句话结论（首屏结论卡第一行的数据来源；独立字段便于复用）。 */
export interface ReportConclusion {
  statement: string;
  direction: string;
  core_conflict: string;
  confidence: string;
}

/** v27 轻量反方检查：本节只做一次短调用，失败不阻断报告交付，故 ok 可能为 false。 */
export interface CounterCheck {
  ok: boolean;
  requested?: boolean;
  strongest_support?: string;
  strongest_counter?: string;
  /* —— v28：支撑/反证必须带来源与日期，且只能是本次报告已引用的真实来源（虚构的一律被服务端剔除） —— */
  strongest_support_source?: string;
  strongest_support_date?: string;
  strongest_counter_source?: string;
  strongest_counter_date?: string;
  /** 被服务端剔除的非真实来源 id（模型引用了但本次证据包里没有）。 */
  dropped_sources?: string[];
  necessary_conditions?: string[];
  most_misread_sentence?: { quote: string; why: string };
  /** 结论对反证的耐受度。 */
  verdict_robustness?: "robust" | "mixed" | "fragile" | string;
  prompt_version?: string;
  /** ok=false 时的原因（模型调用失败 / 输出无法解析）。 */
  detail?: string;
}

/** G01：验证点的回填记录（`POST /judgments/{id}/verify` 只追加的那一条）。 */
export interface ReviewVerification {
  verification_id?: string;
  judgment_id?: string;
  result?: "verified" | "refuted" | "insufficient_data" | string;
  /** 实际结果描述（人工填写，可空）。 */
  outcome?: string;
  metrics?: Record<string, number>;
  checked_at?: string;
}

/** v27 待复盘验证点：GET /ai-research/review-queue 的派生结果。
 * G01（桌面端升级路线图 2026-09-18）：带确切日期的验证点在读路径上被**幂等**落成判断，
 * 因此本队列自 G01 起可回填——`judgment_id` 非空即可走 `POST /judgments/{id}/verify`。 */
export interface ReviewQueueItem {
  report_id?: string;
  /** v39 Q21：复盘五态（suggest_observe|tracked|pending_verify|verified|unverifiable）。
   * 「已加入跟踪」只在服务端确实落成可回填判断时出现；系统不自动监控事件。 */
  tracking_state?: string;
  tracking_label?: string;
  tracking_note?: string;
  symbol?: string;
  title?: string;
  model?: string;
  confidence?: string;
  prompt_policy_version?: string;
  report_generated_at?: string;
  signal?: string;
  verify_by?: string;
  /** 确切到期日（ISO）；锚定事件为空串。 */
  due_on?: string;
  expected_if_true?: string;
  /** G01 起为判断状态：pending=待回填 / verified=成立 / refuted=失效 / insufficient_data=无数据。 */
  status?: string;
  bucket?: "overdue" | "scheduled" | "event_anchored" | string;
  /** 距到期天数；锚定事件为 null。 */
  days_until?: number | null;
  scenarios?: { name: string; invalidates: string; horizon: string; probability: number | null }[];
  /** G01：可回填的判断 id（锚定事件没有日期、不落库，此处为 null）。 */
  judgment_id?: string | null;
  /** G01：最近一次回填记录（未回填为 null）。 */
  verification?: ReviewVerification | null;
  /** G01：未落库的原因（仅在 `judgment_id` 为空时有值，如实说明不猜日期）。 */
  materialization_note?: string;
}

export interface ReviewQueue {
  ok: boolean;
  today: string;
  counts: { overdue: number; scheduled: number; event_anchored: number };
  item_count: number;
  items: ReviewQueueItem[];
  /** v39 Q21：五态汇总（含「本表不承诺自动监控」的口径说明）。 */
  tracking?: {
    version?: string;
    counts?: Record<string, number>;
    summary?: string;
    note?: string;
    auto_monitoring?: boolean;
  } | null;
  /** G01：闭环进度。分母都是**已落库的判断**（事件锚定点没有日期、不计入分母）。 */
  closure?: { materialized: number; filled: number; overdue_unfilled: number; not_materialized: number };
  note?: string;
}

/** v24 档 A/B/C：估值结构化块（市盈率一类）。模型未输出估值时各字段为空串。 */
export interface ReportValuation {
  pe_ttm: string;
  pe_percentile: string;
  peer_position: string;
  verdict: string;
  /** J01（2026-09-18 桌面端路线图）：状态为「无法判断」时服务端把贵贱结论词归一为「无数据」，
   * 模型原话留在 `verdict_raw`、归一原因在 `verdict_note`（不删原话，只停止并排打脸）。 */
  verdict_raw?: string;
  verdict_note?: string;
  basis: string;
  /* —— v32 正常化盈利与估值安全边际（2026-09-13 方案 §二.3；旧记录缺失时如实降级展示） —— */
  /** 正常化盈利：value=null 表示未完成正常化验证（低 PE 分位不自动等于低估）。 */
  normalized_earnings?: NormalizedEarnings;
  /** 悲观/基准/乐观三情景估值（bear/base/bull：盈利×倍数→公允价值）。 */
  valuation_cases?: { name: string; name_label?: string; earnings: number | null; multiple: number | null; fair_value: number | null }[];
  /** 当前价相对基准公允价值的安全边际（-1~1 小数；null=推不出）。 */
  margin_of_safety?: number | null;
  /** 四态估值状态（服务端确定性推导；可能带降级说明 valuation_status_note）。 */
  valuation_status?: string;
  valuation_status_raw?: string;
  valuation_status_note?: string;
  /** 估值口径限制（1-3 条）。 */
  valuation_limitations?: string[];
  /** Q05（2026-09-19 路线图）：估值三态证据视图——区分「指标没取到」与
   * 「指标有了但合理价值未评估」，避免有 PE/PB 的样例被笼统显示成「无数据」。
   * 旧记录缺失时由前端 `valuationEvidenceDisplay` 按同一规则回退推导（只换措辞，不改判）。 */
  valuation_evidence?: ValuationEvidenceView;
}

/** v21 AI 个股研究报告（POST /evidence/stock-research-report 即时返回，不落库）。 */
export interface StockReport {
  ok: true;
  symbol: string;
  title: string;
  /** v22 持久化后的报告 id（落库成功才有；协同装配稿可能缺失 → 追问页签按需降级）。 */
  report_id?: string;
  /** v22 质量升级：结论先行的执行摘要（模型未给时为空串）。 */
  executive_summary?: string;
  report: string;
  citations: string[];
  limitations: string[];
  confidence: string;
  model: string;
  source_keys: string[];
  source_errors: Record<string, string>;
  generated_at: string;
  /** B04（2026-09-15 路线图）：模型初稿调用耗时（毫秒，实测口径为 draft 一次调用；旧记录缺失时不显示）。 */
  latency_ms?: number;
  /* v24 质量闸门痕迹（旧记录可能缺失，缺失时如实降级显示） */
  /** 被剔除的虚假引用（不属于本次真实取到的来源）。 */
  citation_dropped?: string[];
  /** 本次真实可引用的来源 id（S1 恒在，S2/S3/S4/S5 取决于取数成败）。 */
  available_citations?: string[];
  /** 形式质量告警（长度 / 必需小节 / confidence / limitations 偏差），如实展示不阻断交付。 */
  quality_warnings?: string[];
  /* —— v30 四次升级 §2/§3：摘要预算 + 告警四级分组（旧记录缺失时回退平铺展示）。 —— */
  /** 执行摘要字数（闸门口径）。 */
  summary_chars?: number;
  /** 摘要超限重写留痕（仅单股端点有；collab 固定 standard 不重写）。 */
  summary_rewrite?: SummaryRewrite;
  /** 四级分组告警（blocking/evidence/computation/format，每条带影响与建议动作）。 */
  categorized_warnings?: CategorizedWarning[];
  /** 正文中识别到的小节标题。 */
  report_sections?: string[];
  /** 正文字数（去空白）。 */
  report_chars?: number;
  /** 生成本报告时的提示词策略版本。 */
  prompt_policy_version?: string;
  scenarios?: ReportScenario[];
  levels?: { support?: ReportLevel[]; resistance?: ReportLevel[] };
  watchpoints?: ReportWatchpoint[];
  /** JV06：验证点优先级分诊回执。**未启用时为 null**（与 `jev` 同一惯例：没跑就明说没跑）。 */
  jev_followup?: JevFollowupTriage | null;
  /** v24 档 A/B/C：估值结构化块（PE/PB 现值、历史分位、同业位置、贵贱判断）。 */
  valuation?: ReportValuation;
  /** v32 首屏驾驶舱三行结论词（基本面/价值估值/技术状态 + 适用期限；旧记录缺失时首屏退回支撑计数）。 */
  pillar_verdicts?: ReportCockpit;
  /* —— v27「可信交付」 —— */
  /** 证据质量分级（逐来源 A/B/C/D + 覆盖字段）。旧记录无此字段时整块不渲染。 */
  evidence_quality?: EvidenceQuality;
  /** 每条核心判断的覆盖校验结论（引用真实存在 → 引用是否支撑该判断）。 */
  claims?: ClaimFinding[];
  /** 覆盖校验汇总：supported / downgraded / no_source 计数与 warnings。 */
  claim_findings?: ClaimFindings;
  /**
   * JV04 语义支撑层回执。**未接入时为 undefined**——前端据此区分「这一层没跑」与「跑过且通过」，
   * 不拿空白当通过。
   */
  jev?: ClaimJevLayer;
  /** 轻量反方检查结果（单次短调用）。ok=false 表示未完成，如实展示原因，不影响报告本身。 */
  counter_check?: CounterCheck;
  /* —— v28 二次优化（P0） —— */
  /** 一句话结论（结论卡第一行；模型未输出时各字段为空串）。 */
  conclusion?: ReportConclusion;
  /** 核心判断未获完整支持 / 引用数据过期时的摘要旁提示（**不改写 executive_summary**，并列展示）。 */
  summary_downgrade_notes?: string[];
  /** 首屏结论卡：结论 / 证据强弱 / 最大支持 / 最大反证 / 下一动作。旧记录缺失时整块不渲染。 */
  decision_card?: ReportDecisionCard;
  /** 各来源最新数据日期（S1–S5 → ISO 前缀）；时效判定的依据快照。 */
  source_asof?: Record<string, string>;
  /* —— v29 三次升级（§2 字数预算 / §3 图表数据包 / §4 来源元数据） —— */
  /** 研报模式（quick/standard/deep）：本篇按哪档字数预算做软告警。 */
  report_mode?: string;
  /** J06：该模式的字数预算（min/max）——「未达档位深度」判定与标签的数据源。 */
  report_mode_budget?: { min: number; max: number };
  /* —— v31 质量恢复（2026-09-13 方案 §2.2/§2.3/§八.4） —— */
  /** 三态质量状态：complete / needs_review / incomplete（服务端闸门判定，前端只搬运不重判）。 */
  quality_status?: QualityStatus;
  /** 质量状态中文标签（完整 / 待复核 / 不完整）。 */
  quality_status_label?: string;
  /** 阻断项清单（incomplete 时非空；每条带 code 与可读 message）。 */
  quality_blockers?: QualityBlocker[];
  /** 缺失的必需小节名（技术面/消息面/基本面/综合判断）。 */
  missing_sections?: string[];
  /** 四大必需小节的实质状态（present=false → 目录与正文红色「未生成」占位）。 */
  section_states?: SectionState[];
  /** 生成留痕：提示词版本 / 模型 / 已完成阶段 / 模式 / 证据来源。 */
  generation_trace?: GenerationTrace;
  /** v31 §2.4：定向修复留痕（attempted/repaired/strategy）。 */
  repair?: ReportRepair;
  /** v31 §2.4：修订版本号（初稿=1；每次修复 +1）。 */
  report_revision?: number;
  /** v31 §2.4：修订历史（原因/策略/结果），与 revision 一一对应。 */
  revision_history?: RevisionHistoryEntry[];
  /** 行内引用 vs 引用清单一致性检查结论（§八.3）。 */
  citation_consistency?: {
    text_refs: string[];
    declared: string[];
    undeclared_used: string[];
    unavailable_used: string[];
  };
  /** true = 不完整报告的草稿标记：可回看，但不混入正式报告数量（§八.4）。 */
  is_draft?: boolean;
  /** 各来源提供方与取数时刻（与 source_asof 并列展示；旧记录缺失时该列不渲染）。 */
  source_meta?: Record<string, SourceMetaEntry>;
  /** Q09（2026-09-19 路线图）：来源目录——编号/名称/可用链接/数据日期/获取时间/口径限制，
   * 让导出里的 S1…S5 能回查到对应来源记录；缺链接一律如实标注，不伪造链接。 */
  source_catalog?: SourceCatalogEntry[];
  /** v39 Q19：四项关键研究项完成度（缺失即显式标出，字数只作辅助提示）。 */
  research_completeness?: ResearchCompleteness | null;
  /** v39 Q20：反方检查触发的摘要加注与留痕（原话保留在 summary_original）。 */
  summary_overreach?: SummaryOverreach | null;
  /** v39 Q16：盈利增长拆解（业务量/价格/成本效率/低基数/非经常性损益）。 */
  earnings_growth?: EarningsGrowthRow[];
  /** v39 Q17：现金流质量（只有覆盖倍数时结论自动降档）。 */
  cash_flow_quality?: CashFlowQualityBlock | null;
  /** v39 Q18：估值分化解释与可比样本口径。 */
  valuation_divergence?: ValuationDivergence | null;
  /** 单季财务序列（revenue/net_profit/operating_cash_flow，服务端累计差分，缺失报告期 value=null）。 */
  quarterly?: Record<string, QuarterlyPoint[]>;
  /** 价格量能图数据包（前复权日线 + MA）；空对象 = 取数失败，前端如实显示不可用。 */
  price_chart?: PriceChartPack;
  /** 估值分位数据包（历史分位 + 同业中位，都是取数结果）。 */
  valuation_chart?: ValuationChartPack;
  /** 协同流水线修订稿专有：本稿相对初稿改了什么。 */
  revision_notes?: string;
}

/** 单季财务序列点：value=null = 累计值无法拆为单季（断点保留，不补零）。 */
export interface QuarterlyPoint {
  period_end: string;
  /** 如 2026Q2。 */
  label: string;
  value: number | null;
  /** 来源披露的累计原值。 */
  cumulative: number;
  /** true = 相邻报告期差分所得；false = Q1/断档后首期的披露原值。 */
  is_derived: boolean;
  /** 单季同比（与去年同季单季比；缺去年同季为 null，不用累计同比冒充）。 */
  yoy?: number | null;
}

export interface PriceChartPack {
  adjust?: string;
  provider?: string;
  candles?: { date: string; open: number | null; high: number | null; low: number | null; close: number | null; volume: number | null }[];
  ma5?: (number | null)[];
  ma20?: (number | null)[];
  ma60?: (number | null)[];
}

export interface ValuationChartPack {
  trade_date?: string;
  pe_ttm?: number | null;
  pb_mrq?: number | null;
  ps_ttm?: number | null;
  /** 历史分位（0–100）：键 pe_ttm/pb_mrq/ps_ttm，缺哪个就少哪个。 */
  percentiles?: Record<string, number | null>;
  percentile_window_bars?: number;
  percentile_window_from?: string;
  peer_median?: Record<string, number | null>;
  peer_count?: number;
}

export interface SourceMetaEntry {
  provider?: string;
  retrieved_at?: string;
}

/** v21 AI 方向研判（POST /evidence/direction-research）；v33（2026-09-14 路线图 B 组）升级契约。
 * 旧记录只有 direction_summary + 简化 stock_pool（含模型估价 reference_price，仅历史保留）。 */
export interface DirectionPoolCandidate {
  symbol_raw?: string;
  symbol: string;
  name: string;
  /** M1-B01：代码是否由服务端按本次估值证据回填（true 时前端可注明「服务端补码」）。 */
  symbol_backfilled?: boolean;
  /** M1-B01：回填来源标记（当前为 `evidence_name_index`）。 */
  symbol_backfill_source?: string;
  exchange?: string;
  sector?: string;
  /** B12：产业链位置 / 业务关联 / 业绩兑现路径（新报告必填，旧记录缺省）。 */
  value_chain_position?: string;
  business_link?: string;
  profit_path?: string;
  support_refs?: string[];
  counter_evidence?: string;
  gaps?: string[];
  /** B09：证券身份三态（服务端经 instruments 归一校验）。 */
  identity_status?: "verified" | "unverified" | "invalid" | string;
  identity_status_label?: string;
  identity?: { key: string; kind: string; market: string; display: string };
  /** B10：三种资格分列（身份 / 业务关联 / 行情可扫描）。 */
  qualification?: { identity: string; business_relevance: string; market_data: string; market_data_note?: string };
  duplicate_of_pool?: boolean;
  /* —— v35 B05：候选池估值回链（风格/复合主题；失败保留但标注缺失） —— */
  /** 估值证据来源定位（`raw_locator_for_valuation`）；空串表示未回链。 */
  valuation_ref?: string;
  /** 估值结构化快照（当前值 + 历史分位 + 同业位次）；未回链为 null。 */
  valuation?: {
    trade_date?: string;
    board_name?: string;
    pe_ttm?: number | null;
    pb_mrq?: number | null;
    ps_ttm?: number | null;
    pe_ttm_percentile?: number | null;
    pb_mrq_percentile?: number | null;
    percentile_window_bars?: number;
    peer_median?: Record<string, number | null>;
    peer_rank?: Record<string, number>;
    peer_rank_basis?: Record<string, number>;
    summary?: string;
  } | null;
  /** v35 B05 估值回链状态：attached / failed / cooldown / unavailable / not_applicable。 */
  valuation_status?: string;
  valuation_status_label?: string;
  valuation_error?: string;
  /* —— v36 C02：所属板块的系统参考分类与象限告警（候选池象限约束） —— */
  /** 候选所属板块的名称（由服务端按板块名双向子串匹配得出；无法归属时缺省）。 */
  pool_board_name?: string;
  /** 所属板块的系统参考分类（低估候选/深跌未反转/高位/盈利周期顶/待定）。 */
  pool_board_judgment?: string;
  /** true = 该候选来自非「低估候选」象限且正文无论证推翻 → 候选池代表性告警。 */
  pool_quadrant_violation?: boolean;
  /** 正文中针对该板块的推翻论证片段（存在时违规解除，此处留痕）。 */
  pool_override_argument?: string;
  /* —— 旧记录兼容字段 —— */
  reference_price?: string;
  reason?: string;
}

/** B04（2026-09-15 方向研判路线图）：方向研判证据快照条目（来源 / 数据截至 / 抓取时间逐条可查）。 */
export interface DirectionEvidenceItem {
  evidence_id: string;
  source_type: string;
  source: string;
  title: string;
  content: string;
  retrieved_at: string;
  event_id?: string;
  /* —— v34 板块行情层 —— */
  sector_code?: string;
  /* —— v35 B02/B03 板块估值横截面层：榜单排名 / 板块名 / 数据截至 / 代表股分位附录 —— */
  sector_name?: string;
  rank?: number;
  trade_date?: string;
  /* —— v36 A02/A04：四维分类与质量初筛 —— */
  /** 系统参考分类（低估候选/深跌未反转/高位/盈利周期顶/待定；模型可推翻，须给数值论证）。 */
  valuation_judgment?: string;
  /** 分类依据文本（逐维引用数值，如「PB 分位 96.6% ≥ 70% → 高位」）。 */
  valuation_judgment_rationale?: string;
  /** true = 亏损面过闸被排除出深取名单，本条为「对照清单」条目（无分位附录）。 */
  prescreen_excluded?: boolean;
  percentile_appendix?: {
    representative_count: number;
    comparable_count: number;
    pb_percentile_median?: number | null;
    pe_percentile_median?: number | null;
    lowest_three?: {
      symbol: string;
      name: string;
      pb_mrq_percentile?: number | null;
      pe_ttm_percentile?: number | null;
      trade_date?: string;
    }[];
    incomparable_symbols?: string[];
    disclaimer?: string;
  };
}

/** B06：方向反方审查（受影响结论 + 候选质疑）。 */
export interface DirectionCounterCheck {
  ok: boolean;
  requested?: boolean;
  detail?: string;
  strongest_counter?: string;
  affected_conclusions?: { id: string; effect: string; reason: string }[];
  pool_challenges?: { symbol: string; challenge: string }[];
  dropped_refs?: string[];
  verdict_robustness?: string;
  model?: string;
}

export interface DirectionReport {
  ok: true;
  topic: string;
  /** 正文 markdown（v33 起与 `report` 同值；旧字段名保留兼容历史记录）。 */
  direction_summary: string;
  report?: string;
  title?: string;
  executive_summary?: string;
  catalysts: string[];
  risks: string[];
  stock_pool: DirectionPoolCandidate[];
  /* —— v33 新契约（旧记录缺省时前端按旧版渲染） —— */
  core_judgments?: { id: string; text: string; kind: string; kind_label?: string; support_refs?: string[]; confidence: string; missing?: string[] }[];
  data_gaps?: string[];
  next_verification?: string;
  /** B01（2026-09-15 方向研判路线图）：两种诚实输出状态（evidence=证据研判 / knowledge=知识概览）。 */
  research_mode?: "evidence" | "knowledge" | string;
  mode?: string;
  mode_label?: string;
  quality_status?: QualityStatus;
  quality_status_label?: string;
  quality_blockers?: QualityBlocker[];
  quality_warnings?: string[];
  missing_sections?: string[];
  section_states?: { section: string; chars: number; present: boolean }[];
  report_chars?: number;
  summary_chars?: number;
  evidence_snapshot?: DirectionEvidenceItem[];
  evidence_unavailable?: string;
  counter_check?: DirectionCounterCheck;
  generation_trace?: {
    prompt_version?: string;
    model?: string;
    profile_id?: string;
    profile_name?: string;
    requested_profile_id?: string | null;
    stages_completed?: string[];
    mode?: string;
    research_mode?: string;
    evidence_count?: number;
    /* —— v35 A01：主题类型判定留痕 —— */
    theme_kind?: string;
    theme_kind_label?: string;
    style_terms?: string[];
    /* —— v36 A02：板块四维分类留痕（系统参考分类，模型可推翻） —— */
    board_judgments?: Record<string, string>;
    generated_at?: string;
  };
  /* —— v35 A01/A02/C03：主题类型（产业/风格/复合/通用）+ 风格词 + 四类缺口结构化留痕 —— */
  theme_kind?: "industry" | "style" | "composite" | "generic" | string;
  theme_kind_label?: string;
  style_terms?: string[];
  style_gap_entries?: { code: string; label: string; text: string }[];
  /* —— v36 A02/C02/C03：板块四维分类 + 候选池象限约束 + 分类判定引用闸门 —— */
  /** 板块名 → 系统参考分类（低估候选/深跌未反转/高位/盈利周期顶/待定）。 */
  board_judgments?: Record<string, string>;
  /** 候选池象限违规候选（来自非低估象限且正文无论证推翻）；旧报告缺省时整块不渲染。 */
  pool_quadrant_violations?: { symbol: string; sector: string; board: string; label: string }[];
  /** 分类标签表述但同小节无 [E#] 引用/引用不含该板块四维数值的小节。 */
  unsupported_judgment_claims?: { section: string; claims: string[]; refs: string[]; reason: string; boards?: string[] }[];
  model: string;
  generated_at: string;
  report_id?: string;
  is_draft?: boolean;
  /* —— M3（v40 D01–D08）：报告形态 / 前置比较表 / 双池 / 确定性检查留痕（旧报告缺省不渲染） —— */
  /** D01：报告形态（full=完整研判 / partial=局部研判 / stage=阶段性研究）。 */
  report_form?: "full" | "partial" | "stage" | string;
  report_form_label?: string;
  form_required_sections?: string[];
  /** D01：形态非必需、因证据不足未展开的小节（软告警，不阻断交付）。 */
  missing_optional_sections?: string[];
  /** D02：前置行业比较表（先研究谁 / 依据 / 等待什么变化）。 */
  industry_comparison?: {
    industry: string;
    valuation_evidence?: string;
    profit_change?: string;
    catalyst?: string;
    horizon?: string;
    counter_evidence?: string;
    research_priority?: string;
    priority_basis?: string;
    evidence_completeness?: string;
  }[];
  /** D06：低估候选池（资格全通过）与待验证研究池（附缺口与验证动作）。 */
  low_valuation_pool?: DirectionPoolCandidate[];
  research_pool?: DirectionPoolCandidate[];
  /** D05：反方审查的定稿执行留痕。 */
  counter_check_application?: {
    applied?: boolean;
    reason?: string;
    policy_version?: string;
    removed_judgment_ids?: string[];
    downgraded_judgment_ids?: string[];
    annotated_sentences?: number;
    annotation_fallback_hits?: number;
    annotation_misses?: string[];
    body_block_entries?: number;
    summary_revised?: boolean;
    risks_appended?: number;
    catalysts_untouched_reason?: string;
    strongest_counter?: string;
  };
  /** D03：跨层因果（宏观 → 盈利，同句无中间环节证据）命中。 */
  cross_layer_claims?: { section: string; sentence: string; macro_terms: string[]; profit_terms: string[] }[];
  /** D04：周期板块判低估但未披露正常化口径。 */
  cycle_normalization_gaps?: { board: string; reason: string }[];
  /** D07：引用编号与句内数值不对应。 */
  reference_support_problems?: { section: string; refs: string[]; numbers: string[]; sentence: string }[];
  /** D08：同一缺口在多个小节重复出现。 */
  repeated_gaps?: { gap: string; sections: string[] }[];
  /** D06：待验证对象被表述在低估候选语境中的位置。 */
  research_pool_mislabeled?: string[];
}

/** v22 已持久化的 AI 研究产出（列表行；payload 即端点完整响应，查看时直接回填）。 */
export interface AiReportItem {
  report_id: string;
  kind: "direction" | "stock";
  subject: string;
  generated_at: string;
  title?: string;
  topic?: string;
  symbol?: string;
  [key: string]: unknown;
}

/** 追问携带的补充材料（服务端归一后的快照；用户材料一律 verified=false）。 */
export interface FollowUpSupplement {
  supplement_id: string;
  origin: "user" | "system_event" | string;
  text: string;
  source_name: string;
  url: string;
  event_date: string;
  input_at: string;
  verified: boolean;
  trust_label: string;
  event_id?: string;
  source_provider?: string;
}

/** 事实归一化结果（§3.2：主体/事件/日期/原文完整性/来源可信度/与标的关系，全部确定性归一）。 */
export interface FollowUpFact {
  supplement_id?: string;
  subject: string;
  event: string;
  date: string;
  date_is_input_time: boolean;
  complete: boolean;
  source_credibility: string;
  relation: string;
  trust_label?: string;
}

/* ==========================================================================
   v39（2026-09-19 研报与追问质量路线图 P0/P1）：追问契约新增字段的类型。
   全部字段**可选**——1.1 版契约的历史附录落在同一张表里，缺字段必须回退渲染，
   绝不能因为新契约就把旧记录判成损坏，也不能显示 undefined。
   ========================================================================== */

/** 受影响判断的展示行（Q04）：`claim_name` 由后端从原报告带来（编号配合判断名称展示）。 */
export interface FollowupClaim {
  claim_id: string;
  /** 原报告里该编号对应的判断原文；旧记录可能缺失。 */
  claim_name?: string;
  effect: string;
  effect_raw?: string;
  reason: string;
}

/** Q06：追问引用的每个价位都带截至日期与适用窗口（9 月 MA20 不能再当 11 月实时阈值）。 */
export interface FollowupPriceRef {
  level: number;
  /** close|ma|support|resistance|other（越界值后端已归 other）。 */
  role: string;
  as_of: string;
  window: string;
  snapshot_only: boolean;
  forward_looking: boolean;
  note: string;
}

/** Q05：估值三态证据视图（后端 derive_valuation_evidence_state 的载荷，研报与追问共用）。 */
export interface ValuationEvidenceView {
  /** metric_missing | normalization_unverified | fair_value_assessed。 */
  state: string;
  label?: string;
  note?: string;
  display?: string;
  /** 替换笼统「无数据」的单行结论词。 */
  verdict_display?: string;
  metrics?: Record<string, string>;
  metric_count?: number;
  valuation_status?: string;
  valuation_status_label?: string;
  normalized_earnings_ready?: boolean;
  fair_value_ready?: boolean;
  margin_of_safety?: number | null;
}

/** Q08：情景区间界别——只凭历史高/低点身份的价位是「参考位」，不是目标价。 */
export interface ScenarioBoundView {
  side: "low" | "high" | string;
  value: number | null;
  /** technical=技术位推导 | reference=仅历史参考位 | declared=模型自述未核。 */
  kind: string;
  note?: string;
}

/** Q08：情景概率与预期区间的口径视图（页面与导出共用，措辞不得写成上涨概率）。 */
export interface ScenarioProbabilityView {
  calibrated?: boolean;
  probability_label?: string;
  note?: string;
  scenarios?: {
    name: string;
    probability_level?: string;
    probability_display?: string;
    probability_basis?: string;
    bounds?: ScenarioBoundView[];
  }[];
  reference_bounds?: { scenario: string; level: number | null; note: string }[];
}

/** Q09：来源目录条目（编号/名称/可用链接/数据日期/获取时间/口径限制；缺链接如实标注，不伪造）。 */
export interface SourceCatalogEntry {
  source_id: string;
  name: string;
  url: string;
  url_note?: string;
  data_date?: string;
  retrieved_at?: string;
  quality?: string;
  scope_note?: string;
  cited?: boolean;
  used_by?: string[];
}

/** Q11：补证取数结果（成功/部分成功/全部失败都如实展示，不把失败包装成研究完成）。 */
export interface FollowupRetrievalStatus {
  requested: number;
  succeeded: number;
  failed: number;
  /** none | all_succeeded | partial | all_failed。 */
  state: string;
  note?: string;
  failed_items?: { kind: string; label: string; error: string }[];
}

/** Q15：本次追问新增的证据快照（回看「当时依据什么改变判断」）。 */
export interface FollowupEvidenceSnapshot {
  kind: string;
  label: string;
  source_name?: string;
  url?: string;
  url_note?: string;
  event_date?: string;
  retrieved_at?: string;
  trust_label?: string;
  status: string;
  error?: string;
  supported_claims?: string[];
  analysis_version?: string;
}

/** Q13：催化事件证据链的单环（业务量→价格与成本→利润→市场预期→价格反应）。 */
export interface FollowupCatalystStep {
  key: string;
  label: string;
  /** evidenced=该环有具体口径 | missing=缺证据（缺哪环一目了然）。 */
  status: string;
  evidence?: string;
  note?: string;
}

/** Q13：整条链条由服务端确定性拼装，模型只负责解释，不负责数环节。 */
export interface FollowupCatalystChain {
  steps?: FollowupCatalystStep[];
  complete?: boolean;
  broken_at?: string;
  note?: string;
  guardrail?: string;
}

/** v39 Q16：盈利增长拆解单因子（贡献比例算不出即为 null，服务端不代填）。 */
export interface EarningsGrowthRow {
  factor: string;
  factor_label?: string;
  factor_raw?: string;
  effect?: string;
  contribution_pct?: number | null;
  basis?: string;
  formula?: string;
  formula_inputs?: unknown[];
  sources?: string[];
  source_note?: string;
  note?: string;
}

/** v39 Q17：现金流质量（覆盖倍数 + 折旧摊销/营运资本/资本开支/自由现金流）。 */
export interface CashFlowQualityBlock {
  version?: string;
  operating_cash_flow?: number | null;
  net_income?: number | null;
  coverage_ratio?: number | null;
  depreciation_amortization?: number | null;
  working_capital?: number | null;
  working_capital_change?: number | null;
  capex?: number | null;
  free_cash_flow?: number | null;
  extras_present?: string[];
  /** 解释项（折旧摊销 / 营运资本变动）：只有它们能支撑「盈利质量较好」。 */
  explanatory_present?: string[];
  /** 派生项（资本开支 / 自由现金流）：由经营现金流算出，不增加解释力。 */
  derived_present?: string[];
  /** 「不足以判断盈利质量」= 只有覆盖倍数；缺字段时结论强度自动降低。 */
  verdict?: string;
  note?: string;
  downgrade_note?: string;
  strength?: string;
}

/** v39 Q18：可比样本口径与估值分化解释。 */
export interface ComparableReview {
  sample_count?: number;
  /** 模型自述比了几只（v39 真机：与快照不一致时以 sample_count 为准，这里只留痕）。 */
  model_claimed_sample_count?: number;
  /** 同业池只数与每个口径的实际可比只数（如 pe_ttm: 7）。 */
  server_peer_count?: number | null;
  /** 剔除只数按同业池算（池 − 可比），不是模型名单里列了几只。 */
  excluded_count?: number;
  comparable_basis?: Record<string, number>;
  count_conflict?: string;
  excluded?: { name: string; reason: string }[];
  exclusion_rule?: string;
  comparability?: string;
  comparability_label?: string;
  note?: string;
  sensitivity_allowed?: boolean;
}

export interface ValuationDivergence {
  version?: string;
  /** 数值来自服务端估值快照（`metrics_source` 说明来源）；快照缺席时才退回模型转述的字符串。 */
  pe_ttm?: string | number | null;
  pb_mrq?: string | number | null;
  pe_percentile?: string;
  pb_percentile?: string;
  percentile_window_bars?: number | null;
  percentile_window_from?: string;
  peer_median?: Record<string, number | null>;
  metrics_source?: string;
  roe?: number | null;
  equity_basis?: string;
  cycle_note?: string;
  explained_axes?: string[];
  explanation_state?: string;
  low_pe_is_not_margin_of_safety?: boolean;
  guardrail?: string;
  valuation_evidence_state?: string;
  sensitivity_allowed?: boolean;
  sensitivity_note?: string;
  comparable_review?: ComparableReview;
}

/** v39 Q19：深研完成度单项（盈利拆解 / 现金流与资本开支 / 估值解释 / 情景与反证）。 */
export interface ResearchCompletenessItem {
  key: string;
  label: string;
  /** done | partial | missing（服务端确定性推导，不采信模型自评）。 */
  state: string;
  state_label?: string;
  covered?: string[];
  gaps?: string[];
  evidence?: string[];
  note?: string;
}

/** v39 Q19：四项完成度 + 字数辅助提示（字数不参与判定）。 */
export interface ResearchCompleteness {
  version?: string;
  items?: ResearchCompletenessItem[];
  done_count?: number;
  partial_count?: number;
  missing_count?: number;
  score?: number;
  complete?: boolean;
  headline?: string;
  word_count?: number;
  word_count_mode?: { mode?: string; label?: string; min?: number; max?: number };
  word_count_note?: string;
  word_count_advisory?: boolean;
  warnings?: string[];
  note?: string;
}

/** v39 Q20：摘要过度断言的就地标注留痕（服务端只加注，不改写事实句）。 */
export interface SummaryOverreachTrace {
  sentence?: string;
  denial?: string;
  reason?: string;
  marker_added?: boolean;
  also_aligned_by?: string;
  [key: string]: unknown;
}

export interface SummaryOverreach {
  version?: string;
  /** false = 本次没做反方检查，检查未运行要如实说明，不假装已修订。 */
  reviewed?: boolean;
  changed?: boolean;
  overreach?: string[];
  counter_denials?: string[];
  summary_original?: string;
  revised_summary?: string;
  revision_note?: string;
  revision_trace?: SummaryOverreachTrace[];
  valuation_language_repairs?: unknown[];
  original_summary_chars?: number;
  revised_summary_chars?: number;
  original_over_target?: boolean;
  related_downgrade_notes?: string[];
  warnings?: string[];
  note?: string;
}

/** 单次追问的分析附录（§3.3 结果契约；append-only，不允许改写原报告）。 */
export interface AnalysisTurn {
  ok?: true;
  analysis_turn_id: string;
  parent_report_id: string;
  /** 追问时锚定的原报告修订版本（修复会升 revision）。 */
  base_report_version: number;
  symbol?: string;
  /** 附录序号（从 1 递增，按创建时间正序）。 */
  turn_index: number;
  question: string;
  supplement_evidence: FollowUpSupplement[];
  fact_normalizations?: FollowUpFact[];
  affected_claims: { claim_id: string; effect: string; effect_raw?: string; reason: string }[];
  /** 模型自造、被服务端闸门剔除的 claim 编号（不进影响清单，留痕供审计）。 */
  dropped_claim_ids?: string[];
  /** unchanged|strengthened|weakened|changed|undetermined。 */
  conclusion_change: string;
  conclusion_change_raw?: string;
  scenario_changes?: { name: string; change: string }[];
  new_watchpoints?: ReportWatchpoint[];
  /** JV06：新增验证点的分诊回执（与报告验证点同一把尺子、同一个 purpose）。 */
  jev_followup?: JevFollowupTriage | null;
  answer: string;
  limitations?: string[];
  model: string;
  prompt_version?: string;
  created_at: string;
  /* —— 1.2 契约（Q02/Q03/Q04）：先回答问题，再解释条件与缺口 —— */
  /** ≤100 字的第一手回答（永远排在判断影响表之前）。 */
  direct_answer?: string;
  /** answered|partially_answered|not_answerable；旧记录为空串/缺失。 */
  answerability?: string;
  answerability_label?: string;
  key_conditions?: string[];
  evidence_gaps?: string[];
  /** unrevised|revised|undetermined（原判断是否修订，与「能否回答」分开表达）。 */
  revision_status?: string;
  revision_status_label?: string;
  /** report_only|user_supplement|refreshed_data（是否用了新增数据）。 */
  data_basis?: string;
  data_basis_label?: string;
  /** 紧跟问题的一行状态说明（如「沿用原报告判断，未更新行情（未复核最新数据）」）。 */
  state_line?: string;
  /** 非空 = 模型自相矛盾（如声称结论改变却无任何证据），按琥珀告警如实展示。 */
  status_conflict?: string;
  /** 只展开支持/削弱的判断（Q04）。 */
  expanded_claims?: FollowupClaim[];
  /** 无法判断/无关的判断：合并成一行共同说明，不再逐条刷屏。 */
  unresolved_claims?: FollowupClaim[];
  /** unresolved_claims 共用的一句话限制。 */
  shared_limitation?: string;
  /* —— Q05/Q06/Q08：口径视图与价位时点 —— */
  price_refs?: FollowupPriceRef[];
  /** 方向研判无估值结构 → 后端给 null（「不适用」不是「指标缺失」，前端整块不渲染）。 */
  valuation_view?: ValuationEvidenceView | null;
  probability_view?: ScenarioProbabilityView | null;
  /** 本条附录所用数据的截至日（YYYY-MM-DD）；空串=未标注。键缺失=旧记录（未评估）。 */
  data_as_of?: string;
  /* —— Q10/Q11/Q15：入口模式与补证链路 —— */
  /** interpret=解读本报告 | supplement_research=补充研究。 */
  mode?: string;
  mode_label?: string;
  retrieval_status?: FollowupRetrievalStatus;
  evidence_snapshot?: FollowupEvidenceSnapshot[];
  /** 本次分析可用的来源目录（含引用与未引用；缺链接如实标注「未提供链接」）。 */
  source_catalog?: SourceCatalogEntry[];
  /* —— Q13：催化事件证据链与越界断言标注 —— */
  catalyst_chain?: FollowupCatalystChain;
  /** 服务端点破的越界表述（如「超预期」而无预期数据、「必涨」而链条已断）；不改写模型原话。 */
  answer_flags?: string[];
}

/** v23 协同流水线：单角色阶段状态（严格串行，一次只推进一个）。 */
export interface CollabStage {
  stage: string;
  label: string;
  status: "pending" | "running" | "done" | "failed";
  result: Record<string, unknown> | null;
  error: string | null;
  latency_ms: number | null;
  /* v24 质量闸门痕迹（仅初稿/修订阶段写入，由 Core collab 引擎在同一阶段对象上给出） */
  /** 被剔除的虚假引用（不属于本次真实取到的来源）。 */
  citation_dropped?: string[];
  /** 形式质量告警（如实展示，不阻断交付）。 */
  quality_warnings?: string[];
  /* v30：告警四级分组 + 摘要字数（随阶段留痕；collab 无摘要重写）。 */
  categorized_warnings?: CategorizedWarning[];
  summary_chars?: number;
  /** 正文字数（去空白）。 */
  report_chars?: number;
  /** 正文中识别到的小节标题。 */
  report_sections?: string[];
  /* v27/v28：claim 覆盖 + 证据分级 + 一句话结论 + 摘要降级提示（随阶段留痕，落库与前端共用） */
  evidence_quality?: EvidenceQuality;
  claim_findings?: ClaimFindings;
  /** JV04：语义支撑层回执（协同链路与个股端点同一字段口径）。 */
  jev?: ClaimJevLayer;
  conclusion?: ReportConclusion;
  summary_downgrade_notes?: string[];
  /* v31 质量恢复：三态状态 + 阻断项 + 小节实质状态（初稿/修订阶段写入，随 finalReport 透传） */
  quality_status?: QualityStatus;
  quality_status_label?: string;
  quality_blockers?: QualityBlocker[];
  missing_sections?: string[];
  section_states?: SectionState[];
}

/** v23 协同流水线运行视图（后端 collab-engine public_view）。 */
export interface CollabRunView {
  run_id: string;
  symbol: string;
  question: string;
  model: string;
  /** v23 角色→模型分配（用户拍板：不同模型执行不同阶段，串行）。 */
  stage_models: Record<string, string>;
  /** v31：模式随运行定稿（请求显式传递，创建时校验，闸门/落库/前端共用同一值）。 */
  report_mode?: string;
  created_at: string;
  done: boolean;
  next_index: number;
  stages: CollabStage[];
  source_keys: string[];
  source_errors: Record<string, string>;
  /** v29：证据元数据（来源取数时刻 + 图表数据包），由 public_view 下发；旧运行缺失时图表区不渲染。 */
  evidence_meta?: {
    source_meta?: Record<string, SourceMetaEntry>;
    price_chart?: PriceChartPack;
    quarterly?: Record<string, QuarterlyPoint[]>;
    valuation_chart?: ValuationChartPack;
    source_asof?: Record<string, string>;
  };
}

/** v23 阶段 0 跨报告综合：共识/分歧/待核验（分歧如实陈列，不投票）。 */
export interface CompareSynthesis {
  ok: true;
  summary: string;
  consensus: string[];
  divergences: string[];
  to_verify: string[];
  model: string;
  latency_ms?: number;
  generated_at: string;
}

/** v22 批量队列：单份结果（report=null 时 error 必有值；pending=true 表示排队/生成中）。 */
export interface CompareEntry {
  symbol: string;
  name: string;
  profileId: string | null;
  report: StockReport | null;
  error: string | null;
  pending?: boolean;
}

/** 研究工作台的两个 tab（A04 自页面迁入，页面与子模块共用）。 */
export type WorkbenchTab = "direction" | "stock";
