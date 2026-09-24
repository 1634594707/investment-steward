/**
 * A04（前端设计与架构优化任务路线图 2026-09-19）：个股研报面板自 `routes/ResearchWorkbenchPage.tsx` 原样迁出（约 1.0k 行阅读/表单编排），
 * 页面入口只留数据装配与分区切换；类型一律取自 `workbench/researchTypes`，改本子模块不再从页面入口导入（方案 E03）。
 */

import { memo, useEffect, useRef, useState, type ReactNode } from "react";
import { Check } from "lucide-react";
import { ModelProfile } from "@investment-steward/domain-contracts";
import { type FollowUpSupplementInput } from "../../hooks/useResearch";
import { ReportHandoff } from "../../state/handoff";
import { ExportBar, BatchExportBar, buildStockReportMarkdown, buildDirectionReportMarkdown, buildCompareSynthesisMarkdown, buildHtmlDocument, markdownToHtml, reportFileBase } from "./export";
import { keyFactHighlights, ReportResultTabs, ReportFirstScreen } from "./panels";
import { retryCompareEntry } from "./compareJob";
import { FollowUpPanel, EMPTY_FOLLOWUP_DRAFT, type FollowUpDraft } from "./followup";
import { DirectionResultTabs, THEME_INPUT_HINT, THEME_TOPIC_PLACEHOLDER } from "./direction";
import { ReportSourceBar } from "./sourceBar";
import { WorkbenchTab, StockReport, DirectionReport, AnalysisTurn, CollabRunView, CompareSynthesis, CompareEntry } from "./researchTypes";
import { AppView } from "../../shell/nav";
import { CollabResultBlock, Field, type CollabCheckResult, type CollabReportResult, type CollabRiskResult } from "./collab";

const DIRECTION_STYLE_WORDS = [
  "低估", "高估", "破净", "低市盈率", "高市盈率",
  "大盘", "小盘", "绩优", "成长", "价值", "高股息", "红利",
];

function directionThemeHint(topic: string): string {
  const text = topic.replace(/\s/g, "").replace(/(板块|行业|概念|主题|产业|指数)$/, "");
  if (!text) return THEME_INPUT_HINT.generic!;
  const hasStyle = DIRECTION_STYLE_WORDS.some((word) => text.includes(word));
  const remainder = DIRECTION_STYLE_WORDS.reduce((acc, word) => acc.replace(word, ""), text);
  const hasIndustry = remainder.length > 0 && remainder !== text;
  if (hasStyle && hasIndustry) return THEME_INPUT_HINT.composite!;
  if (hasStyle) return THEME_INPUT_HINT.style!;
  return THEME_INPUT_HINT.industry!;
}

/** 质量闸门痕迹：可引用来源 / 已剔除的虚假引用 / 形式质量告警 / 正文口径。旧记录缺字段时整块不渲染。 */

/** 带标签的输入字段（研究工作台表单专用）。 */

/** v23 角色级模型分配：协同流水线四角色（严格串行依次执行），每个角色可指定不同模型方案。 */
/** —— 可筛选下拉选择器（§13.22 用户反馈：chips 铺排不好选,改成带筛选的下拉,参照原生 select 视觉） ——
 * multi=false 时单选（点选项即关闭）；multi=true 时多选,面板内提供「全选/移除筛选结果」。 */
