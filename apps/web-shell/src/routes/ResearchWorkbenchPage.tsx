/**
 * 研究工作台（v21 独立页，2026-09-08 用户拍板从投资页行情 tab 迁出）：
 *   方向研判（先筛方向）→ 标的池送战法雷达筛 K 线 → 个股研报（重点深研）。
 * 三步链路的跨页联动由 AppShell 承担（tacticsExternalPool / pendingReportSymbol）。
 * UI v2（2026-09-08 用户反馈「ui 再优化」）：可点击三步流程卡 + 分段 tab + 字段化表单 + 空态占位。
 */


import { useEffect, useMemo, useRef, useState } from "react";
import { ModelProfile } from "@investment-steward/domain-contracts";
import { createCoreClient } from "../state/coreClient";
import { taskCenterRemove, taskCenterUpsert } from "../state/taskCenter";
import { useResearch, type AiReportListParams } from "../hooks/useResearch";
import { ReportHandoff } from "../state/handoff";
import { DemoNotice } from "../components/DemoNotice";
import { ReviewQueuePanel } from "./workbench/panels";
import { subscribeCompareJob, updateCompareJob, startCompareJob, compareJobSnapshot, type CompareJobSnapshot } from "./workbench/compareJob";
import { presentStockReportFailure } from "./workbench/reportErrors";
import { StockReport, DirectionReport, AiReportItem } from "./workbench/researchTypes";
import { AppView } from "../shell/nav";
import { StockReportPanel } from "./workbench/stockPanel";
import { WorkbenchTab } from "./workbench/researchTypes";

interface WorkbenchProps {
  holdings: { instrument: string; label: string }[];
  aiReady: boolean;
  /** 路线图 A1：演示模式明示（内容为界面示例，刷新即还原、不落库）。 */
  isDemo: boolean;
  /** v22 多模型对比：已配置的模型方案清单（含未启用的）。 */
  modelProfiles: ModelProfile[];
  /** B2：自选入库复用 AppShell 的 createHolding（战法雷达跳转后一键加自选等联动）。 */
  onCreateHolding: (input: { instrument: string; label: string; status: "holding" | "watchlist"; strategy_note: string }) => Promise<boolean>;
  /** v33 D02/D03：候选池 → 战法雷达的结构化交接（发送范围由调用方决定，AppShell 生成 requestId）。 */
  onSendToTactics: (pool: string[], context?: { sourceLabel?: string; sourceReportId?: string; topic?: string; question?: string; excluded?: { symbol: string; reason: string }[] }) => void;
  /** D02（前端设计与架构优化任务路线图 2026-09-19）：视图导航（AppShell setView）——流程卡第②步跳战法雷达、来源条「返回来源」。 */
  onNavigate: (view: AppView) => void;
  /** B2：当前视图是否为研究工作台页（KeepAlive 常驻挂载）。 */
  active: boolean;
  /** 战法雷达「AI 报告」跳转预填的标的代码（AppShell 一次性下发）。 */
  /** v33 D06：雷达/方向 → 单股研究的结构化交接（唯一 requestId）。 */
  reportHandoff: ReportHandoff | null;
}

