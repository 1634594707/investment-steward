import { useState } from "react";
import { Download, Plus } from "lucide-react";
import type { DecisionEntry, Evidence, InvestmentPolicyVersion, LearningActivity, Plan, WeeklyReview } from "@investment-steward/domain-contracts";
import { Metric } from "../components/kit";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EvidenceRef } from "../components/EvidenceRef";
import { formatDate } from "../state/format";
import type { AppView } from "../shell/nav";
import { useFocusTrap } from "../components/useFocusTrap";
import type { CoreRequest } from "../state/coreClient";
import { LongtermSection } from "./LongtermSection";
import { JudgmentHitRateSection } from "./JudgmentHitRateSection";
import "./review.css";

interface Props {
  decisions: DecisionEntry[];
  plans: Plan[];
  activities: LearningActivity[];
  review: WeeklyReview | null;
  /** 路线图 B3：记录决定时可勾选当时依据（证据账本），时间线可回链打开。 */
  evidence: Evidence[];
  onOpenEvidence: (evidenceId: string) => void;
  onNavigate: (view: AppView) => void;
  onAddDecision: (input: { theme: string; decision_summary: string; rationale: string; outcome: string; retrospective: string; linked_evidence_ids: string[]; plan_id: string | null }) => Promise<boolean>;
  /** 路线图 E2：到期补录决定的「结果 / 事后评价」（后端只允许补事后字段，原始判断不可改写）。 */
  onRecordOutcome: (decisionId: string, outcome: string, retrospective: string) => Promise<boolean>;
  onTransitionPlan: (planId: string, target: Plan["status"], confirmationSummary?: string) => Promise<boolean>;
  onDeleteActivity: (activityId: string) => Promise<boolean>;
  onExportReflections: () => Promise<LearningActivity[] | null>;
  onProposePolicyChange: (activityId: string) => Promise<InvestmentPolicyVersion | null>;
  onConfirmPolicy: (policyId: string, summary: string) => Promise<boolean>;
  /** 研究长期面板（阶段 8）：Core 请求通道;未传时面板降级为提示。 */
  coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }>;
}

const PLAN_STATUS_LABEL: Record<Plan["status"], string> = {
  planned: "计划中",
  active: "执行中",
  completed: "已完成",
  cancelled: "已取消",
};

