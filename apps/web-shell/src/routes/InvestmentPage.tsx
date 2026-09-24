import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Pencil, Trash2 } from "lucide-react";
import type { CandleSeries, Evidence, Holding, InvestmentPolicyVersion, Thesis } from "@investment-steward/domain-contracts";
import { SectionHeading } from "../components/kit";
import { CardSlot } from "../cards/CardSlot";
import { KLineCard } from "../cards/KLineCard";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EvidenceRow } from "../components/EvidenceRow";
import { formatDate } from "../state/format";
import { useFocusTrap } from "../components/useFocusTrap";
import { DemoNotice } from "../components/DemoNotice";
import "./investment.css";
import type { AppView } from "../shell/nav";
import type { CandlePeriod } from "../shell/AppShell";
// M4-E01/E02：标的查找结果类型与展示映射（单框搜索 + 下拉）。
import {
  INSTRUMENT_KIND_LABEL,
  INSTRUMENT_MARKET_LABEL,
  type InstrumentLookupOutcome,
  type InstrumentLookupHit,
} from "../state/instrumentLookup";

type EvidenceKind = "announcements" | "news" | "financials";

/** R2-1 标的工作区 tab：行情 / 投资逻辑 / 证据（计划在复盘页，不在此重复）。 */
type HoldingTab = "overview" | "market" | "thesis" | "evidence";

const HOLDING_TABS: { id: HoldingTab; label: string }[] = [
  { id: "overview", label: "概览" },
  { id: "market", label: "行情" },
  { id: "thesis", label: "论点" },
  { id: "evidence", label: "证据" },
];

const EVIDENCE_KINDS: { kind: EvidenceKind; label: string }[] = [
  { kind: "announcements", label: "公告" },
  { kind: "news", label: "新闻" },
  { kind: "financials", label: "财报" },
];

/** v23 证据分类：按 evidence_type 分组展示（新闻历史上以 analysis 类型入账，标签如实并排）。 */
const EVIDENCE_GROUPS: { key: string; label: string; types: Evidence["evidence_type"][] }[] = [
  { key: "announcements", label: "公告", types: ["announcement"] },
  { key: "financials", label: "财报", types: ["financial"] },
  { key: "news", label: "新闻/分析", types: ["analysis"] },
  { key: "others", label: "行情/宏观/其他", types: ["quote", "macro", "learning"] },
];

/** 证据所属标的匹配：subject_refs 可能为裸码、CN:ETF:xxx、instrument:xxx 等载体，采用结尾匹配以兼容。 */
function subjectMatches(ref: string, instrument: string): boolean {
  if (!instrument) return false;
  const code = instrument.split(":").pop();
  return ref === instrument || ref === `instrument:${instrument}` || ref.endsWith(`:${instrument}`) || ref === code || ref === `instrument:${code}`;
}