export function ResearchWorkbenchPage({
  holdings,
  aiReady,
  isDemo,
  modelProfiles,
  active,
  onCreateHolding,
  onSendToTactics,
  onNavigate,
  reportHandoff,
}: WorkbenchProps) {
  // B2：AI 研究产出领域数据自取（回调 props 已清零；别名对齐既有局部命名，页面主体零改动）。
  const client = useMemo(() => createCoreClient(), []);
  const {
    aiReports: reports,
    aiReportsTotal,
    aiReportFacets,
    researchReviewQueue: reviewQueue,
    generateStockReport: onGenerateStockReport,
    generateDirectionReport: onGenerateDirection,
    synthesizeCompareReports: onSynthesizeCompare,
    createCollabRun: onCreateCollabRun,
    runNextCollabStage: onNextCollabStage,
    loadAiReports: onRefreshReports,
    loadAiReportDetail: onLoadReportDetail,
    deleteAiResearchReport: onDeleteReport,
    createFollowUpTurn: onCreateFollowUp,
    loadFollowUpTurns: onLoadFollowUps,
    // G01：验证点回填（成立/失效/无数据）——复用既有 verify 通道，成功后钩子自己刷新队列。
    verifyJudgment: onVerifyJudgment,
  } = useResearch(client);
  const [tab, setTab] = useState<WorkbenchTab>("stock");
  const [reportBusy, setReportBusy] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [directionBusy, setDirectionBusy] = useState(false);
  const [directionReport, setDirectionReport] = useState<DirectionReport | null>(null);
  const [directionError, setDirectionError] = useState<string | null>(null);
  // v22 多模型对比：选中要并行的方案（默认勾「使用中」的那个）、结果集与当前查看的 tab。
  const [selectedProfileIds, setSelectedProfileIds] = useState<string[]>([]);
  // 批量生成状态 = 模块级任务快照的镜像：组件重挂载时自动恢复进行中的任务（不丢报告）。
  const [job, setJob] = useState<CompareJobSnapshot>(compareJobSnapshot);
  useEffect(() => subscribeCompareJob(() => setJob({ ...compareJobSnapshot })), []);
  const compareBusy = job.busy;
  const compareResults = job.results;
  const activeCompare = job.activeIndex;
  const comparePending = job.pending;
  // v22 批量队列：加入生成队列的标的（快选 chips 多选），逐只串行生成。
  const [queue, setQueue] = useState<string[]>([]);
  // v23 历史产出分类（用户需求：方便二次审查）：类型筛选 + 模型筛选 + 时间范围 + 关键字过滤。
  const [historyKind, setHistoryKind] = useState<"all" | "direction" | "stock" | "collab">("all");
  const [historyQuery, setHistoryQuery] = useState("");
  const [historyModel, setHistoryModel] = useState("all");
  const [historyRange, setHistoryRange] = useState<"all" | "7" | "30">("all");
  // v33 A16：质量/草稿筛选 + 分页（默认 10 条/页，可 20；打开报告后返回时恢复筛选与页码）。
  const [historyQuality, setHistoryQuality] = useState<"all" | "formal" | "draft" | "shallow">("all");
  const [historyPage, setHistoryPage] = useState(1);
  const [historyPageSize, setHistoryPageSize] = useState<10 | 20>(10);
  // v33 A01：一级分区（研究报告 / 待验证 / 历史报告）——切换保留输入、任务、筛选与阅读位置。
  const [section, setSection] = useState<"reports" | "verify" | "history">("reports");
  const isCollabReport = (item: AiReportItem): boolean => item.kind === "stock" && typeof item.model === "string" && (item.model as string).endsWith("·协同流水线");
  // B01（桌面端升级路线图 2026-09-18）：筛选/分页下沉 SQL——页签计数与模型清单来自服务端
  // COUNT(*) 真值（aiReportFacets），不再由当前页内存过滤拼出（旧口径在第 51 份起必然失真）。
  const facetCounts = aiReportFacets.counts;
  const historyModels = aiReportFacets.models;
  const dirCount = facetCounts.direction;
  const stockCount = facetCounts.stock;
  const collabCount = facetCounts.collab;
  // 当前页 items 即服务端按筛选返回的一页（旧 filteredReports 内存过滤整段删除）。
  const pagedReports = reports;
  const historyPageCount = Math.max(1, Math.ceil(aiReportsTotal / historyPageSize));
  const historySafePage = Math.min(historyPage, historyPageCount);

  // B01：关键字输入防抖后作为服务端 q 参数（旧实现是逐键内存过滤，现在每次输入是一次请求）。
  const [historyQueryDebounced, setHistoryQueryDebounced] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setHistoryQueryDebounced(historyQuery), 300);
    return () => window.clearTimeout(timer);
  }, [historyQuery]);

  // B01：筛选条件变化时页码回到 1（页码本身不作为依赖，避免翻页时误重置）。
  useEffect(() => {
    setHistoryPage(1);
  }, [historyKind, historyModel, historyQueryDebounced, historyQuality, historyRange]);

  // B01：筛选/页码/页大小变化 → 按服务端参数拉当前页（替换旧的进入页面拉一次全量列表）。
  useEffect(() => {
    const params: AiReportListParams = {
      kind: historyKind === "direction" ? "direction" : historyKind === "collab" || historyKind === "stock" ? "stock" : null,
      collab: historyKind === "collab" ? true : historyKind === "stock" ? false : null,
      model: historyModel === "all" ? null : historyModel,
      q: historyQueryDebounced || null,
      isDraft: historyQuality === "formal" ? false : historyQuality === "draft" ? true : null,
      depth: historyQuality === "shallow" ? "below_min" : null,
      generatedFrom:
        historyRange === "all"
          ? null
          : new Date(Date.now() - (historyRange === "7" ? 7 : 30) * 24 * 3600 * 1000).toISOString(),
      offset: (historySafePage - 1) * historyPageSize,
      limit: historyPageSize,
    };
    void onRefreshReports(params);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyKind, historyModel, historyQueryDebounced, historyQuality, historyRange, historySafePage, historyPageSize]);

  function toggleQueueSymbol(code: string): void {
    setQueue((current) => (current.includes(code) ? current.filter((item) => item !== code) : [...current, code]));
  }

  // 已配置方案变化时（如首载完成），若尚未勾选则默认勾「使用中」的方案。
  useEffect(() => {
    if (selectedProfileIds.length === 0) {
      const active = modelProfiles.find((profile) => profile.status === "使用中");
      if (active) setSelectedProfileIds([active.profile_id]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelProfiles]);

  function toggleProfile(profileId: string): void {
    setSelectedProfileIds((current) => (
      current.includes(profileId)
        ? current.filter((id) => id !== profileId)
        : [...current, profileId]
    ));
  }

  // v31 §八.4：不完整报告（is_draft=true）与正式报告分开计数——草稿可回看，但不计入正式数量。
  // B01：计数来自服务端全局 facets（draft_stock 只数 stock 草稿，与旧口径一致）。
  const draftCount = facetCounts.draft_stock;
  const formalCount = facetCounts.total - facetCounts.draft_stock;

  /** B01：列表项是轻量投影，回看前按 id 取完整落库 payload，再回填对应 tab 的结果区；
   * v33 A01/A16：切回报告分区并定位顶部。 */
  async function openHistoryItem(item: AiReportItem): Promise<void> {
    studyGenerationRef.current += 1; // D02：回看即切换阅读对象，在途生成结果不得覆盖
    const full = await onLoadReportDetail(item.report_id);
    if (!full) {
      setReportError("报告全文读取失败或已被删除，无法回看。");
      return;
    }
    if (full.kind === "direction") {
      setTab("direction");
      setDirectionReport(full as unknown as DirectionReport);
      setDirectionError(null);
    } else {
      setTab("stock");
      const stockItem = full as unknown as StockReport;
      updateCompareJob({ results: [{ symbol: full.subject, name: stockItem.model || full.subject, profileId: null, report: stockItem, error: null }], activeIndex: 0 });
      setReportError(null);
    }
    setSection("reports");
    // 滚动容器是壳层的 #mainScroll（AppShell 的滚动记忆也挂在它上面），window.scrollTo 在桌面宿主里不产生位移。
    const scroller = document.getElementById("mainScroll");
    if (scroller) scroller.scrollTop = 0;
  }

  /** v33 A15：待验证列表跳回来源报告。B01：直接按 id 取全文——来源报告不一定落在当前页，
   * 旧的「在当前页里找 report_id」在翻页后会误报「不存在」。 */
  async function openReportById(reportId: string): Promise<void> {
    const full = await onLoadReportDetail(reportId);
    if (!full) {
      setReportError("来源报告不存在或已删除，无法回看。");
      return;
    }
    await openHistoryItem(full);
  }

  // D02（前端设计与架构优化任务路线图 2026-09-19）：单股研究代次守卫——每次发起生成、每次跨页交接、
  // 每次从历史回看都推进一代；只有当代结果写回结果区，迟到结果不再覆盖当前研究对象
  // （与交接按 requestId 消费同一原则，不另造竞态机制）。
  const studyGenerationRef = useRef(0);
  useEffect(() => {
    studyGenerationRef.current += 1;
  }, [reportHandoff?.requestId]);

  /** B01/B02（2026-09-15 路线图）：单股生成透传所选模型与档位；失败不再转固定文案，
   * 由 reportErrors 把后端 stage/detail/source_errors 呈现为「原因 + 恢复动作」。 */
  function generateReport(target: string, question: string, barsLimit: number, profileId?: string | null, mode?: string): void {
    const generation = ++studyGenerationRef.current;
    const isCurrent = () => studyGenerationRef.current === generation;
    setReportBusy(true);
    setReportError(null);
    updateCompareJob({ results: null, busy: false, pending: 0 });
    void onGenerateStockReport(target, question, barsLimit, profileId ?? undefined, mode)
      .then((result) => {
        if (result.ok) onRefreshReports();
        if (!isCurrent()) return;
        if (result.ok) {
          updateCompareJob({ results: [{ symbol: result.report.symbol, name: result.report.model, profileId: profileId ?? null, report: result.report, error: null }], activeIndex: 0 });
        } else {
          const view = presentStockReportFailure(result.failure);
          setReportError(view.hint ? `${view.message}（${view.hint}）` : view.message);
        }
      })
      .catch(() => {
        if (isCurrent()) setReportError("报告生成请求失败，请重试。");
      })
      .finally(() => {
        if (isCurrent()) setReportBusy(false);
      });
  }

  /**
   * v22 批量队列生成（用户澄清：一次排队生成多个股，不是一股多份）；
   * v22.2 用户需求：模型执行方式可选并行/串行。
   * - parallel（默认）：标的之间串行排队（一只完成自动下一只），同一只股内勾选的多个模型并行；
   * - serial：完全串行——标的逐只、模型逐个，一次只发一个请求（省额度/避开限速）；
   * - 结果渐进更新：先铺出全部占位 tab（pending），每完成一份即时替换并刷新历史。
   */
  function generateCompare(
    symbols: string[],
    question: string,
    barsLimit: number,
    profileIds: string[],
    execMode: "parallel" | "serial" = "parallel",
    mode?: string,
  ): void {
    studyGenerationRef.current += 1; // D02：批量任务接管结果区，作废在途的单股生成
    setReportError(null);
    const nameOf = (profileId: string): string => modelProfiles.find((profile) => profile.profile_id === profileId)?.name ?? profileId.slice(0, 8);
    // 任务跑在模块级：组件重挂载/视图切换不影响循环；快照订阅自动恢复显示。
    // B01（2026-09-15 方向研判路线图）：mode 随任务透传，批量与单份重试保持用户所选档位。
    startCompareJob(symbols, question, barsLimit, profileIds, execMode, nameOf, onGenerateStockReport, onRefreshReports, mode);
  }

  /** v23 多模型协同：某模型修订稿完成 → 追加进对比结果区（与多模型对比同一套 tab/综合/批量导出）。 */
  function appendCollabReport(report: StockReport): void {
    const base = compareResults ?? [];
    if (base.some((entry) => entry.report?.model === report.model && entry.report?.generated_at === report.generated_at)) return;
    updateCompareJob({ results: [...base, { symbol: report.symbol, name: report.model, profileId: null, report, error: null, pending: false }] });
  }

  // D02：在途方向研判请求的 abort 句柄（任务中心「停止」触发）。
  const directionAbortRef = useRef<AbortController | null>(null);

  function generateDirection(topic: string, question: string): void {
    setDirectionBusy(true);
    setDirectionError(null);
    // D02（桌面端升级路线图 2026-09-18）：方向研判登记进任务中心——单请求长任务，
    // 「停止」即经 C01 的 signal 中止在途 POST。
    const controller = new AbortController();
    directionAbortRef.current = controller;
    taskCenterUpsert({
      id: "direction-report",
      label: "方向研判生成",
      detail: topic,
      stop: () => controller.abort(),
      startedAt: new Date().toISOString(),
    });
    void onGenerateDirection(topic, question, null, undefined, controller.signal)
      .then((result) => {
        if (result) {
          setDirectionReport(result);
          onRefreshReports();
        } else {
          setDirectionError(controller.signal.aborted ? "方向研判已停止（可重试）。" : "方向研判生成失败：模型调用或解析未通过，请重试。");
        }
      })
      .catch(() => setDirectionError(controller.signal.aborted ? "方向研判已停止（可重试）。" : "方向研判请求失败，请重试。"))
      .finally(() => {
        taskCenterRemove("direction-report");
        directionAbortRef.current = null;
        setDirectionBusy(false);
      });
  }

  return (
    <section className="page-view workbench-page">
      {isDemo && <DemoNotice context="AI 研究产出" />}
      <header className="settings-heading">
        <div><span className="kicker">投资管家 · 研究工作台</span><h2>AI 研究工作台</h2></div>
        <span>先筛方向 · 再筛 K 线 · 最后深研个股</span>
      </header>

      {/* v33 A01：一级分区 = 研究报告 / 待验证 / 历史报告（互不穿透，切换保留状态）；
          §2 口径：取消占用大面积的三张流程卡，保留紧凑流程提示 + 前往战法雷达入口。 */}
      <div className="wb-sections" role="tablist" aria-label="工作台分区">
        <button role="tab" aria-selected={section === "reports"} className={`wb-section-tab ${section === "reports" ? "active" : ""}`} onClick={() => setSection("reports")}>
          研究报告
        </button>
        <button role="tab" aria-selected={section === "verify"} className={`wb-section-tab ${section === "verify" ? "active" : ""}`} onClick={() => setSection("verify")}>
          待验证
          {(reviewQueue?.item_count ?? 0) > 0 && <span className="wb-tab-badge blue">{reviewQueue!.item_count}</span>}
        </button>
        <button role="tab" aria-selected={section === "history"} className={`wb-section-tab ${section === "history" ? "active" : ""}`} onClick={() => setSection("history")}>
          历史报告
          {reports.length > 0 && <span className="wb-tab-badge blue">{reports.length}</span>}
        </button>
        <span className="wb-flowline" aria-label="研究流程提示">
          流程：方向研判 → 候选股送
          <button className="tag-button" onClick={() => onNavigate("tactics")}>战法雷达筛 K 线</button>
          → 个股研报 → 追问与验证
        </span>
      </div>

      {/* v33 A01：三个分区**常驻挂载**、按 display 切换——切走不卸载，输入/任务/筛选/阅读位置全部保留；
          运行中的批量/协同任务（模块级 compareJob + 页面状态）不因切换分区或隐藏视图被重置。 */}
      <div style={{ display: section === "reports" ? undefined : "none" }}>
      <StockReportPanel
        candidates={holdings}
        aiReady={aiReady}
        tab={tab}
        onTabChange={setTab}
        busy={reportBusy}
        compareResults={compareResults}
        activeCompare={activeCompare}
        onActiveCompareChange={(index) => updateCompareJob({ activeIndex: index })}
        compareBusy={compareBusy}
        selectedProfileIds={selectedProfileIds}
        onSetProfiles={setSelectedProfileIds}
        onGenerate={generateReport}
        onGenerateCompare={generateCompare}
        queue={queue}
        onQueueSet={setQueue}
        comparePending={comparePending}
        reportHandoff={reportHandoff}
        onNavigate={onNavigate}
        directionBusy={directionBusy}
        directionReport={directionReport}
        directionError={directionError}
        reportError={reportError}
        onGenerateDirection={generateDirection}
        onSendToTactics={onSendToTactics}
        modelProfiles={modelProfiles}
        onSynthesizeCompare={onSynthesizeCompare}
        onCreateCollabRun={onCreateCollabRun}
        onNextCollabStage={onNextCollabStage}
        onCollabFinished={appendCollabReport}
        onRefreshReports={onRefreshReports}
        onCreateFollowUp={onCreateFollowUp}
        onLoadFollowUps={onLoadFollowUps}
      />
      </div>

      {/* v33 A15：待验证改为操作列表（筛选/检索/分页/跳回来源报告），独立入口不穿过报告。
          G01：面板自带「回填结论」表单与「到期未回填」计数，回填走既有 verify 通道。 */}
      <div className="wb-section-body" style={{ display: section === "verify" ? undefined : "none" }}>
        <ReviewQueuePanel queue={reviewQueue} onOpenReport={openReportById} onRefill={onVerifyJudgment} />
      </div>

      {/* v22 历史产出：落库的研判/研报，重启不丢；点击回看，可删除。
          v31 §八.4：不完整报告以草稿形式留档，与正式报告分开计数，不混入正式报告数量。
          v33 A16：检索在前、独立入口、打开后定位阅读区，行内突出主题/标题，次要操作弱化。 */}
      <div className="wb-history" style={{ display: section === "history" ? undefined : "none" }}>
        <span className="answer-block-title">
          历史研究产出（{aiReportsTotal}/{facetCounts.total} 条 · 正式 {formalCount} · 不完整草稿 {draftCount} · 已持久化，重启不丢）
        </span>
        {reports.length > 0 && (
          <div className="pool-filter">
            <div className="pool-filter-row">
              <span>分类</span>
              <div className="pref-row" role="group" aria-label="历史产出分类">
                <button className={`tag-button ${historyKind === "all" ? "active" : ""}`} onClick={() => setHistoryKind("all")}>全部（{reports.length}）</button>
                <button className={`tag-button ${historyKind === "direction" ? "active" : ""}`} onClick={() => setHistoryKind("direction")}>方向研判（{dirCount}）</button>
                <button className={`tag-button ${historyKind === "stock" ? "active" : ""}`} onClick={() => setHistoryKind("stock")}>个股研报（{stockCount}）</button>
                <button className={`tag-button ${historyKind === "collab" ? "active" : ""}`} onClick={() => setHistoryKind("collab")}>协同流水线（{collabCount}）</button>
              </div>
            </div>
            <div className="pool-filter-row">
              <span>模型</span>
              <select
                className="picker-control"
                aria-label="历史产出模型筛选"
                value={historyModel}
                onChange={(event) => setHistoryModel(event.target.value)}
              >
                <option value="all">全部模型</option>
                {historyModels.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </div>
            <div className="pool-filter-row">
              <span>时间</span>
              <div className="pref-row" role="group" aria-label="历史产出时间范围">
                <button className={`tag-button ${historyRange === "all" ? "active" : ""}`} onClick={() => setHistoryRange("all")}>全部</button>
                <button className={`tag-button ${historyRange === "7" ? "active" : ""}`} onClick={() => setHistoryRange("7")}>近 7 天</button>
                <button className={`tag-button ${historyRange === "30" ? "active" : ""}`} onClick={() => setHistoryRange("30")}>近 30 天</button>
              </div>
            </div>
            {/* v33 A16：质量/草稿筛选 */}
            <div className="pool-filter-row">
              <span>质量</span>
              <div className="pref-row" role="group" aria-label="历史产出质量筛选">
                <button className={`tag-button ${historyQuality === "all" ? "active" : ""}`} onClick={() => setHistoryQuality("all")}>全部</button>
                <button className={`tag-button ${historyQuality === "formal" ? "active" : ""}`} onClick={() => setHistoryQuality("formal")}>仅正式</button>
                <button className={`tag-button ${historyQuality === "draft" ? "active" : ""}`} onClick={() => setHistoryQuality("draft")}>仅草稿（不完整）</button>
                <button className={`tag-button ${historyQuality === "shallow" ? "active" : ""}`} title="正文低于所选档位的字数下限（如深研版 < 3500 字）——J06 的「未达档位深度」一档" onClick={() => setHistoryQuality("shallow")}>未达档位深度</button>
              </div>
            </div>
            <div className="pool-filter-row">
              <span>关键字</span>
              <input
                className="picker-control"
                placeholder="按代码 / 标题 / 模型关键字过滤，便于二次审查"
                aria-label="历史产出关键字过滤"
                value={historyQuery}
                onChange={(event) => setHistoryQuery(event.target.value)}
              />
              {(historyKind !== "all" || historyQuery || historyModel !== "all" || historyRange !== "all") && (
                <button className="tag-button" onClick={() => { setHistoryKind("all"); setHistoryQuery(""); setHistoryModel("all"); setHistoryRange("all"); }}>清除筛选</button>
              )}
            </div>
          </div>
        )}
        {facetCounts.total === 0 ? (
          <p className="wb-history-empty">还没有保存的研究产出。生成方向研判或个股研报后会自动留档。</p>
        ) : pagedReports.length === 0 ? (
          <p className="wb-history-empty">没有符合筛选条件的产出，请调整分类或关键字。</p>
        ) : (
          <>
          <div className="wb-review-toolbar">
            <span className="report-meta">{aiReportsTotal} 条命中</span>
            <div className="wb-review-pager">
              <label className="report-meta" htmlFor="wb-history-pagesize">每页</label>
              <select
                id="wb-history-pagesize"
                value={historyPageSize}
                onChange={(event) => { setHistoryPageSize(Number(event.target.value) as 10 | 20); setHistoryPage(1); }}
              >
                <option value={10}>10 条</option>
                <option value={20}>20 条</option>
              </select>
              <button className="tag-button" disabled={historyPage <= 1} onClick={() => setHistoryPage(historyPage - 1)}>上一页</button>
              <span className="report-meta">第 {Math.min(historyPage, historyPageCount)} / {historyPageCount} 页</span>
              <button className="tag-button" disabled={historyPage >= historyPageCount} onClick={() => setHistoryPage(historyPage + 1)}>下一页</button>
            </div>
          </div>
          <ul className="wb-history-list">
            {pagedReports.map((item) => (
              <li key={item.report_id} className={item.kind === "direction" ? "is-direction" : "is-stock"}>
                <span className="wb-history-kind">{item.kind === "direction" ? "方向研判" : isCollabReport(item) ? "个股研报·协同" : "个股研报"}</span>
                {item.kind === "stock" && item.is_draft === true && (
                  <span className="soft-tag warn" title="生成不完整的草稿：保留原始输出与缺失项，不计入正式报告数量">不完整草稿</span>
                )}
                <button className="wb-history-open" title="点击回看（打开后定位到阅读区顶部）" onClick={() => void openHistoryItem(item)}>
                  <b>{item.kind === "direction" ? `方向研判 · ${item.subject}` : item.title || `${item.subject} 研究报告`}</b>
                  <span>{typeof item.model === "string" ? item.model : ""} · {new Date(item.generated_at).toLocaleString()}</span>
                </button>
                <button
                  className="wb-history-del"
                  title="删除这条产出"
                  onClick={() => onDeleteReport(item.report_id)}
                >
                  删除
                </button>
              </li>
            ))}
          </ul>
          </>
        )}
      </div>
    </section>
  );
}
