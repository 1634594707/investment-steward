import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ErrorBoundary } from "../components/ErrorBoundary";
import type { AgentResponse, ArtifactPoolView, CandleSeries, CredentialRecord, DecisionEntry, Evidence, Holding, InvestmentPolicyVersion, InvestorProfile, LearningActivity, LearningGoal, LearningUnit, ModelProfile, Notification, PersonalSettings, PersonalSettingsInput, Plan, PluginCapability, PluginCatalogEntry, ResearchRun, Thesis, TodayBrief, UpdateChannelView, WeeklyReview } from "@investment-steward/domain-contracts";
import { useMacro } from "../hooks/useMacro";
import { useTactics } from "../hooks/useTactics";
import { useResearch } from "../hooks/useResearch";
import { newRequestId, type HandoffSourceView, type ReportHandoff, type TacticsHandoff } from "../state/handoff";
import { useQuant } from "../hooks/useQuant";
import { useSteward } from "../hooks/useSteward";
import { useSystem } from "../hooks/useSystem";
import { usePlugins } from "../hooks/usePlugins";
import { requestLibraryReload } from "../state/libraryRefresh";
import { createCoreClient, classifyCoreError, detailOf, onCoreHealthEvent, type CoreClient, type CoreConnection } from "../state/coreClient";
import { subscribeTaskCenter, taskCenterSnapshot, type TaskCenterEntry } from "../state/taskCenter";
import { DEFAULT_VIEW, NAV_ALL, SETTINGS_WORKSPACE, WORKSPACES, workspaceDefaultView, workspaceOfView, type AppView, type WorkspaceDef } from "./nav";
import { loadDefaultView, saveDefaultView, saveProfile } from "./profile";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { TitleBar } from "./TitleBar";
import { StatusBar } from "./StatusBar";
import { ActivityBar } from "./ActivityBar";
import { EvidenceDrawer } from "./EvidenceDrawer";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { InvestorOnboarding, type InvestorOnboardingInput } from "../components/InvestorOnboarding";
import { CARD_ACTIONS, dispatchCardAction, needsConfirmation, type CardAction, type CardActionContext, type KernelHandlers } from "../state/actionDispatcher";
import { CommandPalette, type PaletteCommand } from "./CommandPalette";
import { ShortcutsHelp } from "./ShortcutsHelp";
import { ToastHost } from "../components/ToastHost";
import { toast } from "../state/toastStore";
import { SHORTCUTS_EVENT, viewForDigit, isTypingTarget, type NavReachability } from "../state/shortcuts";
import { applyUiPrefs, saveUiPrefs, useUiPrefs } from "./uiprefs";
import { applyWidthTier } from "../state/widthTier";
import { Skeleton, SkeletonLines } from "../components/Skeleton";
import { useNotifications } from "../hooks/useNotifications";
import { useCandles, type CandlePeriod } from "../hooks/useCandles";
import { isDocumentVisible } from "../state/useCoreQuery";
// M4-E01/E02：标的查找（代码 ⇄ 名称）。
import { lookupInstruments } from "../state/instrumentLookup";
const TodayPage = lazy(() => import("../routes/TodayPage").then((module) => ({ default: module.TodayPage })));
const InvestmentPage = lazy(() => import("../routes/InvestmentPage").then((module) => ({ default: module.InvestmentPage })));
const ResearchWorkbenchPage = lazy(() => import("../routes/ResearchWorkbenchPage").then((module) => ({ default: module.ResearchWorkbenchPage })));
const ResearchPage = lazy(() => import("../routes/ResearchPage").then((module) => ({ default: module.ResearchPage })));
const ReviewPage = lazy(() => import("../routes/ReviewPage").then((module) => ({ default: module.ReviewPage })));
const ExtensionsPage = lazy(() => import("../routes/ExtensionsPage").then((module) => ({ default: module.ExtensionsPage })));
const SettingsPage = lazy(() => import("../routes/SettingsPage").then((module) => ({ default: module.SettingsPage })));
const MacroPage = lazy(() => import("../routes/MacroPage").then((module) => ({ default: module.MacroPage })));
const TacticsPage = lazy(() => import("../routes/TacticsPage").then((module) => ({ default: module.TacticsPage })));
// B2：战法领域类型别名随状态一起下沉 hooks/useTactics，AppShell 不再直接引用。
const LibraryPage = lazy(() => import("../routes/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const QuantPage = lazy(() => import("../routes/QuantPage").then((module) => ({ default: module.QuantPage })));
// P3-D03：游资雷达（official.youzi-radar）。领域数据下沉 hооk/useYouzi，页面只消费。
const YouziPage = lazy(() => import("../routes/YouziPage").then((module) => ({ default: module.YouziPage })));

// K 线周期类型已下沉 hooks/useCandles（B1）；此处 re-export 供既有导入方（KLineCard/InvestmentPage）不动。
export type { CandlePeriod };

/** 路线图 B4 keep-alive 容器：激活时 display:contents（不改变布局），切走时 display:none 隐藏不卸载，
 *  并加 inert 防止焦点进入不可见页面。页内状态（筛选/表单/滚动）由此保留。 */
function KeepAlive({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <div style={{ display: active ? "contents" : "none" }} inert={!active}>
      {children}
    </div>
  );
}

export function AppShell() {
  const client = useMemo(() => createCoreClient(), []);  const [view, setView] = useState<AppView>(() => {
    // 优先级：个人中心「默认落地页」> 上次所在页面 > 系统默认。
    const preferred = loadDefaultView();
    if (preferred && NAV_ALL.some((entry) => entry.id === preferred)) return preferred as AppView;
    // H4-2:重启回到上次所在页面(仅接受合法视图值)
    const saved = localStorage.getItem("steward.last-view");
    return NAV_ALL.some((entry) => entry.id === saved) ? (saved as AppView) : DEFAULT_VIEW;
  });
  useEffect(() => {
    try {
      localStorage.setItem("steward.last-view", view);
    } catch {
      /* 存储不可用时仅本会话生效 */
    }
  }, [view]);
  // 路线图 B4（用户拍板「切页面缓存必须保留」）：keep-alive——首次访问后页面驻留不卸载，
  // 切走时隐藏（保留页内筛选、表单输入与滚动位置），再次切回即时还原。
  const [keptViews, setKeptViews] = useState<AppView[]>(() => [view]);
  useEffect(() => {
    setKeptViews((current) => (current.includes(view) ? current : [...current, view]));
  }, [view]);
  const scrollMemoryRef = useRef(new Map<AppView, number>());
  const keepAlivePrevViewRef = useRef<AppView>(view);
  useEffect(() => {
    if (keepAlivePrevViewRef.current === view) return;
    const scroller = document.getElementById("mainScroll");
    if (scroller) {
      scrollMemoryRef.current.set(keepAlivePrevViewRef.current, scroller.scrollTop);
      scroller.scrollTop = scrollMemoryRef.current.get(view) ?? 0;
    }
    keepAlivePrevViewRef.current = view;
  }, [view]);
  // "connecting"：Core 状态尚未探明的初始态，loadAll 完成后被 ready/demo/stopped 覆盖。
  const [connection, setConnection] = useState<CoreConnection>("connecting");
  // B2：研读图书馆域数据下沉 hooks/useLibrary（书架/研读计划/认知档案），LibraryPage 直接消费。
  // A01（前端设计与架构优化任务路线图 2026-09-19）：壳层不再持有第二份 useLibrary——书架状态只有页面那一份，
  // 插件安装后的重载经 state/libraryRefresh 送达唯一所有者。
  // B1：K 线领域数据下沉 hooks/useCandles（序列状态 + 缓存秒开 + 30s 刷新，窗口隐藏暂停）。
  const { candles, setCandles, candlesLoading, candleSymbol, candlePeriod, selectInstrument, changeCandlePeriod } = useCandles(client, view === "investment");
  // B2：系统域数据下沉 hooks/useSystem（模型方案/凭据/遥测/个人中心设置），loadAll 经同名 setter 回填。
  const {
    modelProfiles, setModelProfiles, credentials, setCredentials,
    slotUsage, setSlotUsage, notifyChannels, setNotifyChannels,
    channels, setChannels, capabilities, setCapabilities, capabilityCount, setCapabilityCount,
    dataAsOf, setDataAsOf, coreVersion, setCoreVersion, schemaVersion, setSchemaVersion,
    investorProfile, setInvestorProfile, personalSettings, setPersonalSettings,
    createModelProfile, updateModelProfile, activateModelProfile, testModelProfile,
    probeModelConnection, discoverModels, deleteModelProfile,
    upsertCredential, testCredential, deleteCredential, applyPersonalSettings, savePersonalSettings,
  } = useSystem(client);
  // B1：通知领域数据下沉 hooks/useNotifications（60s 刷新 + 已读/评估，窗口隐藏暂停）。
  const {
    notifications, setNotifications, triage: notificationTriage,
    loadNotificationTriage, markNotificationRead, evaluateNotifications,
  } = useNotifications(client);
  // M2-03：AI 前置就绪 = 存在「使用中」的模型方案。未就绪时 AI 入口禁用并引导到设置页，
  // 避免用户点击后才收到 model_unavailable 报错。
  const aiReady = modelProfiles.some((profile) => profile.status === "使用中");
  // B2：投资管家核心域下沉 hooks/useSteward（政策/证据/持仓/逻辑/计划/决定/简报/学习/研究运行），loadAll 经同名 setter 回填。
  const {
    policy, setPolicy, evidence, setEvidence, holdings, setHoldings,
    theses, setTheses, plans, setPlans, decisions, setDecisions,
    todayBrief, setTodayBrief, weeklyReview, setWeeklyReview,
    learningUnit, setLearningUnit, learningGoal, setLearningGoal, activities, setActivities,
    runs, setRuns, agentResponse, setAgentResponse,
    createPolicyDraft, confirmPolicy, saveThesis, createThesis,
    createHolding, deleteHolding, generateTodayBrief, patrolEvidenceFreshness,
    pullEvidence, dedupEvidence, submitLearningActivity, deleteLearningActivity,
    proposePolicyChange, exportReflections, createResearchRun, loadRunResponse,
    transitionRun, deleteResearchRun, createDecision, recordDecisionOutcome, transitionPlan,
  } = useSteward(client, aiReady);

  // M4-E01/E02：标的查找（代码 ⇄ 名称）稳定回调——投资页搜索框依赖其引用稳定，
  // 不用 useCallback 会在每次渲染生成新函数并触发防抖 effect 反复重跑。
  const lookupInstrument = useCallback(
    (query: string) => lookupInstruments(client, query),
    [client],
  );

  // B2：战法雷达领域数据下沉 hooks/useTactics（目录/扫描/观察/信号/笔记/板块/AI 复核），TacticsPage 直接消费。
  // createHolding 为函数声明（提升），此处可安全引用；自选入库与投资页共用同一份清单。
  const tactics = useTactics(client, view === "tactics", aiReady, createHolding);
  // B2：AI 研究产出领域数据下沉 hooks/useResearch（生成/协同/历史/复盘队列），ResearchWorkbenchPage 直接消费。
  const research = useResearch(client);
  // B2：量化研究域数据下沉 hooks/useQuant（分享池/因子/模型/策略包/实盘记录），QuantPage 直接消费；pool 由 loadAll 经 setQuantPool 回填。
  const quant = useQuant(client);
  // B2：插件生命周期域下沉 hooks/usePlugins（安装/停用/升级/撤销），跨域联动经 deps 参数化。
  const { plugins, setPlugins, pluginBusy, pluginMessage, changePlugin, updatePlugin, revokePlugin } = usePlugins(client, {
    selectInstrument,
    setCandles,
    getFirstHoldingInstrument: () => holdings[0]?.instrument,
    onMacroPluginInstalled: () => macroReloadRef.current(),
    onLibraryPluginInstalled: () => requestLibraryReload(),
  });
  // TopBar 头像点击 → 跳转设置页并定位「个人中心」分区（一次性请求，SettingsPage 消费后清空）。
  const [settingsStabRequest, setSettingsStabRequest] = useState<"personal" | null>(null);
  // v21 研究工作台联动；v33（2026-09-14 路线图 D03/D06）：交接升级为结构化契约——
  // 每次导航带唯一 requestId（同股重复跳转也有效）、来源报告/主题/研究问题（可返回来源），
  // 迟到的旧请求不覆盖新请求。池 → 雷达走 TacticsHandoff；雷达/方向 → 单股研究走 ReportHandoff。
  const [tacticsHandoff, setTacticsHandoff] = useState<TacticsHandoff | null>(null);
  const [reportHandoff, setReportHandoff] = useState<ReportHandoff | null>(null);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  // true = 从设置页打开的「编辑画像」弹窗（预填已保存值）；false = 首次引导。
  const [onboardingEditMode, setOnboardingEditMode] = useState(false);
  const [onboardingSaving, setOnboardingSaving] = useState(false);
  const [onboardingError, setOnboardingError] = useState<string | null>(null);
  const [loadErrors, setLoadErrors] = useState<string[]>([]);
  // 首次数据装载中：期间渲染全局加载态，不渲染各页「伪空态」（数据没到 ≠ 没有）。
  const [booting, setBooting] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [coreRestarting, setCoreRestarting] = useState(false);
  // C03（桌面端升级路线图 2026-09-18）：Core 连续超时计数（≥2 视为挂起）——挂起态下
  // 「重启本地 Core」不再只在 connection === "stopped" 时可见，而是常驻命令面板 + 状态栏可点提示。
  const [coreHung, setCoreHung] = useState(false);
  const coreTimeoutCountRef = useRef(0);
  // D02（桌面端升级路线图 2026-09-18）：进行中任务中心——StatusBar 展示可展开条目与停止按钮。
  const [taskEntries, setTaskEntries] = useState<TaskCenterEntry[]>([]);
  useEffect(() => subscribeTaskCenter(() => setTaskEntries(taskCenterSnapshot())), []);
  useEffect(() => {
    const off = onCoreHealthEvent((event) => {
      if (event.kind === "timeout") {
        coreTimeoutCountRef.current += 1;
        if (coreTimeoutCountRef.current >= 2) setCoreHung(true);
      } else {
        coreTimeoutCountRef.current = 0;
        setCoreHung(false);
      }
    });
    return off;
  }, []);
  // 内核动作通道反馈：被拒绝的动作（handled=false）与建计划结果以短提示透出，不再静默。
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [proMode, setProMode] = useState(false);
  // R1-3 v2 侧栏单形态：常驻展开（用户反馈「别有两个形式」）；仅汉堡手动收起时切换图标列。
  // 新存储键 v2：旧键在此前默认收窄，作废，避免老值把侧栏钉在收起态。
  const [railCollapsed, setRailCollapsed] = useState(() => localStorage.getItem("steward.sidebar-collapsed") === "1");
  useEffect(() => {
    try {
      localStorage.setItem("steward.sidebar-collapsed", railCollapsed ? "1" : "0");
    } catch {
      /* 存储不可用时仅本会话生效 */
    }
  }, [railCollapsed]);
  // F1 键盘优先：命令面板（Ctrl+K）与快捷键速查（? / 帮助菜单）。
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  // 窄窗口单列合并（迟滞防抖动）：<1024px 进入 narrow——ActivityBar 隐藏、侧栏强制图标列并把工作区切换并入侧栏顶部；≥1064px 恢复宽布局。
  const [narrow, setNarrow] = useState(() => window.innerWidth < 1024);
  useEffect(() => {
    applyWidthTier(window.innerWidth);
    const onResize = () => {
      // 迟滞：窄态在 <1064px 内保持（含 1024~1063 过渡带），宽态在 <1024 时才进入窄态，避免临界宽度来回抖动。
      setNarrow((prev) => (prev ? window.innerWidth < 1064 : window.innerWidth < 1024));
      // K06：宽度档单一来源，写到 body[data-width-tier] 供 CSS 按档响应（sm 上界=1024，与 narrow 对齐）。
      applyWidthTier(window.innerWidth);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // F1-2 全局快捷键层：唯一 keydown 入口，键位语义见 state/shortcuts.ts SHORTCUTS。
  const viewRef = useRef(view);
  viewRef.current = view;
  const candleSymbolRef = useRef(candleSymbol);
  candleSymbolRef.current = candleSymbol;
  const candlePeriodRef = useRef(candlePeriod);
  candlePeriodRef.current = candlePeriod;
  const changeCandlePeriodRef = useRef(changeCandlePeriod);
  changeCandlePeriodRef.current = changeCandlePeriod;
  // B03（桌面端升级路线图 2026-09-18）：数字键落点由导航派生，可达性（插件启用态 + 专业模式）随渲染更新，供 keydown 只读。
  const navReachabilityRef = useRef<NavReachability>({ enabledPluginIds: new Set<string>(), proMode: false });
  navReachabilityRef.current = {
    enabledPluginIds: new Set(plugins.filter((entry) => entry.installation?.state === "enabled").map((entry) => entry.manifest.plugin_id)),
    proMode,
  };

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setShortcutsOpen(false);
        setPaletteOpen((open) => !open);
        return;
      }
      if (event.key === "Escape") {
        // 逐层关闭（F1-2）：面板 → 速查 → 确认对话框 → 证据抽屉 → 引导。
        const layers = layersRef.current;
        if (layers.palette) { setPaletteOpen(false); event.preventDefault(); return; }
        if (layers.shortcuts) { setShortcutsOpen(false); event.preventDefault(); return; }
        if (layers.dialog) { setPendingAction(null); event.preventDefault(); return; }
        if (layers.drawer) { setOpenEvidenceId(null); event.preventDefault(); return; }
        if (layers.onboarding) { setOnboardingOpen(false); setOnboardingEditMode(false); event.preventDefault(); return; }
        return;
      }
      if (ctrl && /^[1-9]$/.test(event.key) && !isTypingTarget(event.target)) {
        const target = viewForDigit(Number(event.key), navReachabilityRef.current);
        if (target) {
          event.preventDefault();
          setView(target);
        }
        return;
      }
      if (event.key === "?" && !isTypingTarget(event.target)) {
        event.preventDefault();
        setPaletteOpen(false);
        setShortcutsOpen(true);
        return;
      }
      if (event.altKey && /^[1-3]$/.test(event.key) && !isTypingTarget(event.target) && viewRef.current === "investment" && candleSymbolRef.current) {
        event.preventDefault();
        const periods: CandlePeriod[] = ["day", "week", "month"];
        const next = periods[Number(event.key) - 1]!;
        if (next !== candlePeriodRef.current) changeCandlePeriodRef.current(next);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // Electron 帮助菜单 → preload → `steward:show-shortcuts`：打开速查。
  useEffect(() => {
    function onMenuShortcuts() {
      setPaletteOpen(false);
      setShortcutsOpen(true);
    }
    window.addEventListener(SHORTCUTS_EVENT, onMenuShortcuts);
    return () => window.removeEventListener(SHORTCUTS_EVENT, onMenuShortcuts);
  }, []);

  // F6-4 路由切换滚动位置保持：main 滚动容器按视图记忆 scrollTop。
  const scrollPosRef = useRef<Partial<Record<AppView, number>>>({});
  const prevViewRef = useRef<AppView>(view);
  useEffect(() => {
    const el = document.getElementById("mainScroll");
    if (!el || prevViewRef.current === view) return;
    scrollPosRef.current[prevViewRef.current] = el.scrollTop;
    el.scrollTop = scrollPosRef.current[view] ?? 0;
    prevViewRef.current = view;
  }, [view]);

  // F0 界面偏好（密度 / 涨跌语义色）落到 body dataset：useUiPrefs 订阅变更时已同步；
  // 这里兜底首帧挂载前 HTML 渲染周期（幂等）。
  const uiPrefs = useUiPrefs();
  useEffect(() => {
    applyUiPrefs(uiPrefs);
  }, [uiPrefs]);
  const [openEvidenceId, setOpenEvidenceId] = useState<string | null>(null);
  // B2：宏观雷达领域数据下沉 hooks/useMacro（快照缓存 + 12h TTL + 权重/信号/看板），MacroPage 直接消费；
  // 插件一键启用后的重拉经 macroReloadRef 触发。
  const macro = useMacro(client, view === "macro", aiReady);
  const macroReloadRef = useRef(macro.reload);
  macroReloadRef.current = macro.reload;
  // 内核动作通道：待确认动作（create_plan / review_policy）先经 ConfirmDialog，确认后才写内核对象。
  const [pendingAction, setPendingAction] = useState<CardAction | null>(null);
  const [pendingCardTitle, setPendingCardTitle] = useState("");
  // F1-2 逐层关闭用的弹层快照（keydown 处理器经 ref 读最新值，避免重挂监听）。
  const layersRef = useRef({ palette: false, shortcuts: false, dialog: false, drawer: false, onboarding: false });
  useEffect(() => {
    layersRef.current = { palette: paletteOpen, shortcuts: shortcutsOpen, dialog: Boolean(pendingAction), drawer: Boolean(openEvidenceId), onboarding: onboardingOpen };
  }, [paletteOpen, shortcutsOpen, pendingAction, openEvidenceId, onboardingOpen]);

  // F6-4 模态滚动锁：任一弹层打开时锁定背景滚动。
  useEffect(() => {
    const modalOpen = paletteOpen || shortcutsOpen || Boolean(pendingAction) || Boolean(openEvidenceId) || onboardingOpen;
    document.body.classList.toggle("modal-open", modalOpen);
    return () => document.body.classList.remove("modal-open");
  }, [paletteOpen, shortcutsOpen, pendingAction, openEvidenceId, onboardingOpen]);

  // 全量数据装载：首次启动与「刷新 / 重试」共用；initialLoad 时 K 线跟随首支持仓，
  // 手动刷新不重置用户当前正在查看的标的。
  // 演示模式已移除（2026-09-09）：无数据通道时不再发任何请求，由启动指引页接管。
  // C02（桌面端升级路线图 2026-09-18）：boot 拆层——首屏必需集（连接态/原则/证据/插件目录/持仓/
  // 通知/个人设置/画像/健康/插槽）先行装载并解除骨架屏；其余端点延后在后台继续，不阻塞可交互。
  const deferredRunningRef = useRef(false);
  const deferredQueuedRef = useRef(false);
  const phase1ErrorsRef = useRef<string[]>([]);

  function toLoadError(entry: [string, { status: number; data?: unknown }]): string {
    const response = entry[1];
    const detail = response.data && typeof response.data === "object" && "detail" in response.data
      ? String((response.data as { detail?: unknown }).detail ?? "")
      : "";
    return `${entry[0]}加载失败（HTTP ${response.status}${detail ? `：${detail}` : ""}）`;
  }

  async function loadAll(initialLoad: boolean) {
    if (!client.hosted) {
      setConnection("stopped");
      setLoadErrors([]);
      return;
    }
    const [coreStatus, policies, evidenceRes, catalog, holdingsRes, notificationsRes, personalSettingsRes, investorProfileRes, healthRes, slotsRes] = await Promise.all([
      client.status(),
      client.request<InvestmentPolicyVersion[]>({ method: "GET", path: "/investment-policies" }),
      client.request<Evidence[]>({ method: "GET", path: "/evidence" }),
      client.request<PluginCatalogEntry[]>({ method: "GET", path: "/plugins/catalog" }),
      client.request<Holding[]>({ method: "GET", path: "/holdings" }),
      client.request<Notification[]>({ method: "GET", path: "/notifications/pending" }),
      client.request<PersonalSettings | null>({ method: "GET", path: "/personal/settings" }),
      client.request<InvestorProfile | null>({ method: "GET", path: "/investor/profile" }),
      client.request<unknown>({ method: "GET", path: "/health" }),
      client.request<Array<{ slot: string; used: number; cap: number }>>({ method: "GET", path: "/slots" }),
    ]);
    setConnection(coreStatus);
    if (policies.status < 400) {
      const active = policies.data.find((item) => item.status === "active");
      if (active) setPolicy(active);
    }
    // A successful empty response is authoritative: clear prior evidence instead of
    // leaving stale records visible after a refresh or context change.
    if (evidenceRes.status < 400) setEvidence(evidenceRes.data);
    if (catalog.status < 400) setPlugins(catalog.data);
    if (holdingsRes.status < 400) setHoldings(holdingsRes.data);
    if (notificationsRes.status < 400) setNotifications(notificationsRes.data);
    // E3 首次引导：真实 Core 返回 null（画像尚未建立）时才弹出；演示模式恒返回已建画像，不打扰预览。
    if (investorProfileRes.status < 400) {
      if (investorProfileRes.data) setInvestorProfile(investorProfileRes.data);
      else if (!client.isDemo && initialLoad && Date.now() - Number(localStorage.getItem("steward.onboarding-deferred-at") || 0) > 7 * 24 * 3600 * 1000) setOnboardingOpen(true);
    }
    // 个人中心：Core 为权威来源，同时刷新本机镜像（离线首屏 / 启动落地页即时生效）。
    if (personalSettingsRes.status < 400 && personalSettingsRes.data) {
      applyPersonalSettings(personalSettingsRes.data);
      if (initialLoad) setView(personalSettingsRes.data.default_view as AppView);
    }
    if (healthRes.status < 400) {
      const health = healthRes.data as { core_version?: string; schema_version?: string; data_as_of?: Record<string, string | null> };
      setDataAsOf(health?.data_as_of ?? null);
      setCoreVersion(health?.core_version ?? "—");
      setSchemaVersion(health?.schema_version ?? "—");
    }
    // 插槽占用以 Core 仲裁为准（GET /slots）；失败时保持 null，StatusBar 回退本地估算。
    if (slotsRes.status < 400 && Array.isArray(slotsRes.data)) {
      setSlotUsage({
        used: slotsRes.data.reduce((sum, row) => sum + row.used, 0),
        cap: slotsRes.data.reduce((sum, row) => sum + row.cap, 0),
      });
    } else {
      setSlotUsage(null);
    }
    // K 线跟随首支持仓（无持仓时不硬编码默认标的）。
    if (initialLoad && holdingsRes.status < 400 && holdingsRes.data.length) {
      void selectInstrument(holdingsRes.data[0]!.instrument);
    }
    const phase1Errors = ([
      ["投资政策", policies],
      ["证据", evidenceRes],
      ["插件目录", catalog],
      ["持仓", holdingsRes],
      ["通知", notificationsRes],
      ["个人设置", personalSettingsRes],
      ["投资者画像", investorProfileRes],
      ["健康信息", healthRes],
    ] as Array<[string, { status: number; data?: unknown }]>)
      .filter(([, res]) => res.status >= 400)
      .map(toLoadError);
    phase1ErrorsRef.current = phase1Errors;
    setLoadErrors(phase1Errors);

    // C02：其余端点延后装载——骨架屏已随首屏必需集解除，这里不再阻塞可交互；
    // 手动刷新撞上在途的延后装载时排队一次，不并发重入。
    if (deferredRunningRef.current) {
      deferredQueuedRef.current = true;
      return;
    }
    deferredRunningRef.current = true;
    void (async () => {
      try {
        do {
          deferredQueuedRef.current = false;
          await loadDeferred();
        } while (deferredQueuedRef.current);
      } finally {
        deferredRunningRef.current = false;
      }
    })();
  }

  /** C02 延后层：研究/逻辑/计划/决定/简报/周复盘/学习/通道/能力/量化池/模型方案/凭据/通知通道。 */
  async function loadDeferred() {
    const [research, runsRes, thesisRes, plansRes, decisionsRes, briefRes, reviewRes, learningUnitRes, learningGoalRes, learningActivitiesRes, channelsRes, capabilitiesRes, quantRes, modelProfilesRes, credentialsRes, notifyChannelsRes] = await Promise.all([
      client.request<AgentResponse | null>({ method: "GET", path: "/research/questions/latest" }),
      client.request<ResearchRun[]>({ method: "GET", path: "/research/runs" }),
      client.request<Thesis[]>({ method: "GET", path: "/thesis" }),
      client.request<Plan[]>({ method: "GET", path: "/plans" }),
      client.request<DecisionEntry[]>({ method: "GET", path: "/decisions" }),
      client.request<TodayBrief>({ method: "GET", path: "/brief/today" }),
      client.request<WeeklyReview>({ method: "GET", path: "/review/weekly" }),
      client.request<LearningUnit | null>({ method: "GET", path: "/learning/unit/today" }),
      client.request<LearningGoal>({ method: "GET", path: "/learning/goals/current" }),
      client.request<LearningActivity[]>({ method: "GET", path: "/learning/activities" }),
      client.request<UpdateChannelView[]>({ method: "GET", path: "/channels" }),
      client.request<unknown[]>({ method: "GET", path: "/capabilities" }),
      client.request<ArtifactPoolView>({ method: "GET", path: "/quant/artifacts" }),
      client.request<ModelProfile[]>({ method: "GET", path: "/model-profiles" }),
      client.request<CredentialRecord[]>({ method: "GET", path: "/credentials" }),
      client.request<Array<{ channel: string; kind: string; label: string; configured: boolean }>>({ method: "GET", path: "/notifications/channels" }),
    ]);
    if (research.status < 400 && research.data) setAgentResponse(research.data);
    if (runsRes.status < 400) setRuns(runsRes.data);
    if (thesisRes.status < 400) setTheses(thesisRes.data);
    if (plansRes.status < 400) setPlans(plansRes.data);
    if (decisionsRes.status < 400) setDecisions(decisionsRes.data);
    if (briefRes.status < 400) setTodayBrief(briefRes.data);
    if (reviewRes.status < 400) setWeeklyReview(reviewRes.data);
    if (learningUnitRes.status < 400 && learningUnitRes.data) setLearningUnit(learningUnitRes.data);
    if (learningGoalRes.status < 400) setLearningGoal(learningGoalRes.data);
    if (learningActivitiesRes.status < 400) setActivities(learningActivitiesRes.data);
    if (channelsRes.status < 400) setChannels(channelsRes.data);
    if (capabilitiesRes.status < 400) {
      setCapabilities(capabilitiesRes.data as PluginCapability[]);
      setCapabilityCount(capabilitiesRes.data.length);
    }
    if (quantRes.status < 400 && quantRes.data) quant.setQuantPool(quantRes.data);
    if (modelProfilesRes.status < 400) setModelProfiles(modelProfilesRes.data);
    if (credentialsRes.status < 400) setCredentials(credentialsRes.data);
    if (notifyChannelsRes.status < 400) setNotifyChannels(notifyChannelsRes.data);
    // B2：宏观权重版本链预填已下沉 hooks/useMacro（seedWeights）。
    await macro.seedWeights();
    const deferredErrors = ([
      ["最新回答", research],
      ["研究列表", runsRes],
      ["投资逻辑", thesisRes],
      ["计划", plansRes],
      ["决定", decisionsRes],
      ["今日简报", briefRes],
      ["周复盘", reviewRes],
      ["今日学习", learningUnitRes],
      ["学习目标", learningGoalRes],
      ["学习活动", learningActivitiesRes],
      ["更新通道", channelsRes],
      ["能力清单", capabilitiesRes],
      ["量化分享池", quantRes],
      ["模型方案", modelProfilesRes],
      ["凭据", credentialsRes],
      ["通知通道", notifyChannelsRes],
    ] as Array<[string, { status: number; data?: unknown }]>)
      .filter(([, res]) => res.status >= 400)
      .map(toLoadError);
    setLoadErrors([...phase1ErrorsRef.current, ...deferredErrors]);
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await loadAll(true);
      if (!cancelled) setBooting(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  async function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    await loadAll(false);
    setRefreshing(false);
  }

  async function handleRestartCore() {
    const bridge = window.steward;
    if (!bridge || coreRestarting) return;
    setCoreRestarting(true);
    try {
      await bridge.restartCore();
      // Core 重启需要时间：轮询直至 ready（最多约 20 秒）再重拉数据。
      for (let attempt = 0; attempt < 20; attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        if ((await client.status()) === "ready") break;
      }
    } finally {
      setCoreRestarting(false);
      coreTimeoutCountRef.current = 0;
      setCoreHung(false);
    }
    await loadAll(false);
  }

  /** 桥接模式专属：Core 掉线时可从错误横幅就地重启 Core（宿主桥已暴露 restartCore）。 */
  // —— H1-2 全量数据导出（JSON 下载,数据不出本机） ——
  async function exportAllData(): Promise<{ exported_at: string } | null> {
    const response = await client.request<{ exported_at: string }>({ method: "GET", path: "/export/all" });
    if (response.status >= 400) return null;
    const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `steward-export-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
    return response.data;
  }

  /** 批注转研究问题：POST /library/annotations/{annotation_id}/to-research。 */
  async function annotationToResearch(annotationId: string, question: string): Promise<ResearchRun | null> {
    const response = await client.request<ResearchRun>({ method: "POST", path: `/library/annotations/${encodeURIComponent(annotationId)}/to-research`, body: { user_question: question } });
    if (response.status >= 400 || !response.data) return null;
    setRuns((current) => [response.data, ...current]);
    return response.data;
  }




  // —— E3 投资者画像：首次引导提交 ——
  async function saveInvestorProfile(input: InvestorOnboardingInput): Promise<void> {
    setOnboardingSaving(true);
    setOnboardingError(null);
    const response = await client.request<InvestorProfile>({ method: "PUT", path: "/investor/profile", body: input });
    setOnboardingSaving(false);
    if (response.status >= 400) {
      const detail = (response.data as { detail?: string } | null)?.detail;
      setOnboardingError(detail && detail !== "null" ? detail : `画像保存失败（HTTP ${response.status}）`);
      return;
    }
    setInvestorProfile(response.data);
    setOnboardingOpen(false);
    setOnboardingEditMode(false);
  }

  /** v33 D02/D03/D04：候选池 → 战法雷达的结构化交接。
   * 每次发送生成新 requestId（接收方按 id 消费并回执，重复发送/迟到不串）。 */
  function sendToTactics(
    pool: string[],
    context?: { sourceLabel?: string; sourceReportId?: string; topic?: string; question?: string; excluded?: { symbol: string; reason: string }[] },
  ): void {
    if (pool.length === 0) return;
    setTacticsHandoff({
      requestId: newRequestId(),
      sourceLabel: context?.sourceLabel || "研究工作台",
      sourceReportId: context?.sourceReportId || "",
      topic: context?.topic || "",
      question: context?.question || "",
      pool,
      excluded: context?.excluded ?? [],
      createdAt: new Date().toISOString(),
    });
    setView("tactics");
  }

  /** v33 D06/D08：雷达/方向 → 单股研究的结构化交接（每次新 requestId，同股重复跳转有效）。 */
  function openStockReport(symbol: string, context?: { question?: string; sourceLabel?: string; scanId?: string }): void {
    // D02（前端设计与架构优化任务路线图 2026-09-19）：交接记下发起视图，工作台据此给出「返回来源」。
    // 只有战法雷达与游资雷达的交接给出返回落点（同视图内的方向→单股是分区切换，不需要跨页返回）。
    const sourceView: HandoffSourceView | undefined =
      view === "tactics" || view === "youzi" ? view : undefined;
    setReportHandoff({
      requestId: newRequestId(),
      symbol,
      question: context?.question || "",
      sourceLabel: context?.sourceLabel || "战法雷达",
      scanId: context?.scanId,
      sourceView,
      createdAt: new Date().toISOString(),
    });
    setView("workbench");
  }

  // —— 内核动作通道（阶段5.1）：插件只声明 supported_actions，动作由内核解释执行。 ——
  const kernelHandlers: KernelHandlers = {
    openEvidence: (evidenceId) => setOpenEvidenceId(evidenceId),
    createPlan: async ({ title }) => {
      const body = { title, status: "active" as Plan["status"] };
      const response = await client.request<Plan>({ method: "POST", path: "/plans", body });
      if (response.status < 400) {
        setPlans((current) => [...current, response.data]);
        setActionNotice(`已创建计划「${title}」，可在复盘页查看。`);
        toast.ok(`已创建计划「${title}」，可在复盘页查看。`);
      } else {
        setActionNotice(`计划创建失败（HTTP ${response.status}），请稍后重试。`);
        toast.err(`计划创建失败（HTTP ${response.status}），请稍后重试。`);
      }
    },
    startLearning: () => setView("today"),
    reviewPolicy: () => setView("investment"),
  };

  function executeCardAction(action: CardAction) {
    // 研究页动作轨没有具体卡片，用当前回答构造一个最小卡片上下文交给内核仲裁。
    const card: CardActionContext = {
      card_id: agentResponse?.run_id ?? "research",
      title: agentResponse?.user_question ?? "研究问题",
      evidence_refs: agentResponse?.evidence_refs ?? [],
      action_mode: agentResponse?.action_mode ?? "research",
      supported_actions: agentResponse?.supported_actions ?? [],
    };
    const result = dispatchCardAction(card, action, kernelHandlers, evidence);
    // 内核仲裁拒绝（插件未声明该动作 / 无证据可展开）时给出可见反馈，不再静默。
    if (!result.handled) {
      setActionNotice(`动作未执行：${result.reason ?? "内核拒绝"}。`);
    }
    // 审计写入权保留在 Core（D-1 方案 b）：前端只读 `GET /audit`，不直接 POST，
    // 写内核对象的动作（建计划/复核原则）由各自服务端端点自带 `_audit` 落账。
    void result.audit;
  }

  function handleCardAction(action: CardAction) {
    if (needsConfirmation(action)) {
      setPendingCardTitle(agentResponse?.user_question ?? "研究问题");
      setPendingAction(action);
      return;
    }
    executeCardAction(action);
  }

  function onConfirmPendingAction(confirmText: string) {
    const action = pendingAction;
    setPendingAction(null);
    if (!action) return;
    executeCardAction(action);
    // D-1 方案 b：前端不写审计；`ui.confirm` 由服务端对应写入端点（_audit）落账。
    void confirmText;
  }

  const activeNav = NAV_ALL.find((item) => item.id === view) ?? NAV_ALL[0]!;
  // 数据新鲜度由证据/行情数据本身推导，不做硬编码。
  const latestAsOf = candles?.as_of ?? (evidence.length ? evidence[0]!.observed_at ?? evidence[0]!.collected_at : null);
  // 行情卡三态判定的前提：中国市场行情插件是否处于启用态（未启用≠获取失败）。
  const marketPluginEnabled = plugins.some((entry) => entry.manifest.plugin_id === "official.cn-market-data" && entry.installation?.state === "enabled");
  // 宏观雷达插件启用态：未启用时 /evidence/macro/* 全部 409（宏观页顶部给一键启用）。
  const macroPluginEnabled = plugins.some((entry) => entry.manifest.plugin_id === "official.macro-radar" && entry.installation?.state === "enabled");
  const tacticsPluginEnabled = plugins.some((entry) => entry.manifest.plugin_id === "official.stock-tactics" && entry.installation?.state === "enabled");
  // P3-D03：游资雷达的启用态（未启用时 /youzi/* 全 409，页面显示「启用」引导）。
  const youziPluginEnabled = plugins.some((entry) => entry.manifest.plugin_id === "official.youzi-radar" && entry.installation?.state === "enabled");
  // 桥接模式（Electron Host）下 Core 掉线才可就地重启；纯浏览器模式无此能力。
  const canRestartCore = !!window.steward && (connection === "stopped" || coreHung);

  // F1-1 命令面板数据源：静态导航/动作 + 动态项（持仓 / 研究运行 / 插件）。
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    const navCommands: PaletteCommand[] = NAV_ALL.map((entry) => ({
      id: `nav-${entry.id}`,
      label: `前往：${entry.label}`,
      hint: entry.hint,
      group: "导航",
      keywords: entry.id,
      run: () => setView(entry.id),
    }));
    const actionCommands: PaletteCommand[] = [
      { id: "act-refresh", label: "刷新全部数据", group: "动作", run: () => void handleRefresh() },
      { id: "act-pro", label: proMode ? "关闭专业模式" : "开启专业模式", group: "动作", run: () => setProMode((current) => !current) },
      { id: "act-rail", label: railCollapsed ? "展开侧栏" : "收起侧栏", group: "动作", run: () => setRailCollapsed((current) => !current) },
      { id: "act-shortcuts", label: "快捷键速查", group: "动作", run: () => setShortcutsOpen(true) },
      {
        id: "act-density",
        label: uiPrefs.density === "compact" ? "切换为舒适密度" : "切换为紧凑密度",
        group: "动作",
        run: () => saveUiPrefs({ ...uiPrefs, density: uiPrefs.density === "compact" ? "comfortable" : "compact" }),
      },
      {
        id: "act-mkt",
        label: uiPrefs.marketColors === "cn" ? "涨跌色切换为 mint / amber" : "涨跌色切换为红涨绿跌",
        group: "动作",
        run: () => saveUiPrefs({ ...uiPrefs, marketColors: uiPrefs.marketColors === "cn" ? "legacy" : "cn" }),
      },
    ];
    if (canRestartCore) {
      actionCommands.push({ id: "act-restart-core", label: "重启本地 Core", group: "动作", run: () => void handleRestartCore() });
    }
    const holdingCommands: PaletteCommand[] = holdings.map((item) => ({
      id: `holding-${item.holding_id}`,
      label: `切到持仓：${item.label}`,
      hint: item.instrument,
      group: "持仓",
      keywords: "行情 K线 market",
      run: () => {
        setView("investment");
        void selectInstrument(item.instrument);
      },
    }));
    const runCommands: PaletteCommand[] = runs.slice(0, 20).map((item) => ({
      id: `run-${item.run_id}`,
      label: `研究：${item.user_question}`,
      hint: item.status,
      group: "研究",
      run: () => {
        setView("research");
        void loadRunResponse(item.run_id);
      },
    }));
    const pluginCommands: PaletteCommand[] = plugins.map((entry) => ({
      id: `plugin-${entry.manifest.plugin_id}`,
      label: `插件：${entry.manifest.display_name}`,
      hint: entry.installation ? `已安装 v${entry.installation.release_version}` : "可安装",
      group: "插件",
      keywords: entry.manifest.plugin_id,
      run: () => setView("extensions"),
    }));
    return [...navCommands, ...actionCommands, ...holdingCommands, ...runCommands, ...pluginCommands];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [holdings, runs, plugins, proMode, railCollapsed, uiPrefs, canRestartCore, refreshing]);

  // 收窄态对齐设计稿：body.rail-collapsed 驱动侧栏 64px 图标列（窄窗口下工作区切换并入侧栏顶部，单列导航）。
  const [railOverlayOpen, setRailOverlayOpen] = useState(false);
  const effectiveRailCollapsed = narrow ? !railOverlayOpen : railCollapsed;
  useEffect(() => { setRailOverlayOpen(false); }, [view, narrow]);
  useEffect(() => {
    if (!railOverlayOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setRailOverlayOpen(false);
        document.querySelector<HTMLButtonElement>(".tb-btn.hamb")?.focus();
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [railOverlayOpen]);
  useEffect(() => {
    document.body.classList.toggle("rail-collapsed", effectiveRailCollapsed);
    return () => document.body.classList.remove("rail-collapsed");
  }, [effectiveRailCollapsed]);

  const activeWorkspaceId = workspaceOfView(view);
  const activeWorkspace: WorkspaceDef =
    [...WORKSPACES, SETTINGS_WORKSPACE].find((ws) => ws.id === activeWorkspaceId) ?? WORKSPACES[0]!;

  return (
    <div className={`app-frame ${narrow && railOverlayOpen ? "rail-overlay-open" : ""}`}>
      <TitleBar connection={connection} coreVersion={coreVersion} context={`${activeWorkspace.label} / ${activeNav.label}`} railCollapsed={effectiveRailCollapsed} onToggleRail={() => narrow ? setRailOverlayOpen((open) => !open) : setRailCollapsed((c) => !c)} />
      {narrow && railOverlayOpen && <button className="rail-backdrop" aria-label="收起导航" onClick={() => setRailOverlayOpen(false)} />}
      {/* v6 修复：ActivityBar 挂载条件改为「侧栏未收起」——此前只看 narrow，用户点汉堡收起侧栏时
          body.rail-collapsed 把 --shell-activity-w 压成 0，ActivityBar 列宽归零但组件仍挂载，
          整列图标溢出叠在侧栏 ws-mini 上（用户三次反馈的重叠破图真根因）。
          收起后工作区切换由侧栏顶部 ws-mini 承担，任意窗宽都是单列。 */}
      {!effectiveRailCollapsed && <ActivityBar active={activeWorkspaceId} onSelect={(ws) => { setView(workspaceDefaultView(ws.id)); setRailOverlayOpen(false); }} plugins={plugins} />}
      <Sidebar
        workspace={activeWorkspace}
        view={view}
        onNavigate={(next) => { setView(next); setRailOverlayOpen(false); }}
        connection={connection}
        policy={policy}
        proMode={proMode}
        onToggleProMode={() => setProMode((current) => !current)}
        activeWorkspaceId={activeWorkspaceId}
        onWorkspaceSelect={(ws) => { setView(workspaceDefaultView(ws.id)); setRailOverlayOpen(false); }}
        plugins={plugins}
      />
      <main className="main" id="mainScroll" inert={narrow && railOverlayOpen}>
        <TopBar entry={activeNav} isDemo={client.isDemo} onOpenPalette={() => setPaletteOpen(true)} onRefresh={handleRefresh} refreshing={refreshing} notifications={notifications} onMarkNotificationRead={markNotificationRead} onOpenToday={() => setView("today")} notificationTriage={notificationTriage} onLoadNotificationTriage={loadNotificationTriage} personalSettings={personalSettings} onOpenPersonalCenter={() => { setSettingsStabRequest("personal"); setView("settings"); }} />
        <div className="page-stack-wrap">
          {booting ? (
            <div className="booting-pane" role="status" aria-live="polite">
              <span className="booting-spinner" aria-hidden="true" />
              <strong>正在从本地 Core 装载数据…</strong>
              <span>持仓 / 证据 / 研究记录 / 学习进度将随后就绪</span>
              <div className="booting-skeletons" aria-hidden="true">
                <SkeletonLines lines={3} />
                <Skeleton kind="card" />
              </div>
            </div>
          ) : !client.hosted ? (
            <div className="unhosted-pane" role="note" aria-label="启动指引">
              <h2>未检测到数据通道</h2>
              <p>浏览器直接打开已不再提供演示数据（2026-09-09 起，应用只连真实本地内核）。</p>
              <ul>
                <li>日常使用：启动桌面端 <strong>Investment Steward.exe</strong>（dist-desktop/win-unpacked），它会自动拉起本地 Core。</li>
                <li>开发联调：复制 <code>apps/web-shell/.env.example</code> 为 <code>.env.local</code>，配置 <code>VITE_CORE_BASE</code> 与 <code>VITE_CORE_TOKEN</code> 后运行 <code>pnpm dev:web</code>。</li>
              </ul>
              <p className="unhosted-hint">数据只存本机：Core 只监听 loopback，凭据走系统凭据管理器。</p>
            </div>
          ) : (
            <>
              {connection === "stopped" && (
                <div className="connection-bar" role="alert">
                  <span>本地 Core 未连接：新数据不可写入，历史记录待连接后可读。</span>
                  <span className="conn-detail">Core 只监听 loopback；若刚启动请稍候，或检查本地进程。</span>
                  <span className="conn-actions">
                    <button className="text-button" onClick={handleRefresh} disabled={refreshing}>
                      {refreshing ? "重连中…" : "重连"}
                    </button>
                    {canRestartCore && (
                      <button className="text-button" onClick={handleRestartCore} disabled={coreRestarting}>
                        {coreRestarting ? "Core 重启中…" : "重启本地 Core"}
                      </button>
                    )}
                  </span>
                </div>
              )}
              {loadErrors.length > 0 && (
                <div className="load-error-banner" role="alert">
                  <span>部分数据加载失败（Host 桥或 Core 未就绪）</span>
                  <span className="load-error-list">{loadErrors.join(" · ")}</span>
                  <span className="load-error-actions">
                    <button className="text-button" onClick={handleRefresh} disabled={refreshing}>
                      {refreshing ? "刷新中…" : "重试加载"}
                    </button>
                    {canRestartCore && (
                      <button className="text-button" onClick={handleRestartCore} disabled={coreRestarting}>
                        {coreRestarting ? "Core 重启中…" : "重启本地 Core"}
                      </button>
                    )}
                  </span>
                </div>
              )}
              <Suspense fallback={<div className="booting-pane" role="status"><strong>正在加载页面…</strong><SkeletonLines lines={3} /></div>}>
              {keptViews.includes("today") && <KeepAlive active={view === "today"}><ErrorBoundary><TodayPage counts={{ evidence: evidence.filter((item) => { const age = Date.now() - new Date(item.observed_at ?? item.collected_at).getTime(); return age >= 0 && age <= 24 * 60 * 60 * 1000; }).length, open: notifications.length, learning: learningGoal?.completed_count ?? 0 }} evidence={evidence} brief={todayBrief} notifications={notifications} learningUnit={learningUnit} learningGoal={learningGoal} onNavigate={setView} onOpenEvidence={setOpenEvidenceId} onMarkNotificationRead={markNotificationRead} onEvaluateNotifications={evaluateNotifications} onSubmitLearning={submitLearningActivity} onGenerateBrief={generateTodayBrief} onboarding={{ hasPolicy: Boolean(policy), holdings: holdings.length, research: runs.length }} isDemo={client.isDemo} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("investment") && <KeepAlive active={view === "investment"}><ErrorBoundary><InvestmentPage policy={policy} evidence={evidence} holdings={holdings} theses={theses} candles={candles} candlesLoading={candlesLoading} candlePeriod={candlePeriod} marketPluginEnabled={marketPluginEnabled} onNavigate={setView} onOpenEvidence={setOpenEvidenceId} onSaveThesis={saveThesis} onCreateHolding={createHolding} onDeleteHolding={deleteHolding} onLookupInstrument={lookupInstrument} onCreateThesis={createThesis} onCreatePolicyDraft={createPolicyDraft} onConfirmPolicy={confirmPolicy} onSelectInstrument={selectInstrument} onSelectCandlePeriod={changeCandlePeriod} onPullEvidence={pullEvidence} onDedupEvidence={dedupEvidence} isDemo={client.isDemo} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("workbench") && <KeepAlive active={view === "workbench"}><ErrorBoundary><ResearchWorkbenchPage holdings={holdings.map((item) => ({ instrument: item.instrument, label: item.label }))} aiReady={aiReady} isDemo={client.isDemo} modelProfiles={modelProfiles} active={view === "workbench"} onCreateHolding={createHolding} onSendToTactics={sendToTactics} onNavigate={setView} reportHandoff={reportHandoff} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("research") && <KeepAlive active={view === "research"}><ErrorBoundary><ResearchPage evidence={evidence} response={agentResponse} runs={runs} onNavigate={setView} onOpenEvidence={setOpenEvidenceId} onCardAction={handleCardAction} onCreateRun={createResearchRun} onSelectRun={loadRunResponse} onTransitionRun={transitionRun} onDeleteRun={deleteResearchRun} onPatrolFreshness={patrolEvidenceFreshness} aiReady={aiReady} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("review") && <KeepAlive active={view === "review"}><ErrorBoundary><ReviewPage decisions={decisions} plans={plans} activities={activities} review={weeklyReview} evidence={evidence} onOpenEvidence={setOpenEvidenceId} onNavigate={setView} onAddDecision={createDecision} onRecordOutcome={recordDecisionOutcome} onTransitionPlan={transitionPlan} onDeleteActivity={deleteLearningActivity} onExportReflections={exportReflections} onProposePolicyChange={proposePolicyChange} onConfirmPolicy={confirmPolicy} coreRequest={client.request} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("extensions") && <KeepAlive active={view === "extensions"}><ErrorBoundary><ExtensionsPage catalog={plugins} channels={channels} client={client} capabilityCount={capabilityCount} busy={pluginBusy} message={pluginMessage} isDemo={client.isDemo} onChange={changePlugin} onUpdate={updatePlugin} onRevoke={revokePlugin} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("settings") && <KeepAlive active={view === "settings"}><ErrorBoundary><SettingsPage plugins={plugins} capabilities={capabilities} capabilityCount={capabilityCount} modelProfiles={modelProfiles} credentials={credentials} notifyChannels={notifyChannels} coreVersion={coreVersion} schemaVersion={schemaVersion} connection={connection} investorProfile={investorProfile} onNavigate={setView} onEditProfile={() => { setOnboardingEditMode(true); setOnboardingError(null); setOnboardingOpen(true); }} personalSettings={personalSettings} onSavePersonalSettings={savePersonalSettings} stabRequest={settingsStabRequest} onStabConsumed={() => setSettingsStabRequest(null)} onCreateModelProfile={createModelProfile} onUpdateModelProfile={updateModelProfile} onActivateModelProfile={activateModelProfile} onTestModelProfile={testModelProfile} onProbeModelConnection={probeModelConnection} onDiscoverModels={discoverModels} onDeleteModelProfile={deleteModelProfile} onExportAll={exportAllData} onUpsertCredential={upsertCredential} onTestCredential={testCredential} onDeleteCredential={deleteCredential} coreRequest={client.request} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("tactics") && <KeepAlive active={view === "tactics"}><ErrorBoundary><TacticsPage isDemo={client.isDemo} pluginEnabled={tacticsPluginEnabled} onEnablePlugin={() => void changePlugin("official.stock-tactics", "install")} active={view === "tactics"} onCreateHolding={createHolding} tacticsHandoff={tacticsHandoff} onOpenStockReport={openStockReport} trackedInstruments={holdings.map((item) => item.instrument)} aiReady={aiReady} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("macro") && <KeepAlive active={view === "macro"}><ErrorBoundary><MacroPage onNavigate={setView} isDemo={client.isDemo} macroPluginEnabled={macroPluginEnabled} onEnablePlugin={() => void changePlugin("official.macro-radar", "install")} active={view === "macro"} aiReady={aiReady} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("library") && <KeepAlive active={view === "library"}><ErrorBoundary><LibraryPage onNavigate={setView} isDemo={client.isDemo} disconnected={connection === "stopped"} connection={connection} active={view === "library"} onAnnotationToResearch={annotationToResearch} aiReady={aiReady} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("quant") && <KeepAlive active={view === "quant"}><ErrorBoundary><QuantPage isDemo={client.isDemo} marketPluginEnabled={marketPluginEnabled} active={view === "quant"} onNavigate={setView} /></ErrorBoundary></KeepAlive>}
              {keptViews.includes("youzi") && <KeepAlive active={view === "youzi"}><ErrorBoundary><YouziPage isDemo={client.isDemo} pluginEnabled={youziPluginEnabled} onEnablePlugin={() => void changePlugin("official.youzi-radar", "install")} active={view === "youzi"} onOpenReport={(symbol) => openStockReport(symbol, { sourceLabel: "游资雷达" })} /></ErrorBoundary></KeepAlive>}
              {proMode && view === "investment" && <div className="pro-notice">专业模式已开启：因子 / 回测 / 策略实验 / 纸面模拟接入中。</div>}
                </Suspense>
            </>
          )}
        </div>
      </main>
      <StatusBar connection={connection} plugins={plugins} dataAsOf={dataAsOf ?? undefined} asOf={latestAsOf} isDemo={client.isDemo} coreVersion={coreVersion} schemaVersion={schemaVersion} slotUsage={slotUsage ?? undefined} coreHung={coreHung} onRestartCore={() => void handleRestartCore()} tasks={taskEntries} />
      <EvidenceDrawer items={evidence} evidenceId={openEvidenceId} onClose={() => setOpenEvidenceId(null)} />
      {pendingAction && (
        <ConfirmDialog
          title={pendingAction === "create_plan" ? "创建一份计划" : "进入投资原则复核"}
          summary={`动作：${pendingAction.replace("_", " ")}。这一步会写入内核对象，确认说明将进入审计记录。`}
          diffs={[
            {
              label: "涉及回答",
              before: "未创建计划",
              after: pendingCardTitle || "本次研究回答",
            },
          ]}
          confirmLabel={pendingAction === "create_plan" ? "确认创建" : "确认复核"}
          onConfirm={onConfirmPendingAction}
          onCancel={() => setPendingAction(null)}
        />
      )}
      {actionNotice && (
        <div className="action-notice" role="status" aria-live="polite">
          {actionNotice}
        </div>
      )}
      {onboardingOpen && (
        <InvestorOnboarding
          saving={onboardingSaving}
          error={onboardingError}
          initial={onboardingEditMode ? investorProfile : null}
          onSubmit={saveInvestorProfile}
          onDismiss={() => {
            setOnboardingError(null);
            setOnboardingOpen(false);
            setOnboardingEditMode(false);
            // H4-1:稍后再说 → 7 天内不再弹
            try {
              localStorage.setItem("steward.onboarding-deferred-at", String(Date.now()));
            } catch {
              /* 存储不可用时仅本会话生效 */
            }
          }}
        />
      )}
      <CommandPalette open={paletteOpen} commands={paletteCommands} onClose={() => setPaletteOpen(false)} />
      <ShortcutsHelp open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />
      <ToastHost />
    </div>
  );
}