interface Props {
  policy: InvestmentPolicyVersion | null;
  evidence: Evidence[];
  holdings: Holding[];
  theses: Thesis[];
  candles: CandleSeries | null;
  /** K 线请求在途：区分「加载中」与「获取失败/插件未启用」。 */
  candlesLoading: boolean;
  /** 当前 K 线周期（日/周/月），由 K 线卡周期切换按钮驱动。 */
  candlePeriod: CandlePeriod;
  /** 路线图 A1：演示模式明示（内容为界面示例，刷新即还原、不落库）。 */
  isDemo: boolean;
  /** 中国市场行情插件是否启用（未启用时行情卡显示引导启用而非报错）。 */
  marketPluginEnabled: boolean;
  onNavigate: (view: AppView) => void;
  onSaveThesis: (thesis: Thesis) => Promise<boolean>;
  onCreateHolding: (input: { instrument: string; label: string; status: "holding" | "watchlist"; strategy_note: string }) => Promise<boolean>;
  /** M5-F02：`note` 选填（自选不传；持仓传用户填的说明，可为空串）。 */
  onDeleteHolding: (holdingId: string, note?: string) => Promise<boolean>;
  /** M4-E01/E02：标的查找（代码 → 名称 / 名称 → 代码）。空结果与服务不可用分两种返回。 */
  onLookupInstrument: (query: string) => Promise<InstrumentLookupOutcome>;
  onCreateThesis: (input: { instrument: string; original_statement: string; core_assumptions: string[]; supporting_conditions: string[]; invalidation_conditions: string[]; observation_metrics: string[] }) => Promise<boolean>;
  onCreatePolicyDraft: (input: object) => Promise<InvestmentPolicyVersion | null>;
  onConfirmPolicy: (policyId: string, summary: string) => Promise<boolean>;
  onSelectInstrument: (instrument: string) => void;
  /** 切换 K 线周期（基于当前标的重拉）。 */
  onSelectCandlePeriod: (period: CandlePeriod) => void;
  onPullEvidence: (instrument: string, kind: EvidenceKind) => Promise<Evidence[] | null>;
  /** v23 历史重复证据清理（后端按类型+标题+日期分组保留最早一条），返回清理条数。 */
  onDedupEvidence: () => Promise<{ removed: number } | null>;
  onOpenEvidence: (evidenceId: string) => void;
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function InvestmentPage({
  policy,
  evidence,
  holdings,
  theses,
  candles,
  candlesLoading,
  candlePeriod,
  isDemo,
  marketPluginEnabled,
  onNavigate,
  onSaveThesis,
  onCreateHolding,
  onDeleteHolding,
  onLookupInstrument,
  onCreateThesis,
  onCreatePolicyDraft,
  onConfirmPolicy,
  onSelectInstrument,
  onSelectCandlePeriod,
  onPullEvidence,
  onDedupEvidence,
  onOpenEvidence,
}: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(holdings[0]?.holding_id ?? null);
  // R2-1 右侧工作区 tab（切换持仓时保持当前 tab，默认回到行情）。
  const [holdingTab, setHoldingTab] = useState<HoldingTab>("overview");
  const selected = holdings.find((item) => item.holding_id === selectedId) ?? holdings[0] ?? null;
  const thesis = useMemo(
    () => theses.find((item) => selected && item.instrument === selected.instrument) ?? null,
    [theses, selected],
  );

  const [editing, setEditing] = useState(false);
  const [creatingThesis, setCreatingThesis] = useState(false);
  const [editStatement, setEditStatement] = useState("");
  const [editAssumptions, setEditAssumptions] = useState("");
  const [editSupporting, setEditSupporting] = useState("");
  const [editInvalidation, setEditInvalidation] = useState("");
  const [editMetrics, setEditMetrics] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  // 移除持仓 / 自选
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // 新建持仓表单
  const [holdingOpen, setHoldingOpen] = useState(false);
  const [holdingLabel, setHoldingLabel] = useState("");
  const [holdingInstrument, setHoldingInstrument] = useState("");
  const [holdingStatus, setHoldingStatus] = useState<"holding" | "watchlist">("watchlist");
  const [holdingNote, setHoldingNote] = useState("");
  const [holdingError, setHoldingError] = useState<string | null>(null);
  // M4-E02/E03：单框搜索——输入即查（防抖 280ms）；唯一命中自动回填，多条必须点选，
  // 0 条 / 服务不可用时禁止保存。选中项只在**当前查询**下有效：查询一变立即作废，
  // 避免「改了几个字仍能保存上一只标的」的错配。
  const [holdingQuery, setHoldingQuery] = useState("");
  const [holdingHits, setHoldingHits] = useState<InstrumentLookupHit[]>([]);
  const [holdingSelected, setHoldingSelected] = useState<InstrumentLookupHit | null>(null);
  const [holdingLookupBusy, setHoldingLookupBusy] = useState(false);
  const [holdingLookupError, setHoldingLookupError] = useState<string | null>(null);
  const holdingLookupRequest = useRef(0);
  // 查找回调放 ref：防抖 effect 只依赖「开关 + 查询串」，父组件即使传未 memo 的新函数
  // 也不会引发 effect 重复触发（否则会 setState → 重渲染 → 新回调 → 再触发的死循环）。
  const holdingLookupFn = useRef(onLookupInstrument);
  useEffect(() => { holdingLookupFn.current = onLookupInstrument; }, [onLookupInstrument]);

  // M4-E02：输入防抖查询；用请求序号丢弃过期响应（快打时后发先至不会覆盖后一次结果）。
  useEffect(() => {
    if (!holdingOpen) return;
    const query = holdingQuery.trim();
    if (!query) {
      setHoldingHits([]);
      setHoldingLookupBusy(false);
      setHoldingLookupError(null);
      return;
    }
    const requestId = ++holdingLookupRequest.current;
    setHoldingLookupBusy(true);
    const timer = window.setTimeout(() => {
      void (async () => {
        const outcome = await holdingLookupFn.current(query);
        if (holdingLookupRequest.current !== requestId) return; // 过期响应：丢弃
        setHoldingLookupBusy(false);
        if (!outcome.ok) {
          setHoldingHits([]);
          setHoldingLookupError(outcome.message);
          return;
        }
        setHoldingHits(outcome.hits);
        setHoldingLookupError(outcome.hits.length === 0 ? "未找到标的" : null);
        // 唯一命中直接回填；多条必须点选（禁止静默取第一条）。
        if (outcome.hits.length === 1) {
          const hit = outcome.hits[0]!;
          setHoldingSelected(hit);
          setHoldingInstrument(hit.key);
          setHoldingLabel(hit.name);
        }
      })();
    }, 280);
    return () => { window.clearTimeout(timer); };
  }, [holdingOpen, holdingQuery]);

  /** 查询变更：立即作废旧选中，不给「防抖窗口内用旧标的保存」的机会。 */
  function changeHoldingQuery(value: string): void {
    setHoldingQuery(value);
    setHoldingSelected(null);
    setHoldingInstrument("");
    setHoldingLabel("");
    setHoldingLookupError(null);
  }

  function pickHoldingHit(hit: InstrumentLookupHit): void {
    setHoldingSelected(hit);
    setHoldingInstrument(hit.key);
    setHoldingLabel(hit.name);
    setHoldingLookupError(null);
  }

  function openHoldingDialog(): void {
    setHoldingError(null);
    setHoldingQuery("");
    setHoldingHits([]);
    setHoldingSelected(null);
    setHoldingInstrument("");
    setHoldingLabel("");
    setHoldingNote("");
    setHoldingLookupError(null);
    setHoldingLookupBusy(false);
    setHoldingOpen(true);
  }

  const holdingDropdownOpen = holdingHits.length > 0 && !holdingSelected;
  const holdingSaveable = Boolean(holdingInstrument.trim() && holdingLabel.trim()) && !holdingLookupBusy;

  // 原则建立（草案 → 显式确认）
  const [policyOpen, setPolicyOpen] = useState(false);
  const [policyGoal, setPolicyGoal] = useState("");
  const [policyHorizon, setPolicyHorizon] = useState("");
  const [policyLiquidity, setPolicyLiquidity] = useState("");
  const [policyObservation, setPolicyObservation] = useState("");
  const [policyInvalidation, setPolicyInvalidation] = useState("");
  const [policyPreferred, setPolicyPreferred] = useState("");
  const [policyExcluded, setPolicyExcluded] = useState("");
  const [policyDraft, setPolicyDraft] = useState<InvestmentPolicyVersion | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [policyConfirmError, setPolicyConfirmError] = useState<string | null>(null);

  // 三类证据拉取结果：null=未拉取，(空数组对象需 JSON 化区分) 用 undefined 表示未请求
  const [pulled, setPulled] = useState<Record<EvidenceKind, Evidence[] | undefined>>({ announcements: undefined, news: undefined, financials: undefined });
  const [pullBusy, setPullBusy] = useState<EvidenceKind | null>(null);
  const [pullError, setPullError] = useState<string | null>(null);
  // v23 一键拉取：公告→新闻→财报 串行依次拉（避免限速），进度即时可见。
  const [pullAllBusy, setPullAllBusy] = useState(false);
  const [pullProgress, setPullProgress] = useState({ done: 0, total: 0 });
  // v23 历史重复证据清理。
  const [dedupBusy, setDedupBusy] = useState(false);
  const [dedupNote, setDedupNote] = useState<string | null>(null);
  // v23 证据分类筛选：按证据类型分组展示 + chips 过滤。
  const [evidenceFilter, setEvidenceFilter] = useState<string>("all");
  const pullRequest = useRef(0);
  const [holdingBusy, setHoldingBusy] = useState(false);
  const [thesisBusy, setThesisBusy] = useState(false);
  const [policyBusy, setPolicyBusy] = useState(false);
  const holdingDialog = useFocusTrap<HTMLElement>(holdingOpen);
  const policyDialog = useFocusTrap<HTMLElement>(policyOpen && !policyDraft);

  // K 线跟随选中标的：首次挂载或切换标的时按需拉取
  useEffect(() => {
    if (!selected) return;
    onSelectInstrument(selected.instrument);
  }, [selected, onSelectInstrument]);

  // 行情自由查询：用户可在 K 线区手输任意代码，不改变持仓/自选
  const [klineSymbol, setKlineSymbol] = useState("");
  const [klineQuery, setKlineQuery] = useState<string | null>(null);

  function submitKlineQuery() {
    const symbol = klineSymbol.trim().split(":").pop() ?? "";
    if (!symbol) return;
    setKlineQuery(symbol);
    onSelectInstrument(symbol);
    setKlineSymbol("");
  }

  // 选中标的切换后重置已拉取的证据与行情查询覆盖，避免串标的
  useEffect(() => {
    ++pullRequest.current;
    setPullBusy(null);
    setPullError(null);
    setPullAllBusy(false);
    setPullProgress({ done: 0, total: 0 });
    setEvidenceFilter("all");
    setPulled({ announcements: undefined, news: undefined, financials: undefined });
    setKlineQuery(null);
  }, [selected?.holding_id]);

  const relevantEvidence = useMemo(() => {
    if (!selected) return [];
    return evidence.filter((item) => item.subject_refs.some((ref) => subjectMatches(ref, selected.instrument)));
  }, [evidence, selected]);

  const relationCounts = useMemo(() => {
    const counts = { supporting: 0, contradicting: 0, unknown: 0 };
    for (const item of relevantEvidence) {
      if (item.relation === "supporting") counts.supporting += 1;
      else if (item.relation === "contradicting") counts.contradicting += 1;
      else counts.unknown += 1;
    }
    return counts;
  }, [relevantEvidence]);

  // v23 按证据类型分组（组内时间倒序），供分类 chips 过滤展示。
  const groupedEvidence = useMemo(
    () =>
      EVIDENCE_GROUPS.map(({ key, label, types }) => ({
        key,
        label,
        items: relevantEvidence
          .filter((item) => types.includes(item.evidence_type))
          .sort((a, b) => new Date(b.observed_at ?? b.collected_at).getTime() - new Date(a.observed_at ?? a.collected_at).getTime()),
      })),
    [relevantEvidence],
  );

  const openExtensions = () => onNavigate("extensions");
  const thesisFields = thesis ? [thesis.original_statement.trim(), thesis.core_assumptions.length, thesis.supporting_conditions.length, thesis.invalidation_conditions.length, thesis.observation_metrics.length] : [];
  const completeFields = thesisFields.filter(Boolean).length;
  const selectedCandles = selected && candles?.instrument.split(":").pop() === selected.instrument.split(":").pop() ? candles : null;
  const latestBar = !candlesLoading ? selectedCandles?.bars.at(-1) : undefined;
  const thesisRecords = theses.filter((item) => item.instrument === selected?.instrument).sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());

