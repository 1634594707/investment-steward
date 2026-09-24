import { useMemo, useRef, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import type { AgentResponse, Evidence, ResearchRun, RunStatus } from "@investment-steward/domain-contracts";
import { needsConfirmation, type CardAction } from "../state/actionDispatcher";
import { SectionHeading } from "../components/kit";
import { EvidenceRow } from "../components/EvidenceRow";
import { EvidenceRef } from "../components/EvidenceRef";
import { ActionModeChip } from "../components/ActionModeChip";
import { SourceLine } from "../components/SourceLine";
import { formatDate } from "../state/format";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type { AppView } from "../shell/nav";
import { SkeletonLines } from "../components/Skeleton";
import { useFocusTrap } from "../components/useFocusTrap";
import "./research.css";

interface Props {
  evidence: Evidence[];
  response: AgentResponse | null;
  runs: ResearchRun[];
  onNavigate: (view: AppView) => void;
  onOpenEvidence: (evidenceId: string) => void;
  onCardAction: (action: CardAction) => void;
  onCreateRun: (question: string) => Promise<AgentResponse | null>;
  onSelectRun: (runId: string) => Promise<AgentResponse | null>;
  onTransitionRun: (runId: string, target: string) => Promise<boolean>;
  onDeleteRun: (runId: string) => Promise<boolean>;
  /** 证据时效巡检（POST /evidence/freshness/patrol：只审计标记过期，不改写账本）。 */
  onPatrolFreshness: () => Promise<{ checked: number; stale: number } | null>;
  /** M2-03：AI 前置就绪 = 存在「使用中」模型方案；未就绪时新建研究入口禁用。 */
  aiReady?: boolean;
}

type GroupKey = "supporting" | "contradicting" | "unknown";

const GROUP_LABEL: Record<GroupKey, { title: string; dot: string }> = {
  supporting: { title: "支持长期逻辑", dot: "supporting" },
  contradicting: { title: "挑战现有判断", dot: "contradicting" },
  unknown: { title: "尚属未知", dot: "unknown" },
};

const RUN_STATUS_META: Record<RunStatus, { label: string; tone: "mint" | "amber" | "red" }> = {
  created: { label: "已创建", tone: "amber" },
  planning: { label: "规划中", tone: "amber" },
  collecting_evidence: { label: "收证中", tone: "amber" },
  analyzing: { label: "分析中", tone: "amber" },
  waiting_confirmation: { label: "待确认", tone: "amber" },
  composing: { label: "组织回答", tone: "amber" },
  completed: { label: "已完成", tone: "mint" },
  failed: { label: "失败", tone: "red" },
  cancelled: { label: "已取消", tone: "red" },
  stale: { label: "已陈旧", tone: "red" },
};

const QUEUES = [
  { id: "all", label: "全部", statuses: [] },
  { id: "new", label: "未开始", statuses: ["created"] },
  { id: "working", label: "研究中", statuses: ["planning", "collecting_evidence", "analyzing", "composing"] },
  { id: "confirm", label: "待确认", statuses: ["waiting_confirmation"] },
  { id: "done", label: "已完成", statuses: ["completed"] },
  { id: "attention", label: "需关注", statuses: ["failed", "cancelled", "stale"] },
] as const;

const RESEARCH_PHASE: Record<RunStatus, number> = {
  created: 0, planning: 0, collecting_evidence: 1, analyzing: 2,
  composing: 2, waiting_confirmation: 3, completed: 3, failed: 0, cancelled: 0, stale: 0,
};

export function ResearchPage({ evidence, response, runs, onNavigate, onOpenEvidence, onCardAction, onCreateRun, onSelectRun, onTransitionRun, onDeleteRun, onPatrolFreshness, aiReady = true }: Props) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [questionOpen, setQuestionOpen] = useState(false);
  const [questionText, setQuestionText] = useState("");
  const [submitBusy, setSubmitBusy] = useState(false);
  const [transitionBusy, setTransitionBusy] = useState(false);
  const [confirmRun, setConfirmRun] = useState<ResearchRun | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [patrolBusy, setPatrolBusy] = useState(false);
  const [patrolResult, setPatrolResult] = useState<string | null>(null);
  const [relationFilter, setRelationFilter] = useState<"all" | GroupKey>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "stale" | "retracted">("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [recentHours, setRecentHours] = useState("all");
  const [deleteRunId, setDeleteRunId] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // 新建研究失败反馈：onCreateRun 返回 null（创建或执行失败）时在弹窗内明示。
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [queue, setQueue] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [selectBusy, setSelectBusy] = useState(false);
  const [selectError, setSelectError] = useState<string | null>(null);
  const selectRequest = useRef(0);
  const questionDialog = useFocusTrap<HTMLElement>(questionOpen);
  const queueStatuses: readonly string[] = QUEUES.find((item) => item.id === queue)?.statuses ?? [];
  const visibleRuns = runs.filter((run) => (queue === "all" || queueStatuses.includes(run.status)) && run.user_question.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));

  async function selectRun(runId: string) {
    const request = ++selectRequest.current;
    setSelectedRunId(runId);
    setSelectBusy(true);
    setSelectError(null);
    try {
      const result = await onSelectRun(runId);
      if (request === selectRequest.current && !result) setSelectError("回答加载失败，请重试。");
    } catch {
      if (request === selectRequest.current) setSelectError("回答加载失败，请重试。");
    } finally {
      if (request === selectRequest.current) setSelectBusy(false);
    }
  }

  const byId = useMemo(() => new Map(evidence.map((item) => [item.evidence_id, item])), [evidence]);

  async function handlePatrolFreshness() {
    if (patrolBusy) return;
    setPatrolBusy(true);
    setPatrolResult(null);
    const result = await onPatrolFreshness();
    setPatrolBusy(false);
    setPatrolResult(result ? `巡检完成：共检查 ${result.checked} 条证据，${result.stale} 条已过有效期` : "巡检失败（Core 未就绪）");
  }
  const refEvidence = (id: string): Evidence | undefined => byId.get(id);

  const selectedRun =
    runs.find((item) => item.run_id === (selectedRunId ?? response?.run_id ?? runs[0]?.run_id)) ?? null;

  // 当前展示的回答必须属于选中的 run，避免切换问题时旧回答短暂串入。
  const shownResponse = response && (!selectedRun || response.run_id === selectedRun.run_id) ? response : null;
  const selectedEvidenceRefs = selectedRun
    ? new Set([
        ...selectedRun.evidence_refs,
        ...(shownResponse?.run_id === selectedRun.run_id ? shownResponse.evidence_refs : []),
      ])
    : null;
  const scopedEvidence = selectedEvidenceRefs
    ? evidence.filter((item) => selectedEvidenceRefs.has(item.evidence_id))
    : evidence;
  const sourceOptions = Array.from(new Set(scopedEvidence.map((item) => item.source_name))).sort();
  const filteredEvidence = scopedEvidence.filter((item) => {
    if (relationFilter !== "all" && item.relation !== relationFilter) return false;
    if (statusFilter !== "all" && item.status !== statusFilter) return false;
    if (sourceFilter !== "all" && item.source_name !== sourceFilter) return false;
    if (recentHours !== "all") {
      const age = Date.now() - new Date(item.observed_at ?? item.collected_at).getTime();
      const limit = Number(recentHours) * 60 * 60 * 1000;
      if (age < 0 || age > limit) return false;
    }
    return true;
  });
  const groups: Record<GroupKey, Evidence[]> = { supporting: [], contradicting: [], unknown: [] };
  for (const item of filteredEvidence) {
    groups[item.relation] ??= [];
    groups[item.relation]!.push(item);
  }

  async function submitQuestion() {
    const question = questionText.trim();
    if (!question || submitBusy) return;
    setSubmitBusy(true);
    setSubmitError(null);
    try {
      const result = await onCreateRun(question);
      if (!result) throw new Error("Research unavailable");
      ++selectRequest.current;
      setSelectBusy(false);
      setSelectError(null);
      setQuestionOpen(false);
      setQuestionText("");
      setSelectedRunId(result.run_id);
    } catch {
      setSubmitError("研究创建失败：未能写入本地 Core 账本（Core 未就绪或网络中断），问题内容已保留，可重试。");
    } finally {
      setSubmitBusy(false);
    }
  }

  async function confirmTransition(run: ResearchRun) {
    if (transitionBusy) return;
    setTransitionBusy(true);
    setTransitionError(null);
    try {
      const ok = await onTransitionRun(run.run_id, "composing");
      if (!ok) throw new Error("Transition unavailable");
      setConfirmRun(null);
      await selectRun(run.run_id);
    } catch {
      setTransitionError("确认未能完成，请重试。");
    } finally {
      setTransitionBusy(false);
    }
  }

  async function handleDeleteRun(runId: string) {
    if (submitBusy || transitionBusy || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      const ok = await onDeleteRun(runId);
      if (ok && selectedRunId === runId) setSelectedRunId(null);
      if (ok) setDeleteRunId(null);
      else setDeleteError("删除失败（Core 未就绪或研究已被移除），请重试。");
    } catch {
      setDeleteError("删除失败（Core 未就绪或网络中断），请重试。");
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <div className="page-stack research-page">
      <section className="research-heading">
        <div>
          <span className="section-kicker">问题与证据</span>
          <h2>研究工作台</h2>
          <p>{runs.length} 个研究问题 · {evidence.length} 条证据</p>
        </div>
        <button className="primary-button" onClick={() => { setSubmitError(null); setQuestionOpen(true); }}>
          <Plus size={16} aria-hidden="true" />新建研究问题
        </button>
      </section>
      <ol className="research-progress" aria-label="研究流程">
        {["问题", "来源", "结论", "确认"].map((label, index) => (
          <li key={label} aria-current={selectedRun && RESEARCH_PHASE[selectedRun.status] === index ? "step" : undefined}>
            <span aria-hidden="true">{index + 1}</span>{label}
          </li>
        ))}
      </ol>
      <section className="research-workspace">
        <div className="research-question-rail">
          <div className="rail-title">
            <span>问题队列</span>
            <b>{String(runs.length).padStart(2, "0")}</b>
          </div>
          <label className="research-search"><span>搜索问题</span><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <label className="research-search"><span>问题状态</span><select value={queue} onChange={(event) => setQueue(event.target.value)}>{QUEUES.map((item) => <option value={item.id} key={item.id}>{item.label} · {item.id === "all" ? runs.length : runs.filter((run) => (item.statuses as readonly string[]).includes(run.status)).length}</option>)}</select></label>
          {visibleRuns.length === 0 && <p className="evidence-group-empty">{runs.length ? "没有匹配的问题" : "暂无研究问题"}</p>}
          {visibleRuns.map((run) => {
            const meta = RUN_STATUS_META[run.status];
            return (
              <div className={`question-item ${selectedRun?.run_id === run.run_id ? "selected" : ""}`} key={run.run_id}>
                <span className={`q-status ${meta.tone}`} />
                <span className="question-main">
                  <button
                    className="question-select"
                    aria-current={selectedRun?.run_id === run.run_id ? "true" : undefined}
                    onClick={() => void selectRun(run.run_id)}
                  >
                    <b>{run.user_question}</b>
                    <small>{meta.label} · {formatDate(run.updated_at)}</small>
                  </button>
                  <button className="question-del" title="删除该研究" aria-label="删除该研究" disabled={deleteBusy} onClick={() => { setDeleteError(null); setDeleteRunId(run.run_id); }}>
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </span>
              </div>
            );
          })}
        </div>

        <div className="research-answer-workspace" aria-label="研究回答" aria-busy={selectBusy}>
          {selectedRun?.status === "waiting_confirmation" && (
            <div className="transition-band">
              <span>该研究等待你确认后可推进到组织回答。</span>
              <button className="primary-button" disabled={transitionBusy} onClick={() => { setTransitionError(null); setConfirmRun(selectedRun); }}>
                确认推进 <span>→</span>
              </button>
            </div>
          )}
          {selectBusy ? <div className="answer-empty" role="status"><strong>正在加载回答…</strong><SkeletonLines lines={3} /></div> : selectError ? (
            <div className="answer-empty" role="alert"><strong>{selectError}</strong><button className="text-button" onClick={() => selectedRun && void selectRun(selectedRun.run_id)}>重试加载</button></div>
          ) : shownResponse ? (
            <AnswerDetail response={shownResponse} evidenceOf={refEvidence} onOpenEvidence={onOpenEvidence} onCardAction={onCardAction} />
          ) : (
            <div className="answer-empty">
              <span className="slot-kicker">ANSWER PENDING</span>
              <strong>还没有一次有据回答。</strong>
              <span>选择一个进行中的问题或新建研究问题，答案会在此呈现全部证据与推理。</span>
            </div>
          )}
        </div>

        <div className="research-evidence-board">
          <SectionHeading eyebrow="证据流" title="支持、挑战与未知" action={filterOpen ? "收起筛选" : "筛选"} onClick={() => setFilterOpen((open) => !open)} />
          <div className="brief-toolbar">
            <button className="text-button" onClick={handlePatrolFreshness} disabled={patrolBusy}>
              {patrolBusy ? "巡检中…" : "巡检证据时效"} <span>→</span>
            </button>
            {patrolResult && <span className="probe-note">{patrolResult}</span>}
          </div>
          {filterOpen && (
            <div className="filter-strip" role="group" aria-label="证据筛选">
              <label>关系
                <select value={relationFilter} onChange={(event) => setRelationFilter(event.target.value as "all" | GroupKey)}>
                  <option value="all">全部</option>
                  <option value="supporting">支持</option>
                  <option value="contradicting">挑战</option>
                  <option value="unknown">未知</option>
                </select>
              </label>
              <label>状态
                <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as "all" | "active" | "stale" | "retracted")}>
                  <option value="all">全部状态</option>
                  <option value="active">有效</option>
                  <option value="stale">陈旧</option>
                  <option value="retracted">已撤回</option>
                </select>
              </label>
              <label>来源
                <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}>
                  <option value="all">全部来源</option>
                  {sourceOptions.map((source) => <option value={source} key={source}>{source}</option>)}
                </select>
              </label>
              <label>时间
                <select value={recentHours} onChange={(event) => setRecentHours(event.target.value)}>
                  <option value="all">全部时间</option>
                  <option value="24">最近 24 小时</option>
                  <option value="168">最近 7 天</option>
                </select>
              </label>
              <span className="filter-summary">{filteredEvidence.length} 条证据</span>
            </div>
          )}
          <div className="evidence-groups">
            {(Object.keys(GROUP_LABEL) as GroupKey[]).map((key) => {
              const groupItems = groups[key] ?? [];
              return (
                <section className="evidence-group" key={key}>
                  <div className="evidence-group-head">
                    <span className={`evidence-dot ${GROUP_LABEL[key]!.dot}`} />
                    <span>{GROUP_LABEL[key]!.title}</span>
                    <b>{groupItems.length}</b>
                  </div>
                  {groupItems.length ? (
                    <div className="evidence-table">
                      {groupItems.map((item) => (
                        <EvidenceRow item={item} expanded key={item.evidence_id} onOpen={onOpenEvidence} />
                      ))}
                    </div>
                  ) : (
                    <p className="evidence-group-empty">暂无该关系类型的证据。</p>
                  )}
                </section>
              );
            })}
          </div>
        </div>
      </section>

      {confirmRun && <ConfirmDialog title="确认推进研究" summary={confirmRun.user_question} diffs={[{ label: "研究状态", before: "待确认", after: "组织回答" }]} confirmLabel="确认推进" requireNote={false} busy={transitionBusy} error={transitionError} onConfirm={() => void confirmTransition(confirmRun)} onCancel={() => { if (!transitionBusy) setConfirmRun(null); }} />}

      {questionOpen && (
        <div className="confirm-overlay" onClick={() => { if (!submitBusy) setQuestionOpen(false); }}>
          <aside ref={questionDialog} className="invest-modal" role="dialog" aria-modal="true" aria-label="新建研究问题" onKeyDown={(event) => { if (event.key === "Escape" && !submitBusy) setQuestionOpen(false); }} onClick={(event) => event.stopPropagation()}>
            <header className="confirm-head">
              <span className="section-kicker">NEW RESEARCH RUN</span>
              <h3>新建研究问题</h3>
              <p>问题会进入研究链路：规划 → 证据收集 → 有据回答。</p>
            </header>
            <div className="invest-form">
              <label className="form-field">
                <span>你的问题</span>
                <textarea
                  value={questionText}
                  onChange={(event) => setQuestionText(event.target.value)}
                  rows={4}
                  placeholder="例如：我的持仓是否出现失效条件即将触发的信号？"
                />
              </label>
              {submitError && <p className="form-error" role="alert">{submitError}</p>}
              <footer className="form-foot">
                <button className="secondary-button" disabled={submitBusy} onClick={() => setQuestionOpen(false)}>取消</button>
                <button
                  className="primary-button"
                  disabled={submitBusy || !questionText.trim() || !aiReady}
                  title={aiReady ? undefined : "AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」"}
                  onClick={submitQuestion}
                >
                  {submitBusy ? "研究进行中…" : "开始研究"} <span>→</span>
                </button>
              </footer>
            </div>
          </aside>
        </div>
      )}

      {deleteRunId && runs.find((item) => item.run_id === deleteRunId) && (
        <ConfirmDialog
          title="删除这条研究问题"
          summary="研究问题及其回答会从本地研究列表移除，已入账的证据不会删除。"
          diffs={[{ label: "研究问题", before: runs.find((item) => item.run_id === deleteRunId)?.user_question ?? "", after: "删除" }]}
          confirmLabel="确认删除"
          requireNote={false}
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void handleDeleteRun(deleteRunId)}
          onCancel={() => {
            if (!deleteBusy) {
              setDeleteError(null);
              setDeleteRunId(null);
            }
          }}
        />
      )}
    </div>
  );
}