export function ReviewPage({ decisions, plans, activities, review, evidence, onOpenEvidence, onNavigate, onAddDecision, onRecordOutcome, onTransitionPlan, onDeleteActivity, onExportReflections, onProposePolicyChange, onConfirmPolicy, coreRequest }: Props) {
  const activePlans = plans.filter((item) => item.status === "active");
  const plannedPlans = plans.filter((item) => item.status === "planned");
  const finishedPlans = plans.filter((item) => item.status === "completed" || item.status === "cancelled");
  const [period, setPeriod] = useState("30");
  const periodDecisions = decisions.filter((item) => {
    if (period === "all") return true;
    const age = Date.now() - new Date(item.made_at).getTime();
    return age >= 0 && age <= Number(period) * 86400000;
  }).sort((a, b) => new Date(b.made_at).getTime() - new Date(a.made_at).getTime());
  const reviewedCount = periodDecisions.filter((item) => item.outcome.trim() && item.retrospective.trim()).length;

  const [decisionOpen, setDecisionOpen] = useState(false);
  const [theme, setTheme] = useState("");
  const [summary, setSummary] = useState("");
  const [rationale, setRationale] = useState("");
  const [outcome, setOutcome] = useState("");
  const [retrospective, setRetrospective] = useState("");
  // 路线图 B3：决定与证据、计划的显式关联（契约字段 linked_evidence_ids / plan_id 此前无 UI 入口）。
  const [linkedEvidence, setLinkedEvidence] = useState<string[]>([]);
  const [linkedPlanId, setLinkedPlanId] = useState<string>("");
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  // 路线图 E2：结果补录 inline 状态（同一时间只编辑一条决定）。
  const [outcomeEditingId, setOutcomeEditingId] = useState<string | null>(null);
  const [outcomeDraft, setOutcomeDraft] = useState("");
  const [retroDraft, setRetroDraft] = useState("");
  const [outcomeBusy, setOutcomeBusy] = useState(false);
  const [outcomeError, setOutcomeError] = useState<string | null>(null);
  // 作出决定超过 7 天仍未记录结果 → 提示补录（先有结果，后验评估才可复算）。
  const pendingOutcomes = decisions.filter(
    (item) => !item.outcome.trim() && Date.now() - new Date(item.made_at).getTime() > 7 * 86400000,
  );

  async function saveOutcome(decisionId: string) {
    if (outcomeBusy || !outcomeDraft.trim()) return;
    setOutcomeBusy(true);
    setOutcomeError(null);
    const ok = await onRecordOutcome(decisionId, outcomeDraft.trim(), retroDraft.trim());
    setOutcomeBusy(false);
    if (ok) {
      setOutcomeEditingId(null);
      setOutcomeDraft("");
      setRetroDraft("");
    } else {
      setOutcomeError("结果补录失败，内容已保留，可重试。");
    }
  }
  const [planTarget, setPlanTarget] = useState<{ plan: Plan; target: Plan["status"] } | null>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [exportMsg, setExportMsg] = useState<string | null>(null);
  const [proposalBusy, setProposalBusy] = useState<string | null>(null);
  const [policyProposal, setPolicyProposal] = useState<InvestmentPolicyVersion | null>(null);
  const [deleteActivityId, setDeleteActivityId] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [policyBusy, setPolicyBusy] = useState(false);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const dialogRef = useFocusTrap<HTMLElement>(decisionOpen);

  async function saveDecision() {
    if (!theme.trim() || !summary.trim() || decisionBusy) return;
    setDecisionBusy(true);
    setDecisionError(null);
    try {
    const ok = await onAddDecision({
      theme: theme.trim(),
      decision_summary: summary.trim(),
      rationale: rationale.trim(),
      outcome: outcome.trim(),
      retrospective: retrospective.trim(),
      linked_evidence_ids: linkedEvidence,
      plan_id: linkedPlanId || null,
    });
    if (ok) {
      setDecisionOpen(false);
      setTheme(""); setSummary(""); setRationale(""); setOutcome(""); setRetrospective(""); setLinkedEvidence([]); setLinkedPlanId("");
    } else {
      throw new Error("Decision not saved");
    }
    } catch {
      setDecisionError("保存决定失败（Core 未就绪或字段格式非法），内容已保留，可重试。");
    } finally {
      setDecisionBusy(false);
    }
  }

  async function doTransitionPlan(confirmNote: string) {
    const pending = planTarget;
    if (!pending || planBusy) return;
    setPlanBusy(true);
    setPlanError(null);
    try {
      const ok = await onTransitionPlan(pending.plan.plan_id, pending.target, confirmNote);
      if (!ok) throw new Error("Plan not updated");
      setPlanTarget(null);
    } catch {
      setPlanError("计划状态变更失败，请检查状态后重试。");
    } finally {
      setPlanBusy(false);
    }
  }

  async function deleteActivity(activityId: string) {
    if (deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      if (!await onDeleteActivity(activityId)) throw new Error("Activity not deleted");
      setDeleteActivityId(null);
    } catch {
      setDeleteError("删除失败，反思记录仍保留，请重试。");
    } finally {
      setDeleteBusy(false);
    }
  }

  async function activatePolicy(note: string) {
    if (!policyProposal || policyBusy) return;
    setPolicyBusy(true);
    setPolicyError(null);
    try {
      if (!await onConfirmPolicy(policyProposal.policy_id, note)) throw new Error("Policy not confirmed");
      setPolicyProposal(null);
    } catch {
      setPolicyError("激活失败，草案已保留，请重试。");
    } finally {
      setPolicyBusy(false);
    }
  }

  async function proposePolicy(activityId: string) {
    if (proposalBusy) return;
    setProposalBusy(activityId);
    setPolicyError(null);
    try {
      const proposal = await onProposePolicyChange(activityId);
      if (!proposal) throw new Error("Proposal unavailable");
      setPolicyProposal(proposal);
    } catch {
      setPolicyError("原则草案生成失败，请重试。");
    } finally {
      setProposalBusy(null);
    }
  }

  async function handleExport() {
    if (exportBusy) return;
    setExportBusy(true);
    setExportMsg(null);
    try {
    const data = await onExportReflections();
    if (!data) {
      setExportMsg("导出失败：读取反思记录未能完成。");
      return;
    }
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `learning-reflections-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    setExportMsg(`已导出 ${data.length} 条反思`);
    } catch {
      setExportMsg("导出失败，反思记录仍保存在本机。");
    } finally {
      setExportBusy(false);
    }
  }

  return (
    <div className="page-stack review-page">
      <section className="review-heading">
        <div><span className="section-kicker">决定与学习</span><h2>决策复盘账本</h2><p>{review?.period_label ?? "暂无周期复盘"}</p></div>
        <label className="review-period">决定范围<select value={period} onChange={(event) => setPeriod(event.target.value)}><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="all">全部记录</option></select></label>
      </section>
      <section className="metric-row" aria-label="决定复盘进度">
        <Metric label="决定记录" value={periodDecisions.length} note={period === "all" ? "全部记录" : `最近 ${period} 天`} tone="blue" />
        <Metric label="已复盘决定" value={`${reviewedCount} / ${periodDecisions.length}`} note="已记录结果与事后评价" tone="mint" />
        <Metric label="待补充复盘" value={periodDecisions.length - reviewedCount} note="缺少结果或事后评价" tone="amber" />
      </section>

      {review && (
        <section className="weekly-review-band">
          <div className="section-heading">
            <div>
              <span className="eyebrow">WEEKLY REVIEW</span>
              <h3>{review.period_label}</h3>
            </div>
          </div>
          <div className="review-sections">
            {review.sections.map((section) => (
              <div className="review-section" key={section.key}>
                <span className="review-section-title">{section.title}</span>
                {section.lines.length ? (
                  <ul className="review-lines">
                    {section.lines.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="evidence-group-empty">暂无内容。</p>
                )}
              </div>
            ))}
            {review.principle_change_reason && (
              <p className="review-principle-note">{review.principle_change_reason}</p>
            )}
          </div>
        </section>
      )}

      <section className="timeline-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">DECISIONS</span>
            <h3>决定时间线</h3>
          </div>
          <div className="section-heading-actions">
            <button className="primary-button" onClick={() => setDecisionOpen(true)}>
              <Plus size={16} aria-hidden="true" />记录一次决定
            </button>
            <button className="text-button" onClick={() => onNavigate("research")}>
              去研究新问题 <span>→</span>
            </button>
          </div>
        </div>

        {pendingOutcomes.length > 0 && (
          <div className="review-plan-band" role="status">
            <span className="q-status amber" />
            <div>
              <strong>待补结果 · {pendingOutcomes.length} 条</strong>
              <span>决定作出已超过 7 天仍未记录结果。后验评估必须先有结果，且只补「结果 / 事后评价」，不改动原始判断。</span>
            </div>
          </div>
        )}
        {periodDecisions.length ? (
          <div className="decision-timeline">
            {periodDecisions.map((item) => (
              <article className="decision-card" key={item.decision_id}>
                <div className="decision-marker" aria-hidden="true" />
                <div className="decision-card-body">
                  <header className="decision-card-head">
                    <span className="decision-theme">{item.theme}</span>
                    <time dateTime={item.made_at}>{formatDate(item.made_at)}</time>
                  </header>
                  <h4>{item.decision_summary}</h4>
                  <div className="decision-detail-grid">
                    <div>
                      <span className="answer-block-title">当时依据</span>
                      <p>{item.rationale || "—"}</p>
                      {(item.linked_evidence_ids?.length ?? 0) > 0 && (
                        <div className="decision-evidence-refs">
                          {item.linked_evidence_ids.map((id) => {
                            const ev = evidence.find((candidate) => candidate.evidence_id === id);
                            return <EvidenceRef key={id} title={ev ? ev.summary : `证据 ${id.slice(0, 8)}`} onOpen={() => onOpenEvidence(id)} />;
                          })}
                        </div>
                      )}
                      {item.plan_id && (
                        <p className="decision-plan-ref">
                          关联计划：{plans.find((plan) => plan.plan_id === item.plan_id)?.title ?? item.plan_id.slice(0, 8)}
                        </p>
                      )}
                    </div>
                    <div>
                      <span className="answer-block-title">结果</span>
                      <p>{item.outcome || "—"}</p>
                      {outcomeEditingId === item.decision_id ? (
                        <div className="outcome-editor">
                          <textarea value={outcomeDraft} onChange={(event) => setOutcomeDraft(event.target.value)} rows={2} aria-label="补录结果" placeholder="实际发生了什么…" />
                          <textarea value={retroDraft} onChange={(event) => setRetroDraft(event.target.value)} rows={2} aria-label="补录事后评价" placeholder="判断偏差与下次调整（可选）…" />
                          {outcomeError && <p className="form-error" role="alert">{outcomeError}</p>}
                          <div className="plan-actions">
                            <button className="pbtn" disabled={outcomeBusy || !outcomeDraft.trim()} onClick={() => void saveOutcome(item.decision_id)}>{outcomeBusy ? "保存中…" : "保存补录"}</button>
                            <button className="pbtn" disabled={outcomeBusy} onClick={() => { setOutcomeEditingId(null); setOutcomeError(null); }}>取消</button>
                          </div>
                        </div>
                      ) : (
                        !item.outcome && (
                          <button className="pbtn" onClick={() => { setOutcomeEditingId(item.decision_id); setOutcomeDraft(""); setRetroDraft(""); setOutcomeError(null); }}>补结果</button>
                        )
                      )}
                    </div>
                    <div className="retro">
                      <span className="answer-block-title">事后评价</span>
                      <p>{item.retrospective || "—"}</p>
                    </div>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="evidence-group-empty">{decisions.length ? "所选时间范围内暂无决定。" : "暂无决定记录。"}</p>
        )}
      </section>

      <section className="plans-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">PLANS</span>
            <h3>执行计划</h3>
          </div>
        </div>
        {activePlans.map((activePlan) => (
          <div className="review-plan-band" key={activePlan.plan_id}>
            <span className="q-status mint" />
            <div>
              <strong>{PLAN_STATUS_LABEL[activePlan.status]}</strong>
              <span>{activePlan.title}</span>
            </div>
            <div className="plan-actions">
              <button className="pbtn" onClick={() => { setPlanError(null); setPlanTarget({ plan: activePlan, target: "completed" }); }}>完成</button>
              <button className="pbtn" onClick={() => { setPlanError(null); setPlanTarget({ plan: activePlan, target: "cancelled" }); }}>取消</button>
            </div>
          </div>
        ))}
        {plannedPlans.length > 0 && (
          <div className="plan-list">
            {plannedPlans.map((plan) => (
              <div className="plan-row" key={plan.plan_id}>
                <span className="q-status amber" />
                <span className="plan-row-title">{plan.title}</span>
                <button className="pbtn" onClick={() => { setPlanError(null); setPlanTarget({ plan, target: "active" }); }}>开始执行</button>
              </div>
            ))}
          </div>
        )}
        {!activePlans.length && plannedPlans.length === 0 && (
          <p className="evidence-group-empty">暂无进行中的计划。在研究页对一条回答可发起「创建计划」。</p>
        )}
        {finishedPlans.length > 0 && <details className="review-plan-history"><summary>已结束计划 · {finishedPlans.length}</summary>{finishedPlans.map((plan) => <div className="plan-row" key={plan.plan_id}><span className="plan-row-title">{plan.title}</span><span className="soft-tag gray">{PLAN_STATUS_LABEL[plan.status]}</span><time dateTime={plan.updated_at}>{formatDate(plan.updated_at)}</time></div>)}</details>}
      </section>

      <section className="learning-reflections">
        <div className="section-heading">
          <div>
            <span className="eyebrow">LEARNING LOG</span>
            <h3>学习活动与反思</h3>
          </div>
          <button className="text-button review-export" disabled={exportBusy} onClick={handleExport}>
            <Download size={16} aria-hidden="true" />{exportBusy ? "导出中…" : "导出反思"}
          </button>
        </div>
        {exportMsg && <p className="evidence-group-empty" role="status">{exportMsg}</p>}
        {policyError && !policyProposal && <p className="form-error" role="alert">{policyError}</p>}
        {activities.length ? (
          <div className="activity-list">
            {activities.map((item) => (
              <div className="activity-row" key={item.activity_id}>
                <div className="activity-copy">
                  <b>{item.objective}</b>
                  <small>{formatDate(item.completed_at)} · {item.unit_type}{item.bound_instrument ? ` · ${item.bound_instrument}` : ""}</small>
                  <dl className="reflection-detail"><div><dt>原判断</dt><dd>{item.user_answer || "未记录"}</dd></div><div><dt>反思与下次调整</dt><dd>{item.reflection || "未记录"}</dd></div></dl>
                </div>
                <div className="activity-actions">
                  {item.reflection && <button className="pbtn" disabled={proposalBusy !== null} onClick={() => void proposePolicy(item.activity_id)}>{proposalBusy === item.activity_id ? "生成中" : "提议原则变更"}</button>}
                  <button className="pbtn danger" onClick={() => { setDeleteError(null); setDeleteActivityId(item.activity_id); }}>删除</button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="evidence-group-empty">还没有学习活动记录。</p>
        )}
      </section>

      {decisionOpen && (
        <div className="confirm-overlay" onClick={() => { if (!decisionBusy) setDecisionOpen(false); }}>
          <aside ref={dialogRef} className="invest-modal wide" role="dialog" aria-modal="true" aria-label="记录一次决定" onKeyDown={(event) => { if (event.key === "Escape" && !decisionBusy) setDecisionOpen(false); }} onClick={(event) => event.stopPropagation()}>
            <header className="confirm-head">
              <span className="section-kicker">RECORD DECISION</span>
              <h3>记录一次决定</h3>
              <p>把当时的思考留档，复盘时不改写过去。</p>
            </header>
            <div className="invest-form">
              <div className="form-row">
                <label className="form-field">
                  <span>主题（必填）</span>
                  <input value={theme} onChange={(event) => setTheme(event.target.value)} placeholder="如：沪深 300 ETF" />
                </label>
              </div>
              <label className="form-field">
                <span>决定概要（必填）</span>
                <textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={2} placeholder="你做了什么决定…" />
              </label>
              <label className="form-field">
                <span>当时依据</span>
                <textarea value={rationale} onChange={(event) => setRationale(event.target.value)} rows={3} />
              </label>
              <label className="form-field">
                <span>结果</span>
                <textarea value={outcome} onChange={(event) => setOutcome(event.target.value)} rows={3} />
              </label>
              <label className="form-field">
                <span>事后评价</span>
                <textarea value={retrospective} onChange={(event) => setRetrospective(event.target.value)} rows={3} />
              </label>
              <div className="form-field">
                <span>当时依据（可选，勾选证据账本中的条目，复盘时可回链打开）</span>
                {evidence.length === 0 ? (
                  <p className="evidence-group-empty">证据账本为空：可先在投资页拉取证据或在研究页生成证据后再关联。</p>
                ) : (
                  <div className="decision-evidence-picker">
                    {evidence.slice(0, 12).map((item) => (
                      <button
                        key={item.evidence_id}
                        type="button"
                        className={`tag-button ${linkedEvidence.includes(item.evidence_id) ? "active" : ""}`}
                        aria-pressed={linkedEvidence.includes(item.evidence_id)}
                        title={`${item.source_name} · ${item.summary}`}
                        onClick={() => setLinkedEvidence((current) => (current.includes(item.evidence_id) ? current.filter((id) => id !== item.evidence_id) : [...current, item.evidence_id]))}
                      >
                        {item.summary.length > 18 ? `${item.summary.slice(0, 18)}…` : item.summary}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <label className="form-field">
                <span>关联执行计划（可选）</span>
                <select value={linkedPlanId} onChange={(event) => setLinkedPlanId(event.target.value)}>
                  <option value="">不关联</option>
                  {[...activePlans, ...plannedPlans].map((plan) => (
                    <option key={plan.plan_id} value={plan.plan_id}>{PLAN_STATUS_LABEL[plan.status]} · {plan.title}</option>
                  ))}
                </select>
              </label>
              <footer className="form-foot">
                <button className="secondary-button" disabled={decisionBusy} onClick={() => setDecisionOpen(false)}>取消</button>
                <button className="primary-button" disabled={decisionBusy || !theme.trim() || !summary.trim()} onClick={saveDecision}>
                  {decisionBusy ? "保存中…" : "保存决定"} <span>→</span>
                </button>
              </footer>
              {decisionError && <p className="form-error" role="alert">{decisionError}</p>}
            </div>
          </aside>
        </div>
      )}

      {planTarget && (
        <ConfirmDialog
          title={planTarget.target === "active" ? "开始执行计划" : planTarget.target === "completed" ? "完成计划" : "取消计划"}
          summary={planError ?? "计划状态变更将写入本地账本与审计记录。"}
          diffs={[{ label: "计划", before: planTarget.plan.title, after: `${planTarget.plan.title}（${PLAN_STATUS_LABEL[planTarget.target]}）` }]}
          confirmLabel={planBusy ? "保存中…" : "确认变更"}
          busy={planBusy}
          error={planError}
          onConfirm={doTransitionPlan}
          onCancel={() => setPlanTarget(null)}
        />
      )}
      {deleteActivityId && activities.find((item) => item.activity_id === deleteActivityId) && (
        <ConfirmDialog
          title="删除这条学习反思"
          summary="学习活动与反思会从本地复盘账本移除，已生成的原则草案不会被自动删除。"
          diffs={[{ label: "学习活动", before: activities.find((item) => item.activity_id === deleteActivityId)?.objective ?? "", after: "删除" }]}
          confirmLabel="确认删除"
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void deleteActivity(deleteActivityId)}
          onCancel={() => setDeleteActivityId(null)}
        />
      )}
      {policyProposal && (
        <ConfirmDialog
          title="确认激活原则草案"
          summary="学习反思只产生 DRAFT；确认后才会成为 active 原则，旧版本保留可追溯。"
          diffs={[{ label: "原则版本", before: "当前 active", after: `v${policyProposal.version}（草案）` }, { label: "变更依据", before: "未记录", after: policyProposal.change_reason }]}
          confirmLabel="确认激活"
          busy={policyBusy}
          error={policyError}
          onConfirm={activatePolicy}
          onCancel={() => setPolicyProposal(null)}
        />
      )}

      {/* G02：后验命中率（模型方案 × 主题 × 时间窗）。只读——命中率不自动改原则。 */}
      <JudgmentHitRateSection coreRequest={coreRequest} />

      <LongtermSection decisions={decisions} coreRequest={coreRequest} onNavigate={onNavigate} />
    </div>
  );
}