  function startEdit() {
    if (!selected) return;
    setHoldingTab("thesis");
    setCreatingThesis(false);
    setEditStatement(thesis?.original_statement ?? "");
    setEditAssumptions((thesis?.core_assumptions ?? []).join("\n"));
    setEditSupporting((thesis?.supporting_conditions ?? []).join("\n"));
    setEditInvalidation((thesis?.invalidation_conditions ?? []).join("\n"));
    setEditMetrics((thesis?.observation_metrics ?? []).join("\n"));
    setEditing(true);
    setEditError(null);
  }

  function startCreateThesis() {
    if (!selected) return;
    setHoldingTab("thesis");
    setEditing(true);
    setCreatingThesis(true);
    setEditStatement("");
    setEditAssumptions("");
    setEditSupporting("");
    setEditInvalidation("");
    setEditMetrics("");
    setEditError(null);
  }

  async function saveEditor() {
    if (!selected || thesisBusy) return;
    const statement = editStatement.trim();
    if (!statement) { setEditError("投资逻辑陈述不能为空。"); return; }
    setEditError(null);
    if (thesis) { setConfirmOpen(true); return; }
    setThesisBusy(true);
    try {
      const ok = await onCreateThesis({
        instrument: selected.instrument, original_statement: statement,
        core_assumptions: lines(editAssumptions), supporting_conditions: lines(editSupporting),
        invalidation_conditions: lines(editInvalidation), observation_metrics: lines(editMetrics),
      });
      if (!ok) throw new Error("Thesis not saved");
      setEditing(false);
      setCreatingThesis(false);
    } catch { setEditError("保存失败，编辑内容已保留，请重试。"); }
    finally { setThesisBusy(false); }
  }

  async function doConfirm(_note: string) {
    if (!thesis || thesisBusy) return;
    setThesisBusy(true);
    setEditError(null);
    try {
      const ok = await onSaveThesis({
        ...thesis, original_statement: editStatement.trim(),
        core_assumptions: lines(editAssumptions), supporting_conditions: lines(editSupporting),
        invalidation_conditions: lines(editInvalidation), observation_metrics: lines(editMetrics),
        updated_at: new Date().toISOString(),
      });
      if (!ok) throw new Error("Thesis not saved");
      setConfirmOpen(false);
      setEditing(false);
    } catch { setEditError("保存失败，编辑内容已保留，请重试。"); }
    finally { setThesisBusy(false); }
  }