function RefChips({
  refs,
  evidenceOf,
  onOpenEvidence,
}: {
  refs: string[];
  evidenceOf: (id: string) => Evidence | undefined;
  onOpenEvidence: (evidenceId: string) => void;
}) {
  if (!refs.length) return <span className="answer-muted">无</span>;
  return (
    <div className="ref-chip-row">
      {refs.map((id) => {
        const item = evidenceOf(id);
        return (
          <EvidenceRef
            key={id}
            title={item ? `${id.slice(0, 8)} · ${item.source_name}` : `${id.slice(0, 8)}（未挂载）`}
            badge={<span className="soft-tag gray">{item?.relation ?? "?"}</span>}
            onOpen={() => onOpenEvidence(id)}
          />
        );
      })}
    </div>
  );
}

/** 导出供契约测试直接渲染（JV05 的 `ScanResultTable` 同例）。 */
export function AnswerDetail({
  response,
  evidenceOf,
  onOpenEvidence,
  onCardAction,
}: {
  response: AgentResponse;
  evidenceOf: (id: string) => Evidence | undefined;
  onOpenEvidence: (evidenceId: string) => void;
  onCardAction: (action: CardAction) => void;
}) {
  const refs = (ids: string[]) => (
    <RefChips refs={ids} evidenceOf={evidenceOf} onOpenEvidence={onOpenEvidence} />
  );

  return (
    <div className="answer-detail">
      <header className="answer-head">
        <div className="answer-title-row">
          <h3>{response.user_question}</h3>
          <ActionModeChip mode={response.action_mode} />
        </div>
        <p className="answer-summary">{response.summary}</p>
        <SourceLine
          sourceName="Steward 研究编排"
          modelProvider={response.model_provider ?? undefined}
          modelName={response.model_name ?? undefined}
          isLocalEngine={response.model_provider === "local-deterministic"}
          asOf={response.created_at}
        />
      </header>

    <div className="action-rail">
      <span className="answer-block-title">支持的下一步</span>
      <div className="action-rail-row">
        {(response.supported_actions ?? []).map((action) => (
          <button className="action-chip" key={action} onClick={() => onCardAction(action)}>
            {action.replace("_", " ")}
            {needsConfirmation(action) && <em>需确认</em>}
          </button>
        ))}
      </div>
    </div>

      <section className="answer-block">
        <div className="answer-block-title">推理路径</div>
        <ol className="answer-steps">
          {response.reasoning_outline.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </section>

      <section className="answer-block">
        <div className="answer-block-title">信心</div>
        <p className="answer-copy">{response.confidence_description}</p>
      </section>

      <section className="answer-block">
        <div className="answer-block-title">支持证据</div>
        {refs(response.supporting_refs)}
      </section>

      <section className="answer-block">
        <div className="answer-block-title">相反证据</div>
        {refs(response.contradicting_refs)}
      </section>

      <section className="answer-block warn">
        <div className="answer-block-title">缺失信息</div>
        <ul className="answer-list">
          {response.missing_information.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      {response.freshness_warning.length > 0 && (
        <section className="answer-block warn">
          <div className="answer-block-title">新鲜度警告</div>
          <ul className="answer-list">
            {response.freshness_warning.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}

      {response.limitations.length > 0 && (
        <section className="answer-block">
          <div className="answer-block-title">限制说明</div>
          <ul className="answer-list">
            {response.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}

      {/* JV07：路由结论单独成行——它说的是「这次跑了哪条链路」，与下面的 `action_mode`
          是两件事；`jev_route` 为 null（本轮没跑预判）时**零渲染**。 */}
      {response.jev_route && (
        <section className={`answer-block ${response.jev_route.bypassed_model ? "warn" : ""}`}>
          <div className="answer-block-title">本次链路</div>
          <p className="answer-route">
            {response.jev_route.bypassed_model
              ? `未调用完整研究链路（预判：${response.jev_route.mode_label}）`
              : `完整研究链路（预判：${response.jev_route.mode_label}）`}
            {response.jev_route.confidence !== null &&
              ` · 预判置信度 ${response.jev_route.confidence.toFixed(2)}`}
            {" · "}
            {response.jev_route.note}
          </p>
        </section>
      )}

      <section className="answer-meta">
        <span>run {response.run_id}</span>
        <span>回答于 {formatDate(response.created_at)}</span>
        <span>策略 {response.prompt_policy_version}</span>
        <span>{response.plugin_runs.length} 次插件调用</span>
        <span>{response.tool_calls.length} 次工具调用</span>
      </section>
    </div>
  );
}