function SelectPicker({ value, options, onChange, multi = false, placeholder = "请选择", minWidth = 260 }: {
  value: string[];
  options: { value: string; label: string }[];
  onChange: (next: string[]) => void;
  multi?: boolean;
  placeholder?: string;
  minWidth?: number;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    function onDocMouseDown(event: MouseEvent): void {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);
  const keyword = query.trim().toLowerCase();
  const filtered = keyword ? options.filter((option) => option.label.toLowerCase().includes(keyword)) : options;
  const summary = value.length === 0
    ? placeholder
    : multi
      ? `已选 ${value.length} 项：${value.map((v) => options.find((option) => option.value === v)?.label ?? v).join("、")}`
      : options.find((option) => option.value === value[0])?.label ?? placeholder;
  return (
    <div className="picker" ref={rootRef} style={{ minWidth }}>
      <button
        type="button"
        className={`picker-toggle ${value.length > 0 ? "active" : ""}`}
        aria-expanded={open}
        aria-haspopup="listbox"
        title={summary}
        onClick={() => { setOpen((current) => !current); setQuery(""); }}
      >
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{summary}</span>
        <span className="picker-caret">▾</span>
      </button>
      {open && (
        <div className="picker-panel" role="listbox">
          <input
            className="picker-filter"
            value={query}
            placeholder="筛选（名称 / 代码）…"
            autoFocus
            onChange={(event) => setQuery(event.target.value)}
          />
          {multi && (
            <div className="picker-actions">
              <button
                type="button"
                disabled={filtered.length === 0}
                title="把当前筛选结果全部勾选"
                onClick={() => onChange(Array.from(new Set([...value, ...filtered.map((option) => option.value)])))}
              >全选（筛选结果）</button>
              <button
                type="button"
                disabled={value.length === 0}
                title="从已选中移除当前筛选结果"
                onClick={() => onChange(value.filter((v) => !filtered.some((option) => option.value === v)))}
              >移除（筛选结果）</button>
              <button type="button" disabled={value.length === 0} title="清空全部已选" onClick={() => onChange([])}>清空</button>
            </div>
          )}
          {filtered.length === 0 && <div className="picker-empty">无匹配项</div>}
          {filtered.map((option) => {
            const active = value.includes(option.value);
            return (
              <div
                key={option.value}
                className={`picker-option ${active ? "active" : ""}`}
                role="option"
                aria-selected={active}
                onClick={() => {
                  if (multi) onChange(active ? value.filter((v) => v !== option.value) : [...value, option.value]);
                  else { onChange([option.value]); setOpen(false); }
                }}
              >
                <span className="picker-check">{active ? <Check size={12} aria-hidden="true" /> : null}</span>
                {option.label}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

const COLLAB_STAGE_DEFS: { key: "check" | "draft" | "risk" | "revise"; label: string }[] = [
  { key: "check", label: "① 数据核对" },
  { key: "draft", label: "② 首席初稿" },
  { key: "risk", label: "③ 风险审查" },
  { key: "revise", label: "④ 首席修订" },
];

/** AI 研究面板：方向研判与个股研报两个 tab。tab 状态由页面级持有（与流程卡联动）。 */
export function StockReportPanel({
  candidates,
  aiReady,
  tab,
  onTabChange,
  busy,
  compareResults,
  activeCompare,
  onActiveCompareChange,
  compareBusy,
  selectedProfileIds,
  onSetProfiles,
  onGenerate,
  onGenerateCompare,
  queue,
  onQueueSet,
  comparePending,
  reportHandoff,
  onNavigate,
  directionBusy,
  directionReport,
  directionError,
  reportError,
  onGenerateDirection,
  onSendToTactics,
  modelProfiles,
  onSynthesizeCompare,
  onCreateCollabRun,
  onNextCollabStage,
  onCollabFinished,
  onRefreshReports,
  onCreateFollowUp,
  onLoadFollowUps,
}: {
  candidates: { instrument: string; label: string }[];
  aiReady: boolean;
  tab: WorkbenchTab;
  onTabChange: (tab: WorkbenchTab) => void;
  busy: boolean;
  compareResults: CompareEntry[] | null;
  activeCompare: number;
  onActiveCompareChange: (index: number) => void;
  compareBusy: boolean;
  selectedProfileIds: string[];
  onSetProfiles: (ids: string[]) => void;
  /** B01（2026-09-15 路线图）：单股分支显式传递所选模型（profileId，null=跟随全局使用中）与研报档位 mode，均透传后端。 */
  onGenerate: (symbol: string, question: string, barsLimit: number, profileId?: string | null, mode?: string) => void;
  onGenerateCompare: (symbols: string[], question: string, barsLimit: number, profileIds: string[], execMode?: "parallel" | "serial", mode?: string) => void;
  /** v22 批量队列：已加入生成队列的标的代码（快选 chips 多选）。 */
  queue: string[];
  /** 批量队列整体设置（全选/清空快选）。 */
  onQueueSet: (codes: string[]) => void;
  /** v22 批量队列：尚未完成的份数（用于进度显示）。 */
  comparePending: number;
  /** v33 D06：雷达/方向 → 单股研究的结构化交接（唯一 requestId，消费后回执）。 */
  reportHandoff: ReportHandoff | null;
  /** D02（前端设计与架构优化任务路线图 2026-09-19）：视图导航——来源条的「返回来源」由此回到发起交接的雷达页。 */
  onNavigate: (view: AppView) => void;
  directionBusy: boolean;
  directionReport: DirectionReport | null;
  directionError: string | null;
  /** B02/B04（2026-09-15 方向研判路线图）：单股生成失败的「原因 + 恢复动作」文案（外层 generateReport 写入，null=无错误）。 */
  reportError: string | null;
  /** v33 C02：方向研判本次模型可选（profileId 为空 = 跟随全局使用中）。 */
  onGenerateDirection: (topic: string, question: string, profileId?: string | null) => void;
  /** v33 D02/D03：候选池 → 战法雷达的结构化交接（发送范围由调用方决定，AppShell 生成 requestId）。 */
  onSendToTactics: (pool: string[], context?: { sourceLabel?: string; sourceReportId?: string; topic?: string; question?: string; excluded?: { symbol: string; reason: string }[] }) => void;
  modelProfiles: ModelProfile[];
  /** v23 阶段 0：多模型报告跨报告综合（共识/分歧/待核验，分歧不投票）。 */
  onSynthesizeCompare: (
    reports: { symbol: string; model: string; confidence: string; executive_summary: string; report: string }[],
    question: string,
  ) => Promise<CompareSynthesis | null>;
  /** v23 协同流水线：创建运行（定稿证据包，角色由 onNextCollabStage 依次推进；stageProfiles=角色→方案 id，角色级模型分配）。 */
  onCreateCollabRun: (symbol: string, question: string, barsLimit: number, stageProfiles?: Record<string, string> | null) => Promise<CollabRunView | null>;
  /** v23 协同流水线：执行下一个角色（严格串行：一次调一个；失败阶段再次调用即重试）。 */
  onNextCollabStage: (runId: string) => Promise<{ done: boolean; run: CollabRunView; executedStage: string; executedStatus: string; collabReportId?: string } | null>;
  /** v23：协同完成后刷新历史产出列表（修订稿已落库）。 */
  onRefreshReports: () => void;
  /** v23 多模型协同：某模型的修订稿完成 → 交给页面追加进对比结果区（可对比/综合/批量导出）。 */
  onCollabFinished: (report: StockReport) => void;
  /** v32 研报追问：提交一次追问（原报告不可变，结果保存为追加的分析附录）。
   * v39 Q10：`mode` 带入本次入口（interpret 解读本报告 / supplement_research 补充研究）。 */
  onCreateFollowUp: (reportId: string, question: string, supplements: FollowUpSupplementInput[], mode: string) => Promise<AnalysisTurn | null>;
  /** v32 研报追问：载入某报告的全部追问附录（append-only，时间正序）。 */
  onLoadFollowUps: (reportId: string) => Promise<AnalysisTurn[] | null>;
}) {
  const [symbol, setSymbol] = useState("");
  // 快选候选默认铺 12 只（此前 slice(0,12) 硬截断,16 只自选显示不全）；更多时折叠可展开。
  const [showAllCandidates, setShowAllCandidates] = useState(false);
  const [question, setQuestion] = useState("");
  const [topic, setTopic] = useState("");
  const [directionQuestion, setDirectionQuestion] = useState("");
  // v22 交互升级：K 线回看区间（日）+ 标的池筛选（板块 / 参考价粗筛）
  const [klineRange, setKlineRange] = useState<60 | 120 | 250>(120);
  // v29 研报字数预算档（quick 1500-2500 / standard 2000-3500 / deep 3500-6000）；只影响软告警，不改变硬闸门。
  const [reportMode, setReportMode] = useState<"quick" | "standard" | "deep">("standard");
  const [poolSector, setPoolSector] = useState<string>("all");
  const [poolMin, setPoolMin] = useState("");
  const [poolMax, setPoolMax] = useState("");
  // v22.2 用户需求：多模型执行方式可选——并行（同股内各模型同时出，快）或串行（依次执行，省额度/避开限速）。
  const [execMode, setExecMode] = useState<"parallel" | "serial">("parallel");
  // v23 协同流水线（单股·角色级模型分配）：四角色严格串行，每个角色可用不同模型（依次执行）。
  const [collabMode, setCollabMode] = useState(false);
  const [collabRuns, setCollabRuns] = useState<CollabRunView[]>([]);
  const [collabBusy, setCollabBusy] = useState(false);
  const [collabError, setCollabError] = useState<string | null>(null);
  const [collabStageModels, setCollabStageModels] = useState<Record<string, string>>({ check: "", draft: "", risk: "", revise: "" });
  // 2026-09-12 用户追加：协同流水线支持**队列串行批量**——一只完整走完四角色并落库后，再按队列顺序生成下一只。
  // collabTargets 保存本批待生成标的（保持不动，供「继续执行」从断点恢复）；collabIndex 是当前标的在队列中的下标。
  const [collabTargets, setCollabTargets] = useState<string[]>([]);
  const [collabIndex, setCollabIndex] = useState(0);
  const [collabFinishedCount, setCollabFinishedCount] = useState(0);
  // v23 阶段 0：跨报告综合。
  const [synthesis, setSynthesis] = useState<CompareSynthesis | null>(null);
  const [synthesisBusy, setSynthesisBusy] = useState(false);
  const [synthesisError, setSynthesisError] = useState<string | null>(null);
  // v33 C01：方向研判「本次模型」选择（"" = 跟随全局使用中；方向选择不覆盖个股模型选择）。
  const [directionProfileId, setDirectionProfileId] = useState("");
  // v33 A02：新建研究表单折叠状态（有结果时默认收起，用户可展开调整配置）。
  const [newStudyExpanded, setNewStudyExpanded] = useState(false);
  // v33 D07：被单股研究暂存的批量队列（显式恢复，不自动合并）。
  const [stashedQueue, setStashedQueue] = useState<string[]>([]);
  // v32 研报追问（2026-09-13 方案 §三）：当前查看报告的追问附录（append-only）与提交状态。
  const [followupTurns, setFollowupTurns] = useState<AnalysisTurn[]>([]);
  const [followupLoading, setFollowupLoading] = useState(false);
  const [followupBusy, setFollowupBusy] = useState(false);
  const [followupError, setFollowupError] = useState<string | null>(null);
  // v33 A14：追问草稿按报告 id 保存（切页签/切报告/提交失败都不丢），报告身份变化时读取对应草稿。
  const [followupDrafts, setFollowupDrafts] = useState<Record<string, FollowUpDraft>>({});
  // v33 A14：迟到请求隔离——报告 A 的追问响应不得覆盖报告 B 的附录列表（ref 随 activeReportId 同步）。
  const activeReportIdRef = useRef<string | null>(null);
  // 当前展示的报告（单份/对比/协同/历史回看都走 compareResults），其 id 变化时重新载入追问附录。
  const activeReportId = compareResults?.[Math.min(activeCompare, (compareResults?.length ?? 1) - 1)]?.report?.report_id ?? null;
  const followupDraft = (activeReportId && followupDrafts[activeReportId]) || EMPTY_FOLLOWUP_DRAFT;
  const setFollowupDraft = (next: FollowUpDraft): void => {
    if (!activeReportId) return;
    setFollowupDrafts((current) => ({ ...current, [activeReportId]: next }));
  };
  // v39 方向追问接线：方向研判复用同一后端端点（Core 按报告 kind 自动路由到方向分支 J# 编号闸门），
  // 状态与个股研报区完全隔离——两区页签不同，附录列表/草稿互不覆盖。
  const [directionFollowupTurns, setDirectionFollowupTurns] = useState<AnalysisTurn[]>([]);
  const [directionFollowupLoading, setDirectionFollowupLoading] = useState(false);
  const [directionFollowupBusy, setDirectionFollowupBusy] = useState(false);
  const [directionFollowupError, setDirectionFollowupError] = useState<string | null>(null);
  const [directionFollowupDrafts, setDirectionFollowupDrafts] = useState<Record<string, FollowUpDraft>>({});
  const directionFollowupReportId = directionReport?.report_id ?? null;
  const directionFollowupDraft = (directionFollowupReportId && directionFollowupDrafts[directionFollowupReportId]) || EMPTY_FOLLOWUP_DRAFT;
  const setDirectionFollowupDraft = (next: FollowUpDraft): void => {
    if (!directionFollowupReportId) return;
    setDirectionFollowupDrafts((current) => ({ ...current, [directionFollowupReportId]: next }));
  };
  // v33 A02：个股区是否有结果（对比区或协同运行）——决定新建表单默认折叠。
  const hasStockResults = compareResults != null || collabRuns.length > 0;
  // v33 C01：全局「使用中」方案（方向研判「跟随全局」时显示其名称）。
  const activeProfile = modelProfiles.find((profile) => profile.status === "使用中") ?? null;
  useEffect(() => {
    if (!activeReportId) {
      setFollowupTurns([]);
      setFollowupError(null);
      return;
    }
    let cancelled = false;
    setFollowupLoading(true);
    onLoadFollowUps(activeReportId)
      .then((items) => { if (!cancelled) setFollowupTurns(items ?? []); })
      .catch(() => { if (!cancelled) setFollowupError("追问附录载入失败，请稍后重试。"); })
      .finally(() => { if (!cancelled) setFollowupLoading(false); });
    return () => { cancelled = true; };
    // onLoadFollowUps 每次渲染都是新引用（hooks 未 memo），只按 reportId 触发即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeReportId]);
  useEffect(() => { activeReportIdRef.current = activeReportId; }, [activeReportId]);
  // v39 方向追问附录：随 directionReport.report_id 变化载入（含历史回看回填），与个股 activeReportId 链路独立。
  const directionFollowupReportIdRef = useRef<string | null>(null);
  useEffect(() => { directionFollowupReportIdRef.current = directionFollowupReportId; }, [directionFollowupReportId]);
  useEffect(() => {
    if (!directionFollowupReportId) {
      setDirectionFollowupTurns([]);
      setDirectionFollowupError(null);
      return;
    }
    let cancelled = false;
    setDirectionFollowupLoading(true);
    onLoadFollowUps(directionFollowupReportId)
      .then((items) => { if (!cancelled) setDirectionFollowupTurns(items ?? []); })
      .catch(() => { if (!cancelled) setDirectionFollowupError("追问附录载入失败，请稍后重试。"); })
      .finally(() => { if (!cancelled) setDirectionFollowupLoading(false); });
    return () => { cancelled = true; };
    // onLoadFollowUps 每次渲染都是新引用（hooks 未 memo），只按报告 id 触发即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [directionFollowupReportId]);

  /** v32 提交追问：成功后把新附录追加到本地列表（服务端已落库，append-only）。
   * v33 A14：提交时锚定报告 id——响应返回前若用户已切换报告，迟到结果按丢弃处理（不覆盖新报告视图）。
   * v39 Q10：`mode` 随请求送出（解读本报告 / 补充研究），面板上选哪个入口就按哪个口径记录数据来源。 */
  async function submitFollowUp(question: string, supplements: FollowUpSupplementInput[], mode: string): Promise<boolean> {
    const submittedFor = activeReportId;
    if (!submittedFor) return false;
    setFollowupBusy(true);
    setFollowupError(null);
    try {
      const turn = await onCreateFollowUp(submittedFor, question, supplements, mode);
      if (activeReportIdRef.current !== submittedFor) return false; // 已切换报告：迟到响应不落地
      if (!turn) {
        setFollowupError("追问未完成：模型调用或解析未通过（原报告保持不变）。请调整问题或补充材料后重试。");
        return false;
      }
      setFollowupTurns((current) => [...current, turn]);
      return true;
    } catch {
      if (activeReportIdRef.current === submittedFor) setFollowupError("追问请求失败，请重试。");
      return false;
    } finally {
      if (activeReportIdRef.current === submittedFor) setFollowupBusy(false);
    }
  }

  /** v39 方向追问提交：与个股同一端点（Core 按报告 kind 路由到方向分支，编号取 core_judgments J#）。
   * 迟到隔离同款：响应返回前若已切换方向报告，结果不落地（不覆盖新报告视图）。 */
  async function submitDirectionFollowUp(question: string, supplements: FollowUpSupplementInput[], mode: string): Promise<boolean> {
    const submittedFor = directionFollowupReportId;
    if (!submittedFor) return false;
    setDirectionFollowupBusy(true);
    setDirectionFollowupError(null);
    try {
      const turn = await onCreateFollowUp(submittedFor, question, supplements, mode);
      if (directionFollowupReportIdRef.current !== submittedFor) return false; // 已切换报告：迟到响应不落地
      if (!turn) {
        setDirectionFollowupError("追问未完成：模型调用或解析未通过（原报告保持不变）。请调整问题或补充材料后重试。");
        return false;
      }
      setDirectionFollowupTurns((current) => [...current, turn]);
      return true;
    } catch {
      if (directionFollowupReportIdRef.current === submittedFor) setDirectionFollowupError("追问请求失败，请重试。");
      return false;
    } finally {
      if (directionFollowupReportIdRef.current === submittedFor) setDirectionFollowupBusy(false);
    }
  }

  /** 由运行态装配一份协同修订稿——创建链路与「继续执行」链路共用同一份口径，避免两处漂移。
   * 模型名带上各角色实际使用的方案，便于在对比区分辨「是哪几个模型协同出的」。 */
  function collabReportFrom(current: CollabRunView): StockReport | null {
    const revise = current.stages[3]?.result as CollabReportResult | null | undefined;
    const reviseStage = current.stages[3];
    if (!revise || (!revise.report && !revise.title)) return null;
    const stageModelNames = Object.values(current.stage_models ?? {}).filter(Boolean);
    return {
      ok: true,
      symbol: current.symbol,
      title: revise.title ?? `${current.symbol} 协同研究报告`,
      executive_summary: revise.executive_summary ?? "",
      report: revise.report ?? "",
      citations: revise.citations ?? [],
      limitations: revise.limitations ?? [],
      confidence: revise.confidence ?? "low",
      model: `${stageModelNames.join("/") || current.model}·协同流水线`,
      source_keys: current.source_keys,
      source_errors: current.source_errors,
      generated_at: current.created_at,
      citation_dropped: reviseStage?.citation_dropped ?? [],
      quality_warnings: reviseStage?.quality_warnings ?? [],
      /* v30：告警四级分组 + 摘要字数（collab 无摘要重写，超限仅告警交人工复核）。 */
      categorized_warnings: reviseStage?.categorized_warnings ?? [],
      summary_chars: reviseStage?.summary_chars,
      report_sections: reviseStage?.report_sections,
      report_chars: reviseStage?.report_chars,
      /* v31 质量恢复：三态状态 + 阻断项 + 小节实质状态随修订阶段透传（与单股研报同一口径）。 */
      quality_status: reviseStage?.quality_status,
      quality_status_label: reviseStage?.quality_status_label,
      quality_blockers: reviseStage?.quality_blockers,
      missing_sections: reviseStage?.missing_sections,
      section_states: reviseStage?.section_states,
      generation_trace: {
        model: current.model,
        stage_models: current.stage_models,
        stages_completed: current.stages.filter((stage) => stage.status === "done").map((stage) => stage.stage),
        report_mode: current.report_mode ?? "standard",
        source_keys: current.source_keys,
      },
      scenarios: revise.scenarios ?? [],
      levels: revise.levels ?? {},
      watchpoints: revise.watchpoints ?? [],
      valuation: revise.valuation,
      /* v27/v28：与个股研报端点同一口径（证据分级 / claim 支撑链 / 结论卡数据源）。 */
      evidence_quality: reviseStage?.evidence_quality,
      claims: revise.claims ?? [],
      claim_findings: reviseStage?.claim_findings,
      conclusion: reviseStage?.conclusion ?? revise.conclusion,
      summary_downgrade_notes: reviseStage?.summary_downgrade_notes ?? [],
      /* v29：图表数据包与来源元数据随运行快照透传（与单股研报同一组件渲染，证据同源）。 */
      /* v31：模式来自运行定稿值（不再写死字面量），与闸门/落库共用同一 report_mode。 */
      report_mode: current.report_mode ?? "standard",
      source_meta: current.evidence_meta?.source_meta,
      source_asof: current.evidence_meta?.source_asof,
      price_chart: current.evidence_meta?.price_chart,
      quarterly: current.evidence_meta?.quarterly,
      valuation_chart: current.evidence_meta?.valuation_chart,
      revision_notes: revise.revision_notes,
    };
  }

  /** 推进**单只标的**的四角色（check→draft→risk→revise）严格串行。
   * `existing` 非空表示从断点恢复：复用已有运行、不重建证据包（证据同源，已完成角色不重跑）。
   * 返回 "done" = 该只已跑完并交付；"stopped" = 已停在原阶段（原因写入 collabError，等用户点「继续执行」）。
   * 2026-09-10 真机教训：阶段失败必须立刻停下，绝不能在失败阶段上无限自动重试——那会把整个工作台锁死。 */
  async function advanceCollabSymbol(target: string, q: string, bars: number, existing: CollabRunView | null): Promise<"done" | "stopped"> {
    let current = existing;
    if (!current) {
      const stageProfiles = Object.fromEntries(Object.entries(collabStageModels).filter(([, value]) => value));
      const created = await onCreateCollabRun(target, q, bars, stageProfiles);
      if (!created) {
        setCollabError(`「${target}」协同运行创建失败：证据包构建未通过（全部来源取数失败）或模型配置不可用。本批已停下，可换方案后点「继续执行」。`);
        return "stopped";
      }
      current = created;
      setCollabRuns([current]);
    }
    let reportId: string | null = null;
    while (!current.done) {
      const step = await onNextCollabStage(current.run_id);
      if (!step) {
        setCollabError(`「${current.symbol}」阶段执行请求失败，可点「继续执行」恢复（已完成角色不重跑）。`);
        return "stopped";
      }
      current = step.run;
      reportId = step.collabReportId ?? reportId;
      setCollabRuns([current]);
      if (step.executedStatus === "failed") {
        const failedStage = current.stages[current.next_index];
        setCollabError(
          `「${current.symbol}」阶段「${failedStage?.label ?? step.executedStage}」执行失败：${failedStage?.error ?? "原因未知"}`
          + "。已停在原阶段：可点「继续执行」重试（已完成角色不重跑），也可调大该模型方案的超时后重试。",
        );
        return "stopped";
      }
    }
    const report = collabReportFrom(current);
    // v32：协同修订稿落库后的报告 id 随装配稿下发（追问复盘页签入口）。
    if (report && reportId) report.report_id = reportId;
    if (report) onCollabFinished(report);
    return "done";
  }

  /** v23 协同流水线（角色级模型分配）：四角色严格串行——每个角色用各自指定的模型依次出话
   * （check→draft→risk→revise），修订稿完成即时进入对比区并落库。未指定角色的用「使用中」方案。
   * 2026-09-12 用户追加：支持**队列串行批量**——一只完整跑完并落库后，再按队列顺序生成下一只，
   * 全程共用同一套角色级模型设置（即「按协同流水线的模型设置接着生成下一份」）。
   * 任一只失败即整批停下并保留断点，等用户点「继续执行」从该只重试，绝不无限自动重试。 */
  async function runCollabPipeline(targets: string[], q: string, bars: number): Promise<void> {
    setCollabBusy(true);
    setCollabError(null);
    setCollabRuns([]);
    setCollabTargets(targets);
    setCollabIndex(0);
    setCollabFinishedCount(0);
    try {
      for (let index = 0; index < targets.length; index += 1) {
        setCollabIndex(index);
        const status = await advanceCollabSymbol(targets[index]!, q, bars, null);
        if (status !== "done") return;
        setCollabFinishedCount(index + 1);
      }
      onRefreshReports();
    } finally {
      setCollabBusy(false);
    }
  }

  /** 从断点继续：先把当前标的那个未完成的运行跑完，再按队列顺序接着跑剩下的标的。 */
  async function resumeCollabPipeline(): Promise<void> {
    if (collabBusy || collabTargets.length === 0) return;
    setCollabBusy(true);
    setCollabError(null);
    try {
      const pendingRun = collabRuns.find((run) => !run.done) ?? null;
      for (let index = collabIndex; index < collabTargets.length; index += 1) {
        setCollabIndex(index);
        const status = await advanceCollabSymbol(
          collabTargets[index]!,
          question.trim(),
          klineRange,
          index === collabIndex ? pendingRun : null,
        );
        if (status !== "done") return;
        setCollabFinishedCount(index + 1);
      }
      onRefreshReports();
    } finally {
      setCollabBusy(false);
    }
  }

  /** v23 阶段 0：把本批成功的对比报告送综合（共识/分歧/待核验）。 */
  async function runCompareSynthesis(q: string): Promise<void> {
    if (!compareResults) return;
    const reports = compareResults
      .filter((entry) => entry.report)
      .map((entry) => ({
        symbol: entry.report!.symbol,
        model: entry.report!.model,
        confidence: entry.report!.confidence,
        executive_summary: entry.report!.executive_summary ?? "",
        report: entry.report!.report,
      }));
    if (reports.length < 2) return;
    setSynthesisBusy(true);
    setSynthesisError(null);
    setSynthesis(null);
    try {
      const result = await onSynthesizeCompare(reports, q);
      if (result) setSynthesis(result);
      else setSynthesisError("综合失败：模型调用或输出解析未通过，请重试。");
    } catch {
      setSynthesisError("综合请求失败，请重试。");
    } finally {
      setSynthesisBusy(false);
    }
  }
  // v33 D06/D07/D08：报告交接按唯一 requestId 消费——同股重复跳转有效，迟到旧请求不覆盖新请求。
  // 进入明确的单股上下文：批量队列暂存（可一键恢复），生成目标=交接标的；研究问题随交接预填。
  useEffect(() => {
    if (!reportHandoff) return;
    if (queue.length > 0 && !queue.includes(reportHandoff.symbol)) {
      setStashedQueue((current) => Array.from(new Set([...current, ...queue])));
      onQueueSet([]);
    }
    setSymbol(reportHandoff.symbol);
    if (reportHandoff.question) setQuestion(reportHandoff.question);
    onTabChange("stock");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportHandoff?.requestId]);

  /** v33 D07：「研究该股」= 明确的单股上下文。旧批量队列暂存并显示恢复入口，绝不静默覆盖。 */
  function studySingle(target: string, prefillQuestion?: string): void {
    if (queue.length > 0) {
      setStashedQueue((current) => Array.from(new Set([...current, ...queue])));
      onQueueSet([]);
    }
    setSymbol(target);
    if (prefillQuestion) setQuestion(prefillQuestion);
    onTabChange("stock");
  }

  /** v33 D07：恢复被暂存的批量队列（用户显式操作，不自动合并）。 */
  function restoreQueue(): void {
    if (stashedQueue.length === 0) return;
    onQueueSet(stashedQueue);
    setStashedQueue([]);
  }

  return (
    <div className="wb-panel">
      <div className="wb-tabs" role="tablist" aria-label="研究类型">
        <button role="tab" aria-selected={tab === "direction"} className={tab === "direction" ? "active" : ""} onClick={() => onTabChange("direction")}>方向研判</button>
        <button role="tab" aria-selected={tab === "stock"} className={tab === "stock" ? "active" : ""} onClick={() => onTabChange("stock")}>个股研报</button>
      </div>
      {!aiReady && (
        <p className="form-error">AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」，再生成研究内容。</p>
      )}

      {tab === "direction" && (
        <>
          <details className="wb-newstudy" id="wb-direction-newstudy" key={`dir-${String(!!directionReport)}`} open={!directionReport}>
            <summary aria-label="展开或收起新建方向研判表单">
              <b>新建方向研判</b>
              <span className="wb-newstudy-brief">主题 {topic.trim() || "未填"}{directionBusy ? " · 研判中…" : ""}</span>
            </summary>
            <p className="wb-lede">
              输入产业/主题关键词：有可用证据时生成「证据研判」（[E#] 可回链），无证据时如实输出「知识概览」并声明数据缺口。
              候选公司带证券身份核验与入选解释；<b className="wb-warn">不提供模型估价，价格以行情数据为准。</b>
            </p>
            {/* v35 D02：输入提示按主题类型更新（风格/筛选类主题提示估值口径证据可用）。 */}
            <p className="wb-lede wb-lede-hint" data-testid="direction-theme-hint">
              {directionThemeHint(topic)}
            </p>
            <div className="wb-form">
            <Field label="研究方向">
              <input
                placeholder={THEME_TOPIC_PLACEHOLDER}
                aria-label="研究方向主题"
                value={topic}
                onChange={(event) => setTopic(event.target.value)}
              />
            </Field>
            <Field label="关注点（可选）">
              <input
                placeholder="如「国产替代进度」「景气度拐点」"
                aria-label="方向研判关注点"
                value={directionQuestion}
                onChange={(event) => setDirectionQuestion(event.target.value)}
              />
            </Field>
            {/* v33 C01：本次模型可选——只影响本次方向研判，不改全局「使用中」，不影响个股模型选择。 */}
            <Field label="本次模型">
              <select
                aria-label="方向研判本次使用的模型方案"
                value={directionProfileId}
                onChange={(event) => setDirectionProfileId(event.target.value)}
              >
                <option value="">跟随全局使用中{activeProfile ? `（${activeProfile.name} · ${activeProfile.model}）` : "（未配置时不可用）"}</option>
                {modelProfiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.name} · {profile.model}{profile.status === "使用中" ? "（使用中）" : ""}
                  </option>
                ))}
              </select>
            </Field>
            {/* v33 C03：所选方案失效时保留问题并提供重新选择入口。 */}
            {directionProfileId && !modelProfiles.some((profile) => profile.profile_id === directionProfileId) && (
              <p className="form-error" role="alert">
                所选模型方案已被删除或不可用——请重新选择（研究问题已保留）。
                <button className="tag-button" onClick={() => setDirectionProfileId("")}>改回跟随全局</button>
              </p>
            )}
            <div className="wb-actions">
              <button
                className="primary-btn"
                disabled={!aiReady || directionBusy || topic.trim().length < 2}
                onClick={() => onGenerateDirection(topic.trim(), directionQuestion.trim(), directionProfileId || null)}
              >
                {directionBusy ? "研判中…（最长约 2 分钟，请勿关闭）" : "生成方向研判"}
              </button>
              <span className="wb-hint">产出：概览 + 产业分析 + 候选股表 + 证据与反方审查</span>
            </div>
            </div>
          </details>
          {directionError && <p className="form-error" role="alert">{directionError}</p>}
          {directionReport && (
            <div className="wb-result">
              <h4>{directionReport.title || `方向研判 · ${directionReport.topic}`}</h4>
              <div className="wb-result-meta">
                <span className="soft-tag">{directionReport.topic}</span>
                <span className="soft-tag blue" title="实际执行本次研判的模型">{directionReport.model}</span>
                {directionReport.generation_trace?.profile_name && (
                  <span className="soft-tag gray" title="C04：本次请求冻结的方案快照">方案 {directionReport.generation_trace.profile_name}</span>
                )}
                <span className="report-meta">{new Date(directionReport.generated_at).toLocaleString()}</span>
                {/* v33 C05：换模型重做——先展开新建表单切换「本次模型」，重做会产生新记录（旧记录保留可对比）。 */}
                <button
                  className="ghost-btn"
                  title="先在新建表单里切换「本次模型」，再重做；重做产生新记录，旧记录保留用于对比"
                  onClick={() => document.getElementById("wb-direction-newstudy")?.setAttribute("open", "")}
                >换模型重做</button>
              </div>
              <ExportBar
                filenameBase={`方向研判-${directionReport.topic}-${directionReport.generated_at.slice(0, 10)}`}
                markdown={buildDirectionReportMarkdown(directionReport)}
                htmlDocument={buildHtmlDocument(
                  directionReport.title || `方向研判 · ${directionReport.topic}`,
                  [directionReport.topic, `模型 ${directionReport.model}`, new Date(directionReport.generated_at).toLocaleString()],
                  markdownToHtml(buildDirectionReportMarkdown(directionReport).replace(/^# .*$/m, "").trim()),
                )}
              />
              {/* v33 A06：方向阅读区四页签（概览 / 产业分析 / 候选股 / 证据与风险）；A17 候选表内置。
                  v39 追问接线：第五页签「追问复盘」——后端按报告 kind 路由到方向分支（J# 编号闸门）。 */}
              <DirectionResultTabs
                report={directionReport}
                onSendToTactics={onSendToTactics}
                onStudySingle={studySingle}
                followup={directionReport.report_id ? (
                  <FollowUpPanel
                    reportId={directionReport.report_id}
                    turns={directionFollowupTurns}
                    loading={directionFollowupLoading}
                    busy={directionFollowupBusy}
                    error={directionFollowupError}
                    draft={directionFollowupDraft}
                    onDraftChange={setDirectionFollowupDraft}
                    onSubmit={submitDirectionFollowUp}
                  />
                ) : undefined}
                followupCount={directionFollowupTurns.length}
                followupActive={directionFollowupBusy || directionFollowupLoading}
              />
            </div>
          )}
          {!directionReport && !directionBusy && !directionError && (
            <div className="wb-empty">
              <b>还没有方向研判结果</b>
              输入产业/主题后点「生成方向研判」。候选股表里可勾选后送战法雷达做 K 线筛选。
            </div>
          )}
          {directionBusy && !directionReport && <div className="wb-empty wb-loading">模型正在研判方向，最长约 2 分钟…</div>}
        </>
      )}

      {tab === "stock" && (
        <>
          {/* D02（前端设计与架构优化任务路线图 2026-09-19）：来源与返回常驻——交接的标的/来源/带入问题
              此前只出现在可折叠表单的摘要里，阅读态不可见；返回按钮给出跨页往返的后半程。 */}
          {reportHandoff && symbol.trim() === reportHandoff.symbol && (
            <ReportSourceBar handoff={reportHandoff} onNavigate={onNavigate} />
          )}
          {/* v33 A02：新建与阅读分离——有结果（或协同运行）时表单默认折叠为摘要行，点开可调整配置；
              无结果/失败时展开，输入与错误保留。 */}
          <details className="wb-newstudy" key={`stock-${String(hasStockResults)}`} open={!hasStockResults}>
            <summary aria-label="展开或收起新建个股研报表单">
              <b>新建个股研报</b>
              <span className="wb-newstudy-brief">
                标的 {symbol.trim() || "未填"} · 模型 {selectedProfileIds.length || "未选"} 个
                {queue.length > 0 ? ` · 队列 ${queue.length} 只` : ""}
                {(busy || compareBusy || collabBusy) ? " · 生成中…" : ""}
              </span>
            </summary>
            <p className="wb-lede">
              选股后生成「新闻/公告/财报 + K 线战法快照」深研报告。
              <b className="wb-warn">研究参考，不构成投资建议。</b>
            </p>
            {/* v36 排版修正：wb-form 默认三列（第三列 auto）会把模板 chips 挤进窄格、按钮悬在中列；
                wb-form-study 收成两列主行，模板/模型/操作区/高级设置各自整行。 */}
            <div className="wb-form wb-form-study">
              <Field label="标的代码">
                <input
                  placeholder="手动输入：600519 / 000060 / 510300"
                  aria-label="研究报告标的代码"
                  value={symbol}
                  onChange={(event) => setSymbol(event.target.value)}
                />
              </Field>
              <Field label="关注点（可选）">
                <input
                  placeholder="如「趋势能否延续」「估值是否合理」"
                  aria-label="研究报告关注点"
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                />
              </Field>
              <div className="wb-quick">
                <span>研究问题模板（点击填入，可改：对象与时间范围 · 待验证假设 · 证据范围 · 反证条件）：</span>
                <div className="pref-row" role="group" aria-label="研究问题模板">
                  {[
                    "未来一个季度，该标的中期趋势能否延续？待验证假设：现处上行结构且量能配合；重点核对近 60 日价量与公告；反证条件：跌破关键支撑或出现重大利空公告。",
                    "该标的当前估值处于什么位置？待验证假设：估值相对盈利增速合理；证据范围：最近 8 期财务摘要与一致预期；反证条件：盈利下修或估值分位极端偏离。",
                    "该标的近 30 日公告与新闻是否改变原有投资逻辑？待验证假设：无重大逻辑变化；证据范围：全部在案公告/新闻；反证条件：出现与失效条件匹配的事件。",
                  ].map((template) => (
                    <button
                      key={template.slice(0, 8)}
                      type="button"
                      className="tag-button"
                      title={template}
                      onClick={() => setQuestion(template)}
                    >
                      {template.slice(0, 10)}…
                    </button>
                  ))}
                </div>
              </div>
              {/* v33 A03：本次模型在首层（带可见标签）；K 线窗口/篇幅/执行方式/角色模型/批量队列收进「高级设置」。 */}
              {modelProfiles.length > 0 && (
                <Field label="本次模型（可多选，结果同屏对比）">
                  <SelectPicker
                    multi
                    minWidth={300}
                    placeholder="选择生成模型（可筛选）…"
                    value={selectedProfileIds}
                    options={modelProfiles.map((profile) => ({ value: profile.profile_id, label: `${profile.name} · ${profile.model}` }))}
                    onChange={onSetProfiles}
                  />
                </Field>
              )}
              <div className="wb-actions">
                <button
                  className="primary-btn"
                  disabled={
                    !aiReady || busy || compareBusy || collabBusy ||
                    (queue.length === 0 && symbol.trim().length < 2) ||
                    (!collabMode && selectedProfileIds.length === 0)
                  }
                  onClick={() => {
                    const symbols = queue.length > 0 ? queue : [symbol.trim()];
                    if (collabMode) {
                      void runCollabPipeline(symbols, question.trim(), klineRange);
                      return;
                    }
                    if (symbols.length > 1 || selectedProfileIds.length > 1) onGenerateCompare(symbols, question.trim(), klineRange, selectedProfileIds, execMode, reportMode);
                    else onGenerate(symbols[0] ?? symbol.trim(), question.trim(), klineRange, selectedProfileIds[0] ?? null, reportMode);
                  }}
                >
                  {collabMode
                    ? (collabBusy
                      ? `协同中…第 ${collabIndex + 1}/${collabTargets.length} 只（${collabTargets[collabIndex] ?? ""}），本只已完成 ${collabRuns[0]?.stages.filter((stage) => stage.status === "done").length ?? 0}/4，请勿关闭`
                      : `启动协同流水线（${queue.length > 1 ? `${queue.length} 只股逐只串行，每只` : ""}四角色依次：核对→初稿→风险→修订，可用不同模型）`)
                    : busy || compareBusy
                      ? `生成中…共 ${queue.length > 0 ? queue.length : 1} 只股 × ${selectedProfileIds.length} 模型，已完成 ${(queue.length > 0 ? queue.length : 1) * selectedProfileIds.length - comparePending} 份，请勿关闭`
                      : `生成报告${queue.length > 1 || selectedProfileIds.length > 1 ? `（${queue.length > 1 ? `${queue.length} 只股排队` : ""}${queue.length > 1 && selectedProfileIds.length > 1 ? " × " : ""}${selectedProfileIds.length > 1 ? `${selectedProfileIds.length} 模型${execMode === "parallel" ? "并行" : "串行"}` : ""}）` : ""}`}
                </button>
                <span className="wb-hint">
                  {collabMode
                    ? `协同模式：四角色（数据核对→首席初稿→风险审查→首席修订）严格串行依次执行；每个角色可用不同模型（在「高级设置」的角色级模型分配指定，留空的角色用「使用中」方案），修订稿自动进入对比区并留档。${queue.length > 1 ? `已排队 ${queue.length} 只：逐只串行——一只完整跑完四角色并落库后，再按队列顺序开始下一只；任一只失败即整批停下，可点「继续执行」从该只恢复。` : ""}`
                    : `产出：概览 + 正文 + 图表 + 情景与验证 + 证据 + 追问${queue.length > 0 ? `；队列逐只串行，勾选的模型在每只股内${execMode === "parallel" ? "并行" : "串行"}` : ""}`}
                </span>
              </div>
              {/* v33 A03：高级设置折叠——个股专用参数不挤占首层。 */}
              <details className="wb-advanced">
                <summary>高级设置（K 线窗口 · 篇幅档 · 执行方式 · 角色模型 · 批量队列）</summary>
                <div className="wb-quick">
                  <span>K 线回看区间（影响技术面分析窗口）：</span>
                  <div className="pref-row" role="group" aria-label="K 线回看区间">
                    {([60, 120, 250] as const).map((days) => (
                      <button
                        key={days}
                        className={`tag-button ${klineRange === days ? "active" : ""}`}
                        aria-pressed={klineRange === days}
                        onClick={() => setKlineRange(days)}
                      >
                        {days} 日
                      </button>
                    ))}
                  </div>
                </div>
                {/* v29 研报字数预算档：只影响软告警阈值与提示词预算，不改变引用真实性等硬闸门。 */}
                <div className="wb-quick">
                  <span>研报篇幅档（软告警预算）：</span>
                  <div className="pref-row" role="group" aria-label="研报篇幅档">
                    <button
                      className={`tag-button ${reportMode === "quick" ? "active" : ""}`}
                      aria-pressed={reportMode === "quick"}
                      disabled={busy || compareBusy || collabBusy}
                      title="快速：1500–2500 字。适合批量初筛。"
                      onClick={() => setReportMode("quick")}
                    >快速</button>
                    <button
                      className={`tag-button ${reportMode === "standard" ? "active" : ""}`}
                      aria-pressed={reportMode === "standard"}
                      disabled={busy || compareBusy || collabBusy}
                      title="标准：2000–3500 字（默认）。"
                      onClick={() => setReportMode("standard")}
                    >标准</button>
                    <button
                      className={`tag-button ${reportMode === "deep" ? "active" : ""}`}
                      aria-pressed={reportMode === "deep"}
                      disabled={busy || compareBusy || collabBusy}
                      title="深研：3500–6000 字。仅单股生成接入；协同流水线仍按标准档。"
                      onClick={() => setReportMode("deep")}
                    >深研</button>
                  </div>
                </div>
                <div className="wb-quick">
                  <span>多模型执行方式：</span>
                  <div className="pref-row" role="group" aria-label="多模型执行方式">
                    <button
                      className={`tag-button ${!collabMode && execMode === "parallel" ? "active" : ""}`}
                      aria-pressed={!collabMode && execMode === "parallel"}
                      disabled={busy || compareBusy || collabBusy}
                      title="同一只股内勾选的模型同时生成（快，费额度快）"
                      onClick={() => { setCollabMode(false); setExecMode("parallel"); }}
                    >模型并行（快）</button>
                    <button
                      className={`tag-button ${!collabMode && execMode === "serial" ? "active" : ""}`}
                      aria-pressed={!collabMode && execMode === "serial"}
                      disabled={busy || compareBusy || collabBusy}
                      title="同一只股内的模型依次逐个生成（省额度，避开限速）"
                      onClick={() => { setCollabMode(false); setExecMode("serial"); }}
                    >模型串行（省额度）</button>
                    <button
                      className={`tag-button ${collabMode ? "active" : ""}`}
                      aria-pressed={collabMode}
                      disabled={busy || compareBusy || collabBusy}
                      title="协同流水线：数据核对→首席初稿→风险审查→首席修订，四个角色依次执行；队列多只时逐只串行，一只完整跑完并落库后再按角色级模型设置生成下一只"
                      onClick={() => setCollabMode((current) => !current)}
                    >协同流水线（四角色依次）</button>
                  </div>
                </div>
                {collabMode && modelProfiles.length > 0 && (
                  <div className="wb-quick">
                    <span>角色级模型分配（每个角色一个模型，严格串行依次执行；留空的角色用「使用中」方案）：</span>
                    {COLLAB_STAGE_DEFS.map((def) => (
                      <div key={def.key} className="pref-row" role="group" aria-label={`角色「${def.label}」的模型选择`}>
                        <span style={{ minWidth: 92, display: "inline-block" }}>{def.label}：</span>
                        <SelectPicker
                          minWidth={280}
                          placeholder="使用中方案"
                          value={[collabStageModels[def.key] ?? ""].filter((item) => item !== "")}
                          options={modelProfiles.map((profile) => ({ value: profile.profile_id, label: `${profile.name} · ${profile.model}` }))}
                          onChange={(next) => setCollabStageModels((prev) => ({ ...prev, [def.key]: next[0] ?? "" }))}
                        />
                      </div>
                    ))}
                  </div>
                )}
                {candidates.length > 0 && (
                  <div className="wb-quick">
                    <span>从持仓 / 自选快选（下拉多选加入生成队列，支持筛选）：</span>
                    <SelectPicker
                      multi
                      minWidth={320}
                      placeholder={`从持仓 / 自选选择（共 ${candidates.length} 只）…`}
                      value={queue}
                      options={candidates.map((item) => ({ value: item.instrument, label: `${item.label} ${item.instrument}` }))}
                      onChange={onQueueSet}
                    />
                    {queue.length > 0 && (
                      <span className="pool-filter-count">已排队 {queue.length} 只：{queue.join(" → ")}</span>
                    )}
                    {/* v33 D07：被单股研究暂存的批量队列——显式恢复，不自动合并。 */}
                    {stashedQueue.length > 0 && (
                      <button className="tag-button" onClick={restoreQueue} title="恢复之前因「研究该股」而暂存的批量队列">
                        恢复暂存的批量队列（{stashedQueue.length} 只）
                      </button>
                    )}
                  </div>
                )}
              </details>
            </div>
          </details>
          {collabMode && collabTargets.length > 0 && (
            <div className="wb-collab-queue" role="status" aria-live="polite">
              <span className="wb-collab-queue-title">
                协同批次进度（共 {collabTargets.length} 只，已完成 {collabFinishedCount} 只）：
              </span>
              <ol className="wb-collab-queue-list">
                {collabTargets.map((item, index) => {
                  const state = index < collabFinishedCount
                    ? "done"
                    : index === collabIndex
                      ? (collabBusy ? "running" : "pending")
                      : "pending";
                  const doneStages = state === "running"
                    ? collabRuns[0]?.stages.filter((stage) => stage.status === "done").length ?? 0
                    : 0;
                  return (
                    <li key={`${item}-${index}`} className={`wb-collab-queue-item is-${state}`}>
                      <span className="wb-collab-queue-symbol">{item}</span>
                      <span className="wb-collab-queue-state">
                        {state === "done" ? "已完成" : state === "running" ? `进行中 ${doneStages}/4` : "待执行"}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
          {compareResults && compareResults.length > 1 && (
            <div className="wb-quick">
              <span>生成结果（点切换查看）：</span>
              <div className="pref-row" role="group" aria-label="生成结果切换">
                {compareResults.map((entry, index) => (
                  <button
                    key={`${entry.symbol}-${entry.profileId ?? entry.name}-${index}`}
                    className={`tag-button ${index === activeCompare ? "active" : ""}`}
                    aria-pressed={index === activeCompare}
                    onClick={() => onActiveCompareChange(index)}
                  >
                    {entry.symbol}
                    {entry.pending
                      ? <i style={{ display: "block", marginTop: 2 }}>生成中…</i>
                      : entry.report
                        ? <i style={{ display: "block", marginTop: 2 }}>{entry.name} · 置信 {entry.report.confidence}</i>
                        : <i style={{ display: "block", marginTop: 2 }}>{entry.name} · 失败</i>}
                  </button>
                ))}
              </div>
            </div>
          )}
          {compareResults && compareResults.length > 1 && (
            <BatchExportBar entries={compareResults} disabled={compareBusy} />
          )}
          {compareResults && !compareBusy && compareResults.filter((entry) => entry.report).length >= 2 && (
            <div className="wb-export" role="group" aria-label="跨报告综合">
              <button
                className="ghost-btn"
                disabled={synthesisBusy || collabBusy}
                onClick={() => void runCompareSynthesis(question.trim())}
              >
                {synthesisBusy ? "综合中…" : `综合本批结论（${compareResults.filter((entry) => entry.report).length} 份成功报告）`}
              </button>
              {synthesisError && <span className="wb-export-note">{synthesisError}</span>}
            </div>
          )}
          {synthesis && (
            <div className="wb-result">
              <h4>跨报告综合 · 共识与分歧</h4>
              <div className="wb-result-meta">
                <span className="soft-tag blue">{synthesis.model}</span>
                <span className="report-meta">{new Date(synthesis.generated_at).toLocaleString()}</span>
              </div>
              <p><b>{synthesis.summary}</b></p>
              {synthesis.consensus.length > 0 && (
                <div><span className="answer-block-title">共识点</span><ul className="answer-list">{synthesis.consensus.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
              )}
              {synthesis.divergences.length > 0 && (
                <div><span className="answer-block-title">分歧点（如实陈列，未投票未调和）</span><ul className="answer-list">{synthesis.divergences.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
              )}
              {synthesis.to_verify.length > 0 && (
                <div><span className="answer-block-title">待人工核验</span><ul className="answer-list">{synthesis.to_verify.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
              )}
              <ExportBar
                filenameBase={`跨报告综合-${synthesis.generated_at.slice(0, 10)}`}
                markdown={buildCompareSynthesisMarkdown(synthesis)}
                htmlDocument={buildHtmlDocument(
                  "跨报告综合 · 共识与分歧",
                  [`模型 ${synthesis.model}`, new Date(synthesis.generated_at).toLocaleString()],
                  markdownToHtml(buildCompareSynthesisMarkdown(synthesis).replace(/^# .*$/m, "").trim()),
                )}
              />
            </div>
          )}
          {collabError && (
            <p className="form-error" role="alert">{collabError}</p>
          )}
          {collabRuns.map((run, runIndex) => (
            <CollabResultBlock
              key={run.run_id}
              run={run}
              busy={collabBusy && !run.done}
              titlePrefix={collabRuns.length > 1 ? `（模型 ${runIndex + 1}/${collabRuns.length}）` : ""}
              onResume={() => void resumeCollabPipeline()}
            />
          ))}
          {(() => {
            const entryIndex = Math.min(activeCompare, (compareResults?.length ?? 1) - 1);
            const entry = compareResults?.[entryIndex];
            if (entry) {
              if (entry.pending) {
                return (
                  <div className="wb-result">
                    <h4>{entry.symbol} · {entry.name} · 生成中</h4>
                    <div className="wb-empty wb-loading">队列中该份报告正在生成，完成后自动切换显示…</div>
                  </div>
                );
              }
              if (!entry.report) {
                return (
                  <div className="wb-result">
                    <h4>{entry.symbol} · {entry.name} · 生成失败</h4>
                    <p className="form-error" role="alert">{entry.error ?? "模型调用或证据解析未通过。"}</p>
                    {!compareBusy && (
                      <button className="ghost-btn" onClick={() => retryCompareEntry(entryIndex)}>
                        重试这一份（{entry.symbol} · {entry.name}，其余份数不动）
                      </button>
                    )}
                  </div>
                );
              }
              const report = entry.report;
              return (
            <div className="wb-result">
              <h4>{report.title}</h4>
              <div className="wb-result-meta">
                <span className="soft-tag">{report.symbol}</span>
                <span className="soft-tag blue">{report.model}</span>
                <span className="soft-tag">置信 {report.confidence}</span>
                {/* B04（2026-09-15 方向研判路线图）：如实显示模型初稿耗时（总请求还含闸门/反方检查/修复，会更长；旧记录缺失时不显示）。 */}
                {typeof report.latency_ms === "number" && report.latency_ms > 0 && (
                  <span className="report-meta">模型生成 {Math.round(report.latency_ms / 1000)} 秒</span>
                )}
                <span className="report-meta">{new Date(report.generated_at).toLocaleString()}</span>
              </div>
              {/* v31 §4.1 首屏固定行：模式 · 字数 · 三态质量状态；不完整时红色横幅 + 缺失项 */}
              <ReportFirstScreen report={report} />
              {report.quality_status === "incomplete" && (
                <div className="wb-actions">
                  <button
                    className="ghost-btn"
                    onClick={() => onGenerate(report.symbol, question.trim(), klineRange, entry.profileId, "standard")}
                  >
                    切换标准模式重新生成完整报告
                  </button>
                </div>
              )}
              {/* v33 A05：结论卡与执行摘要移入「概览」页签（ReportResultTabs overview），首屏只留身份 + 驾驶舱 + 质量。 */}
              {(() => {
                /* v32：导出 Markdown 与追问附录同源——导出件包含全部追问附录（不可变追加记录）。 */
                const reportMarkdown = buildStockReportMarkdown(report, followupTurns);
                /* J08（2026-09-18 桌面端路线图）：文件名带上标的名称。名称只取自选/持仓的
                   label（服务端标的查找的结果），取不到就只留 6 位代码——不解析模型写的标题、不留空。
                   R04：单导与批量 ZIP 共用 reportFileBase，保证两侧文件名口径一致。 */
                const instrumentLabel = candidates.find((item) => item.instrument === report.symbol)?.label?.trim() ?? "";
                return (
                  <ExportBar
                    filenameBase={reportFileBase(report.symbol, instrumentLabel, report.model, report.generated_at.slice(0, 10))}
                    markdown={reportMarkdown}
                    htmlDocument={buildHtmlDocument(
                      report.title,
                      [report.symbol, `模型 ${report.model}`, `置信 ${report.confidence}`, new Date(report.generated_at).toLocaleString()],
                      markdownToHtml(reportMarkdown.replace(/^# .*$/m, "").trim(), keyFactHighlights(report)),
                    )}
                  />
                );
              })()}
              {/* v30 结果区页签：结构化判断 / 正文 / 证据附录；v32 增「追问复盘」页签 */}
              <ReportResultTabs
                report={report}
                followup={report.report_id ? (
                  <FollowUpPanel
                    reportId={report.report_id}
                    turns={followupTurns}
                    loading={followupLoading}
                    busy={followupBusy}
                    error={followupError}
                    draft={followupDraft}
                    onDraftChange={setFollowupDraft}
                    onSubmit={submitFollowUp}
                  />
                ) : undefined}
                followupCount={followupTurns.length}
                followupActive={followupBusy || followupLoading}
                bodyAppend={
                  report.limitations.length > 0 ? (
                    <div>
                      <span className="answer-block-title">局限与口径声明</span>
                      <ul className="answer-list">{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                    </div>
                  ) : null
                }
                evidenceAppend={
                  report.source_errors && Object.keys(report.source_errors).length > 0 ? (
                    <p className="report-meta amber">部分来源取数失败（报告已如实标注，未编造）：{Object.values(report.source_errors).join("；")}</p>
                  ) : null
                }
              />
              <p className="report-meta">研究参考，不构成投资建议；关键判断请回链原始来源复核。</p>
            </div>
              );
            }
            return null;
          })()}
          {!compareResults && !busy && !compareBusy && (
            <div className="wb-empty">
              <b>还没有个股研报</b>
              从持仓/自选快选或手动输入代码，生成深研报告。可在「生成模型」里勾选多个模型并行生成、同屏对比。也可先在「方向研判」拿到标的池，经战法雷达筛完 K 线再回来深研。
            </div>
          )}
          {(busy || compareBusy) && !compareResults && (
            <div className="wb-empty wb-loading">
              正在拉取新闻/财报与 K 线并生成报告，完成后自动展示。深研档与多模型串行耗时较长（单份可达 5-10 分钟），请勿关闭窗口。
            </div>
          )}
          {/* B02/B04（2026-09-15 路线图）：单股失败原因 + 恢复动作在此展示（此前 reportError 从未被渲染）。 */}
          {busy ? null : reportError === null ? null : (
            <p className="form-error" role="alert">{reportError}</p>
          )}
        </>
      )}
    </div>
  );
}