  async function doConfirmDelete(note: string) {
    if (!selected || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      // M5-F02：说明为选填——空串照常提交，审计侧用固定句兜底（后端决定，不在前端编造）。
      if (!await onDeleteHolding(selected.holding_id, note.trim())) throw new Error("Holding not removed");
      setDeleteOpen(false);
    } catch { setDeleteError("移除失败，请重试。"); }
    finally { setDeleteBusy(false); }
  }

  async function saveHolding() {
    const instrument = holdingInstrument.trim();
    if (!instrument || !holdingLabel.trim() || holdingBusy) return;
    setHoldingBusy(true);
    setHoldingError(null);
    try {
      const ok = await onCreateHolding({ instrument, label: holdingLabel.trim(), status: holdingStatus, strategy_note: holdingNote.trim() });
      if (!ok) throw new Error("Holding not saved");
      setHoldingOpen(false);
      setHoldingLabel(""); setHoldingInstrument(""); setHoldingNote("");
      setHoldingQuery(""); setHoldingHits([]); setHoldingSelected(null); setHoldingLookupError(null);
    } catch { setHoldingError("保存失败，表单内容已保留，请重试。"); }
    finally { setHoldingBusy(false); }
  }

  async function createPolicyDraft() {
    if (!policyGoal.trim() || policyBusy) return;
    if (policyHorizon && (!Number.isFinite(Number(policyHorizon)) || Number(policyHorizon) < 0)) {
      setPolicyError("投资年限必须是非负数。"); return;
    }
    setPolicyBusy(true);
    setPolicyError(null);
    try {
      const draft = await onCreatePolicyDraft({
        investment_goal: policyGoal.trim(), horizon_years: policyHorizon ? Number(policyHorizon) : undefined,
        liquidity_needs: policyLiquidity.trim(), observation_conditions: lines(policyObservation),
        invalidation_conditions: lines(policyInvalidation),
        preferred_methods: policyPreferred ? policyPreferred.split(/[,，、]/).map((item) => item.trim()).filter(Boolean) : [],
        excluded_methods: policyExcluded ? policyExcluded.split(/[,，、]/).map((item) => item.trim()).filter(Boolean) : [],
      });
      if (!draft) throw new Error("Policy draft not saved");
      setPolicyConfirmError(null);
      setPolicyDraft(draft);
    } catch { setPolicyError("草案创建失败，内容已保留，请重试。"); }
    finally { setPolicyBusy(false); }
  }

  async function doConfirmPolicy(summary: string) {
    if (!policyDraft || policyBusy) return;
    setPolicyBusy(true);
    setPolicyConfirmError(null);
    try {
      if (!await onConfirmPolicy(policyDraft.policy_id, summary)) throw new Error("Policy not confirmed");
      setPolicyDraft(null); setPolicyOpen(false);
      setPolicyGoal(""); setPolicyHorizon(""); setPolicyLiquidity("");
      setPolicyObservation(""); setPolicyInvalidation(""); setPolicyPreferred(""); setPolicyExcluded("");
    } catch { setPolicyConfirmError("确认失败，草案已保留，请重试。"); }
    finally { setPolicyBusy(false); }
  }

  async function pull(kind: EvidenceKind) {
    if (!selected || pullBusy) return;
    const request = ++pullRequest.current;
    setPullBusy(kind);
    setPullError(null);
    try {
      const entries = await onPullEvidence(selected.instrument, kind);
      if (request !== pullRequest.current) return;
      if (entries === null) throw new Error("Evidence unavailable");
      setPulled((current) => ({ ...current, [kind]: entries }));
    } catch {
      if (request === pullRequest.current) setPullError("证据拉取失败，请重试。");
    } finally {
      if (request === pullRequest.current) setPullBusy(null);
    }
  }

  /** v23 一键拉取：公告→新闻→财报 串行依次拉取（避免触发限速），单源失败不中断其余来源。 */
  async function pullAll() {
    if (!selected || pullBusy || pullAllBusy) return;
    const request = ++pullRequest.current;
    setPullAllBusy(true);
    setPullError(null);
    setPullProgress({ done: 0, total: EVIDENCE_KINDS.length });
    let failures = 0;
    try {
      for (const [index, { kind }] of EVIDENCE_KINDS.entries()) {
        try {
          const entries = await onPullEvidence(selected.instrument, kind);
          if (request !== pullRequest.current) return;
          if (entries === null) failures += 1;
          else setPulled((current) => ({ ...current, [kind]: entries }));
        } catch {
          if (request !== pullRequest.current) return;
          failures += 1;
        }
        setPullProgress({ done: index + 1, total: EVIDENCE_KINDS.length });
      }
      if (request === pullRequest.current) {
        if (failures === EVIDENCE_KINDS.length) setPullError("三个来源全部拉取失败：请检查数据插件是否启用或网络后重试。");
        else if (failures > 0) setPullError(`已完成拉取，${failures} 个来源失败（成功来源的结果已保留）。`);
      }
    } finally {
      if (request === pullRequest.current) setPullAllBusy(false);
    }
  }

  /** v23 清理历史重复证据（哈希归一化之前入账的同内容多条），保留最早一条。 */
  async function runDedup() {
    if (dedupBusy) return;
    setDedupBusy(true);
    setDedupNote(null);
    try {
      const result = await onDedupEvidence();
      setDedupNote(result ? (result.removed > 0 ? `已清理 ${result.removed} 条重复证据（各组保留最早入账一条）` : "未发现重复证据") : "清理请求失败，请重试。");
    } catch {
      setDedupNote("清理请求失败，请重试。");
    } finally {
      setDedupBusy(false);
    }
  }

  const klineTitle = klineQuery
    ? `${klineQuery}K 线`
    : selected
      ? `${selected.label} · ${selected.instrument.split(":").pop()}`
      : "标的行情";

  return (
    <div className="page-stack investment-page">
      {isDemo && <DemoNotice context="持仓 / 证据 / 行情" />}
      <section className="investment-heading">
        <div>
          <span className="section-kicker">投资原则 {policy ? `v${policy.version}` : "待建立"}</span>
          <h2>{policy?.investment_goal ?? "先写下你希望怎样投资"}</h2>
          <p>
            {policy
              ? `${policy.status === "active" ? "已确认" : "待确认"} · ${holdings.filter((item) => item.status === "holding").length} 个持仓 · ${holdings.filter((item) => item.status === "watchlist").length} 个自选`
              : "暂无投资原则"}
          </p>
        </div>
        <button className="primary-button" onClick={() => { setPolicyError(null); (policy ? onNavigate("review") : setPolicyOpen(true)); }}>
          {policy ? "复核原则" : "建立原则"}
          <span>→</span>
        </button>
      </section>

      {holdings.length > 0 ? (
        <section className="investment-workspace">
          <aside className="holding-rail">
            <div className="rail-title">
              <span>持仓与自选</span>
              <b>{holdings.length}</b>
              <button className="text-button" onClick={openHoldingDialog}>
                <Plus size={15} aria-hidden="true" />新建
              </button>
            </div>
            {holdings.map((item) => (
              <button
                type="button"
                className={`holding-item ${selected?.holding_id === item.holding_id ? "selected" : ""}`}
                key={item.holding_id}
                aria-current={selected?.holding_id === item.holding_id ? "true" : undefined}
                onClick={() => {
                  setSelectedId(item.holding_id);
                  setEditing(false);
                  setCreatingThesis(false);
                }}
              >
                <span className={`q-status ${item.status === "holding" ? "mint" : "amber"}`} />
                <span>
                  <b>{item.label}</b>
                  <small>{item.instrument} · {item.status === "holding" ? "持仓" : "自选"}</small>
                </span>
              </button>
            ))}
          </aside>

          <div className="holding-detail">
            {selected && (
              <>
                <div className="holding-head">
                  <div>
                    <span className="eyebrow">{selected.instrument}</span>
                    <h3>{selected.label}</h3>
                    <p>{selected.strategy_note}</p>
                  </div>
                  <div className="holding-actions">
                    <button className="secondary-button" onClick={() => (thesis ? startEdit() : startCreateThesis())}>
                      <Pencil size={15} aria-hidden="true" />
                      {thesis ? "编辑投资逻辑" : "新建投资逻辑"}
                    </button>
                    <button className="icon-button danger" aria-label={`移除${selected.label}`} title={`移除${selected.status === "holding" ? "持仓" : "自选"}`} onClick={() => { setDeleteError(null); setDeleteOpen(true); }}>
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </div>
                </div>

                <dl className="holding-summary">
                  <div><dt>标的状态</dt><dd>{selected.status === "holding" ? "持仓" : "自选"}</dd></div>
                  <div><dt>最近收盘价</dt><dd>{candlesLoading ? "加载中" : latestBar ? latestBar.close.toLocaleString("zh-CN", { maximumFractionDigits: 3 }) : "暂无行情"}</dd></div>
                  <div><dt>论点完整度</dt><dd>{completeFields} / 5</dd></div>
                  <div><dt>反证 / 过期证据</dt><dd>{relationCounts.contradicting} / {relevantEvidence.filter((item) => item.status === "stale").length}</dd></div>
                </dl>

                <nav className="holding-tabs" role="tablist" aria-label="标的工作区">
                  {HOLDING_TABS.map((tab) => (
                    <button
                      key={tab.id}
                      className={`extension-tab ${holdingTab === tab.id ? "active" : ""}`}
                      role="tab"
                      id={`holding-tab-${tab.id}`}
                      aria-controls="holding-work-panel"
                      tabIndex={holdingTab === tab.id ? 0 : -1}
                      aria-selected={holdingTab === tab.id}
                      onKeyDown={(event) => {
                        const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
                        if (!direction && event.key !== "Home" && event.key !== "End") return;
                        event.preventDefault();
                        const index = HOLDING_TABS.findIndex((item) => item.id === tab.id);
                        const next = event.key === "Home" ? HOLDING_TABS[0]! : event.key === "End" ? HOLDING_TABS.at(-1)! : HOLDING_TABS[(index + direction + HOLDING_TABS.length) % HOLDING_TABS.length]!;
                        setHoldingTab(next.id);
                        document.getElementById(`holding-tab-${next.id}`)?.focus();
                      }}
                      onClick={() => setHoldingTab(tab.id)}
                    >
                      {tab.label}
                    </button>
                  ))}
                </nav>

                <div id="holding-work-panel" role="tabpanel" aria-labelledby={`holding-tab-${holdingTab}`}>

                {holdingTab === "thesis" && (thesis || creatingThesis ? (
                  <>
                    <div className="thesis-block">
                      <SectionHeading eyebrow="投资逻辑" title="一条可被证据检验的陈述" />
                      {editing ? (
                        <div className="thesis-editor-stack">
                          <textarea
                            className="thesis-editor"
                            aria-label="投资逻辑陈述"
                            placeholder={creatingThesis ? "写下可被证据支持的判断陈述…" : undefined}
                            value={editStatement}
                            onChange={(event) => setEditStatement(event.target.value)}
                            rows={3}
                          />
                          <div className="thesis-grid">
                            <div className="thesis-panel">
                              <div className="answer-block-title">核心假设</div>
                              <textarea aria-label="核心假设" value={editAssumptions} onChange={(event) => setEditAssumptions(event.target.value)} rows={3} placeholder="每行一条" />
                            </div>
                            <div className="thesis-panel">
                              <div className="answer-block-title">观察指标</div>
                              <textarea aria-label="观察指标" value={editMetrics} onChange={(event) => setEditMetrics(event.target.value)} rows={3} placeholder="每行一条" />
                            </div>
                            <div className="thesis-panel mint">
                              <div className="answer-block-title">支持条件</div>
                              <textarea aria-label="支持条件" value={editSupporting} onChange={(event) => setEditSupporting(event.target.value)} rows={3} placeholder="每行一条" />
                            </div>
                            <div className="thesis-panel amber">
                              <div className="answer-block-title">失效条件</div>
                              <textarea aria-label="失效条件" value={editInvalidation} onChange={(event) => setEditInvalidation(event.target.value)} rows={3} placeholder="每行一条" />
                            </div>
                          </div>
                        </div>
                      ) : (
                        <p className="thesis-copy">{thesis?.original_statement}</p>
                      )}
                      {editError && <p className="form-error">{editError}</p>}
                    </div>
                    {editing && (
                      <div className="thesis-edit-actions">
                        <button className="primary-button" disabled={thesisBusy} onClick={saveEditor}>
                          {thesisBusy ? "保存中…" : creatingThesis ? "保存投资逻辑" : "预览并确认"} <span>→</span>
                        </button>
                        <button className="text-button" disabled={thesisBusy} onClick={() => { setEditing(false); setCreatingThesis(false); }}>
                          取消
                        </button>
                      </div>
                    )}

                    {!editing && thesis && (
                      <div className="thesis-grid">
                        <div className="thesis-panel">
                          <div className="answer-block-title">核心假设</div>
                          <ul className="answer-list">{thesis.core_assumptions.map((item) => <li key={item}>{item}</li>)}</ul>
                        </div>
                        <div className="thesis-panel">
                          <div className="answer-block-title">观察指标</div>
                          <ul className="answer-list">{thesis.observation_metrics.map((item) => <li key={item}>{item}</li>)}</ul>
                        </div>
                        <div className="thesis-panel mint">
                          <div className="answer-block-title">支持条件</div>
                          <ul className="answer-list">{thesis.supporting_conditions.map((item) => <li key={item}>{item}</li>)}</ul>
                        </div>
                        <div className="thesis-panel amber">
                          <div className="answer-block-title">失效条件</div>
                          <ul className="answer-list">{thesis.invalidation_conditions.map((item) => <li key={item}>{item}</li>)}</ul>
                        </div>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="evidence-group-empty">尚无投资逻辑。点击「新建投资逻辑」先写下你的判断依据。</p>
                ))}

            {selected && (holdingTab === "market" || holdingTab === "overview") && (
              <div className="holding-market">
                <div className="market-toolbar">
                  <input
                    className="market-symbol-input"
                    placeholder="输入行情代码查询 K 线，如 600519 或 510300"
                    aria-label="行情代码查询"
                    value={klineSymbol}
                    onChange={(event) => setKlineSymbol(event.target.value)}
                    onKeyDown={(event) => { if (event.key === "Enter") submitKlineQuery(); }}
                  />
                  <button className="primary-btn" onClick={submitKlineQuery}>查询</button>
                  {klineQuery && (
                    <span className="market-query-note">当前查询 {klineQuery} · 切换持仓回到对应标的</span>
                  )}
                </div>
                <CardSlot slot="invest.market_view" loading={candlesLoading}>
                  <KLineCard series={candles} title={klineTitle} loading={candlesLoading} period={candlePeriod} marketPluginEnabled={marketPluginEnabled} onManage={openExtensions} onSelectPeriod={onSelectCandlePeriod} />
                </CardSlot>
              </div>
            )}

            {holdingTab === "overview" && <section className="investment-overview">
              <div><SectionHeading eyebrow="当前论点" title="判断与重新验证条件" /><p>{thesis?.original_statement || "尚未建立投资论点"}</p><h4>重新验证条件</h4>{thesis?.invalidation_conditions.length ? <ul>{thesis.invalidation_conditions.map((condition) => <li key={condition}>{condition}</li>)}</ul> : <p className="evidence-group-empty">暂无失效条件</p>}<button className="text-button" onClick={() => setHoldingTab("thesis")}>查看论点</button></div>
              <div><SectionHeading eyebrow="证据状态" title="支持与反证" /><div className="tag-row"><span className="soft-tag mint">支持 {relationCounts.supporting}</span><span className="soft-tag amber">反证 {relationCounts.contradicting}</span><span className="soft-tag gray">未知 {relationCounts.unknown}</span></div>{relevantEvidence.slice(0, 3).map((item) => <EvidenceRow item={item} key={item.evidence_id} onOpen={onOpenEvidence} />)}{!relevantEvidence.length && <p className="evidence-group-empty">暂无关联证据</p>}<button className="text-button" onClick={() => setHoldingTab("evidence")}>查看全部证据</button></div>
            </section>}
            {holdingTab === "thesis" && thesisRecords.length > 0 && <section className="thesis-records"><SectionHeading eyebrow="论点记录" title="建立与更新" /><ol>{thesisRecords.map((record) => <li key={record.thesis_id}><time dateTime={record.created_at}>建立于 {formatDate(record.created_at)}</time><p>{record.original_statement}</p><span>{record.status === "active" ? "当前有效" : "已归档"} · 最近更新 {formatDate(record.updated_at)}</span></li>)}</ol></section>}

            {selected && holdingTab === "evidence" && (
              <div className="holding-evidence">
                <SectionHeading eyebrow="相关证据" title={`${selected.label} 的证据流`} />
                <div className="tag-row">
                  <span className="soft-tag mint">支持证据 {relationCounts.supporting}</span>
                  <span className="soft-tag amber">相反证据 {relationCounts.contradicting}</span>
                  <span className="soft-tag gray">证据不足 {relationCounts.unknown}</span>
                </div>

                {/* v23 拉取操作前置：一键拉取（公告→新闻→财报串行）+ 分源拉取，不再沉底 */}
                <div className="pull-block">
                  {pullError && <p className="form-error" role="alert">{pullError}</p>}
                  <span className="pull-block-label">拉取外部证据</span>
                  <div className="pull-row">
                    <button className="pull-btn primary" disabled={pullBusy !== null || pullAllBusy} onClick={() => void pullAll()}>
                      {pullAllBusy ? `一键拉取中 ${pullProgress.done}/${pullProgress.total}…` : "一键拉取（公告+新闻+财报）"}
                    </button>
                    {EVIDENCE_KINDS.map(({ kind, label }) => (
                      <button key={kind} className="pull-btn" disabled={pullBusy !== null || pullAllBusy} onClick={() => void pull(kind)}>
                        {pullBusy === kind ? "获取中…" : `拉取${label}`}
                      </button>
                    ))}
                    <button className="pull-btn" disabled={dedupBusy || pullAllBusy || pullBusy !== null} title="历史重复证据一次性清理：按类型+标题+日期分组，保留最早入账一条" onClick={() => void runDedup()}>
                      {dedupBusy ? "清理中…" : "清理重复"}
                    </button>
                  </div>
                  {dedupNote && <p className="report-meta">{dedupNote}</p>}
                  {Object.entries(pulled).some(([, entries]) => entries !== undefined) && (
                    <div className="pulled-group">
                      {EVIDENCE_KINDS.map(({ kind, label }) => {
                        const entries = pulled[kind];
                        if (entries === undefined) return null;
                        return (
                          <div key={kind}>
                            <div className="evidence-group-head">
                              <span>本次拉取 · {label}</span>
                              <b>{entries.length} 条</b>
                            </div>
                            {entries.length > 0 ? (
                              <div className="pull-grid">
                                {entries.map((item) => (
                                  <EvidenceRow item={item} key={item.evidence_id} expanded onOpen={onOpenEvidence} />
                                ))}
                              </div>
                            ) : (
                              <p className="evidence-group-empty">该数据源尚未产生可入账的证据（可能取数被限或未启用数据插件），不编造结果。</p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                {/* v23 已入库证据：分类 chips 过滤 + 按类型分组（组内时间倒序） */}
                <div className="tag-row" role="group" aria-label="证据分类筛选">
                  <button className={`tag-button ${evidenceFilter === "all" ? "active" : ""}`} onClick={() => setEvidenceFilter("all")}>全部（{relevantEvidence.length}）</button>
                  {groupedEvidence.map((group) => (
                    <button key={group.key} className={`tag-button ${evidenceFilter === group.key ? "active" : ""}`} onClick={() => setEvidenceFilter(group.key)}>
                      {group.label}（{group.items.length}）
                    </button>
                  ))}
                </div>
                {relevantEvidence.length === 0 ? (
                  <p className="evidence-group-empty">还没有与该标的关联的证据。点上方「一键拉取」获取公告/新闻/财报，结果会自动入账并在此分类展示。</p>
                ) : (
                  groupedEvidence
                    .filter((group) => evidenceFilter === "all" || evidenceFilter === group.key)
                    .filter((group) => evidenceFilter !== "all" || group.items.length > 0)
                    .map((group) => (
                      <div key={group.key} className="pulled-evidence">
                        <div className="evidence-group-head">
                          <span>{group.label}</span>
                          <b>{group.items.length} 条</b>
                        </div>
                        {group.items.length > 0 ? (
                          group.items.map((item) => <EvidenceRow item={item} key={item.evidence_id} onOpen={onOpenEvidence} />)
                        ) : (
                          <p className="evidence-group-empty">该分类暂无已入账证据。</p>
                        )}
                      </div>
                    ))
                )}
              </div>
            )}
                </div>
              </>
            )}
          </div>
        </section>
      ) : (
        <div>
          <p className="evidence-group-empty">还没有记录任何持仓或自选。先建立一个标的，管家才能把变化与你关联起来。</p>
          <button className="primary-button" onClick={openHoldingDialog}>
            新建第一个标的 <span>→</span>
          </button>
        </div>
      )}

      {deleteOpen && selected && (
        <ConfirmDialog
          title={`移除${selected.status === "holding" ? "持仓" : "自选"}「${selected.label}」`}
          summary="该标的将从本地账本移除。关联的历史 Evidence 证据仍保留可读（只删持有记录，不动证据账本）。"
          diffs={[{ label: "标的", before: `${selected.instrument} · ${selected.label}`, after: "移除" }]}
          confirmLabel="确认移除"
          // M5-F01/F02：自选移除不写原因（不显示说明框）；持仓移除保留说明框但改为**选填**。
          // 原则确认与投资逻辑确认维持必填（本弹窗之外的实例不受影响）。
          requireNote={false}
          noteOptional={selected.status === "holding"}
          busy={deleteBusy}
          error={deleteError}
          onConfirm={doConfirmDelete}
          onCancel={() => setDeleteOpen(false)}
        />
      )}

      {confirmOpen && thesis && (
        <ConfirmDialog
          title="确认修改投资逻辑"
          summary="投资逻辑是判断依据；修改后旧版本保留在账本中，本次确认将写入审计记录。"
          diffs={[
            { label: "投资逻辑陈述", before: thesis.original_statement, after: editStatement.trim() },
            { label: "核心假设", before: thesis.core_assumptions.join("\n"), after: lines(editAssumptions).join("\n") },
            { label: "支持条件", before: thesis.supporting_conditions.join("\n"), after: lines(editSupporting).join("\n") },
            { label: "失效条件", before: thesis.invalidation_conditions.join("\n"), after: lines(editInvalidation).join("\n") },
            { label: "观察指标", before: thesis.observation_metrics.join("\n"), after: lines(editMetrics).join("\n") },
          ]}
          busy={thesisBusy}
          error={editError}
          onConfirm={doConfirm}
          onCancel={() => setConfirmOpen(false)}
        />
      )}

      {policyDraft && (
        <ConfirmDialog
          title="确认建立投资原则"
          summary="这套原则将成为你判断的边界。确认后起草版本转为有效，旧版原则自动转 superseded，本次确认写入审计。"
          diffs={[{ label: "投资目标", before: "无原则", after: policyDraft.investment_goal }]}
          confirmLabel="确认原则"
          busy={policyBusy}
          error={policyConfirmError}
          onConfirm={doConfirmPolicy}
          onCancel={() => setPolicyDraft(null)}
        />
      )}

      {holdingOpen && (
        <div className="confirm-overlay" onClick={() => { if (!holdingBusy) setHoldingOpen(false); }}>
          <aside ref={holdingDialog} className="invest-modal" role="dialog" aria-modal="true" aria-label="新建持仓 / 自选" onKeyDown={(event) => { if (event.key === "Escape" && !holdingBusy) setHoldingOpen(false); }} onClick={(event) => event.stopPropagation()}>
            <header className="confirm-head">
              <span className="section-kicker">NEW HOLDING</span>
              <h3>新建持仓 / 自选</h3>
              <p>先记账再讨论：管家只根据你这条记录关联后续变化。</p>
            </header>
            <div className="invest-form">
              <div className="form-field invest-lookup">
                <label htmlFor="holding-lookup-input">标的（输入代码或名称）</label>
                <input
                  id="holding-lookup-input"
                  value={holdingQuery}
                  onChange={(event) => changeHoldingQuery(event.target.value)}
                  placeholder="001201 / 510300 / 东瑞股份"
                  role="combobox"
                  aria-expanded={holdingDropdownOpen}
                  aria-controls="holding-lookup-list"
                  aria-autocomplete="list"
                  autoComplete="off"
                />
                {holdingDropdownOpen && (
                  <ul id="holding-lookup-list" className="invest-lookup-list" role="listbox" aria-label="匹配到的标的">
                    {holdingHits.map((hit) => (
                      <li key={hit.key}>
                        <button type="button" role="option" aria-selected={false} onClick={() => pickHoldingHit(hit)}>
                          <b>{hit.name}</b>
                          <small>{hit.key}</small>
                          <em>{INSTRUMENT_KIND_LABEL[hit.kind] ?? hit.kind} · {INSTRUMENT_MARKET_LABEL[hit.market] ?? hit.market}</em>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {holdingLookupBusy && <small className="form-hint">查找中…</small>}
                {!holdingLookupBusy && holdingLookupError && <small className="form-error" role="alert">{holdingLookupError}</small>}
                {!holdingLookupBusy && !holdingLookupError && holdingHits.length > 1 && (
                  <small className="form-hint">命中 {holdingHits.length} 条，请点选一条（不会自动取第一条）。</small>
                )}
              </div>
              {holdingSelected && (
                <p className="form-hint invest-lookup-picked">
                  已选中 <b>{holdingSelected.name}</b>（{holdingSelected.key}，{INSTRUMENT_KIND_LABEL[holdingSelected.kind] ?? holdingSelected.kind}）。
                </p>
              )}
              <div className="form-row">
                <label className="form-field">
                  <span>名称（可改）</span>
                  <input value={holdingLabel} onChange={(event) => setHoldingLabel(event.target.value)} placeholder="沪深 300 ETF" />
                </label>
                <label className="form-field">
                  <span>标的编码</span>
                  <input value={holdingInstrument} readOnly placeholder="选中标的后自动回填" aria-readonly="true" />
                </label>
              </div>
              <label className="form-field">
                <span>类型</span>
                <select value={holdingStatus} onChange={(event) => setHoldingStatus(event.target.value as "holding" | "watchlist")}>
                  <option value="watchlist">自选（观察）</option>
                  <option value="holding">持仓</option>
                </select>
              </label>
              <label className="form-field">
                <span>策略备注</span>
                <textarea value={holdingNote} onChange={(event) => setHoldingNote(event.target.value)} rows={2} placeholder="为什么要关注它…" />
              </label>
              {holdingError && <p className="form-error" role="alert">{holdingError}</p>}
              <footer className="form-foot">
                <button className="secondary-button" disabled={holdingBusy} onClick={() => setHoldingOpen(false)}>取消</button>
                <button className="primary-button" disabled={holdingBusy || !holdingSaveable} onClick={saveHolding}>
                  {holdingBusy ? "保存中…" : "保存"} <span>→</span>
                </button>
              </footer>
            </div>
          </aside>
        </div>
      )}

      {policyOpen && !policyDraft && (
        <div className="confirm-overlay" onClick={() => { if (!policyBusy) setPolicyOpen(false); }}>
          <aside ref={policyDialog} className="invest-modal wide" role="dialog" aria-modal="true" aria-label="建立第一版投资原则" onKeyDown={(event) => { if (event.key === "Escape" && !policyBusy) setPolicyOpen(false); }} onClick={(event) => event.stopPropagation()}>
            <header className="confirm-head">
              <span className="section-kicker">NEW POLICY DRAFT</span>
              <h3>建立第一版投资原则</h3>
              <p>先起草，经显式确认后才生效。仅「投资目标」为必填，其余按需补充。</p>
            </header>
            <div className="invest-form">
              <label className="form-field">
                <span>投资目标（必填）</span>
                <textarea value={policyGoal} onChange={(event) => setPolicyGoal(event.target.value)} rows={3} placeholder="用一句话说明你希望怎样投资…" />
              </label>
              <div className="form-row">
                <label className="form-field">
                  <span>投资年限（年）</span>
                  <input value={policyHorizon} onChange={(event) => setPolicyHorizon(event.target.value)} placeholder="如 10" type="number" />
                </label>
                <label className="form-field">
                  <span>流动性需求</span>
                  <input value={policyLiquidity} onChange={(event) => setPolicyLiquidity(event.target.value)} placeholder="近期用钱需求…" />
                </label>
              </div>
              <label className="form-field">
                <span>可接受的偏好方法（逗号分隔）</span>
                <input value={policyPreferred} onChange={(event) => setPolicyPreferred(event.target.value)} placeholder="如 定投, 指数化" />
              </label>
              <label className="form-field">
                <span>排除方法（逗号分隔）</span>
                <input value={policyExcluded} onChange={(event) => setPolicyExcluded(event.target.value)} placeholder="如 加杠杆, 高频交易" />
              </label>
              <div className="form-row">
                <label className="form-field">
                  <span>观察条件（每行一条）</span>
                  <textarea value={policyObservation} onChange={(event) => setPolicyObservation(event.target.value)} rows={3} />
                </label>
                <label className="form-field">
                  <span>失效条件（每行一条）</span>
                  <textarea value={policyInvalidation} onChange={(event) => setPolicyInvalidation(event.target.value)} rows={3} />
                </label>
              </div>
              {policyError && <p className="form-error" role="alert">{policyError}</p>}
              <footer className="form-foot">
                <button className="secondary-button" disabled={policyBusy} onClick={() => setPolicyOpen(false)}>取消</button>
                <button className="primary-button" disabled={policyBusy || !policyGoal.trim()} onClick={createPolicyDraft}>
                  {policyBusy ? "起草中…" : "起草原则"} <span>→</span>
                </button>
              </footer>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
