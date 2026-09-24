import type { Evidence, LearningGoal, LearningUnit, Notification, TodayBrief } from "@investment-steward/domain-contracts";
import { useState } from "react";
import { ActionItem, Metric, SectionHeading } from "../components/kit";
import { EvidenceRow } from "../components/EvidenceRow";
import { CardSlot } from "../cards/CardSlot";
import { renderCard } from "../cards/renderers";
import type { AppView } from "../shell/nav";
import { IconRefresh } from "../shell/icons";
import { DemoNotice } from "../components/DemoNotice";

interface Props {
  counts: { evidence: number; open: number; learning: number };
  evidence: Evidence[];
  brief: TodayBrief | null;
  notifications: Notification[];
  learningUnit: LearningUnit | null;
  learningGoal: LearningGoal | null;
  /** 路线图 A1：演示模式明示（内容为界面示例，刷新即还原、不落库）。 */
  isDemo: boolean;
  onNavigate: (view: AppView) => void;
  onOpenEvidence: (evidenceId: string) => void;
  onMarkNotificationRead: (notificationId: string) => Promise<boolean>;
  onEvaluateNotifications: () => Promise<boolean>;
  onSubmitLearning: (input: {
    unit_id: string;
    objective: string;
    bound_instrument?: string | null;
    bound_instrument_label?: string | null;
    user_answer: string;
    reflection: string;
  }) => Promise<boolean>;
  /** 手动生成今日简报（POST /brief/today/generate，立即落库）。 */
  onGenerateBrief: () => Promise<TodayBrief | null>;
  /** 上手三步引导数据源（原则/持仓/研究问题是否已建立）。 */
  onboarding: { hasPolicy: boolean; holdings: number; research: number };
}

const SIGNAL_TONE: Record<string, "mint" | "amber" | "blue"> = {
  observe: "mint",
  research: "amber",
  review_plan: "blue",
};

/**
 * D01（前端设计与架构优化任务路线图 2026-09-19）：简报条目的处理动作。
 * `BriefItem.signal` 与通知行的 `action_mode` 同一套语义（observe/research/review_plan），
 * 按钮名称写明点击后的去处；无对应处理的 signal（如 no_action）只给「查看依据」，不猜去处。
 */
const BRIEF_ACTION: Record<string, { label: string; view: AppView }> = {
  observe: { label: "看持仓与逻辑", view: "investment" },
  research: { label: "去研究", view: "research" },
  review_plan: { label: "去复核", view: "review" },
};

export function TodayPage({ counts, evidence, brief, notifications, learningUnit, learningGoal, isDemo, onNavigate, onOpenEvidence, onMarkNotificationRead, onEvaluateNotifications, onSubmitLearning, onGenerateBrief, onboarding }: Props) {
  const hasBrief = Boolean(brief && brief.has_personalization);
  // 上手三步：把散落在三个页面的空态入口收成一条进度引导（全部完成即隐藏）
  const steps = [
    { key: "policy", label: "① 建立投资原则", done: onboarding.hasPolicy, view: "investment" as AppView },
    { key: "holding", label: "② 录入第一个标的", done: onboarding.holdings > 0, view: "investment" as AppView },
    { key: "research", label: "③ 提出第一个研究问题", done: onboarding.research > 0, view: "research" as AppView },
  ];
  const doneCount = steps.filter((step) => step.done).length;
  const goalProgress = learningGoal
    ? `${String(learningGoal.completed_count).padStart(2, "0")} / ${String(learningGoal.target_count).padStart(2, "0")}`
    : "--";
  const [answer, setAnswer] = useState("");
  const [reflection, setReflection] = useState("");
  const [saved, setSaved] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);

  async function handleGenerateBrief() {
    if (briefBusy) return;
    setBriefBusy(true);
    setBriefError(null);
    const result = await onGenerateBrief();
    setBriefBusy(false);
    if (!result) setBriefError("简报生成失败（Core 未就绪或证据不足）");
  }

  async function handleSubmitLearning() {
    if (!learningUnit || submitting) return;
    setSubmitting(true);
    const ok = await onSubmitLearning({
      unit_id: learningUnit.unit_id,
      objective: learningUnit.objective,
      bound_instrument: learningUnit.bound_instrument,
      bound_instrument_label: learningUnit.bound_instrument_label,
      user_answer: answer,
      reflection,
    });
    setSubmitting(false);
    if (ok) {
      setSaved(true);
      setAnswer("");
      setReflection("");
    }
  }

  return (
    <div className="page-stack today-page">
      {isDemo && <DemoNotice context="今日简报 / 通知 / 学习" />}
      <section className="decision-header" aria-label="今日决策概览">
        <div>
          <time className="section-kicker" dateTime={new Date().toLocaleDateString("en-CA")}>{new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" })}</time>
          <h2>{hasBrief ? brief!.headline : "今日决策台"}</h2>
          <p>{hasBrief ? brief!.items[0]?.summary ?? "今日简报已更新" : brief?.empty_reason ?? "今日简报尚未生成"}</p>
          <div className="decision-context">
            <span>{onboarding.holdings} 个持仓标的</span>
            <span>{onboarding.hasPolicy ? "已建立投资原则" : "待建立投资原则"}</span>
          </div>
        </div>
        <div className="decision-command">
          <button className="primary-button" onClick={handleGenerateBrief} disabled={briefBusy}>
            <IconRefresh className={briefBusy ? "spin" : ""} />{briefBusy ? "生成中…" : "生成今日简报"}
          </button>
          {briefError && <span className="probe-note err" role="alert">{briefError}</span>}
        </div>
      </section>
      {doneCount < steps.length && (
        <section className="onboard-strip" aria-label="上手引导">
          <span className="onboard-label">上手三步 · {doneCount}/{steps.length}</span>
          <div className="onboard-steps">
            {steps.map((step) => (
              <button
                key={step.key}
                className={`onboard-step ${step.done ? "done" : "todo"}`}
                onClick={() => onNavigate(step.view)}
                aria-label={`${step.label}${step.done ? "（已完成）" : "（去完成）"}`}
              >
                {step.label}{step.done ? " · 已完成" : " →"}
              </button>
            ))}
          </div>
        </section>
      )}
      <section className="metric-row">
        <Metric label="待处理" value={counts.open} note={notifications.length ? "由已确认条件与新证据触发" : "暂无触发条件"} tone="amber" />
        <Metric label="新证据" value={counts.evidence} note="最近 24 小时" tone="mint" />
        <Metric label="学习进度" value={goalProgress} note={learningGoal ? `本周目标 ${learningGoal.completed_count}/${learningGoal.target_count}` : "本周目标 --"} tone="blue" />
      </section>
      <section className="content-grid">
        <div className="section-block">
          <SectionHeading eyebrow="需要你的判断" title="优先处理" action={hasBrief ? "查看全部" : undefined} onClick={() => onNavigate("research")} />
          {hasBrief ? (
            <div className="action-list">
              {brief!.items.map((item) => (
                <ActionItem
                  key={item.display_order}
                  number={String(item.display_order + 1).padStart(2, "0")}
                  title={item.title}
                  desc={item.summary}
                  tag={item.signal}
                  color={SIGNAL_TONE[item.signal] ?? "mint"}
                  meta={<>
                    <span>{item.evidence_refs.length ? `依据 ${item.evidence_refs.length} 条` : "本条未附证据引用"}</span>
                    {item.data_time && <span title={item.data_time}>数据截至 {item.data_time.slice(0, 10)}</span>}
                    {item.related_instrument && <span>标的 {item.related_instrument}</span>}
                  </>}
                  actions={<>
                    {item.evidence_refs.length > 0 ? (
                      <button className="text-btn" onClick={() => onOpenEvidence(item.evidence_refs[0]!)}>
                        {item.evidence_refs.length > 1 ? `查看依据（${item.evidence_refs.length} 条，打开第 1 条）` : "查看依据"}
                      </button>
                    ) : (
                      <span className="action-item-missing">暂无依据可查</span>
                    )}
                    {BRIEF_ACTION[item.signal] && (
                      <button className="text-btn" onClick={() => onNavigate(BRIEF_ACTION[item.signal]!.view)}>
                        {BRIEF_ACTION[item.signal]!.label}
                      </button>
                    )}
                  </>}
                />
              ))}
            </div>
          ) : (
            <p className="evidence-group-empty">{brief?.empty_reason ?? "今日简报尚未生成。日报由本地证据账本生成，缺依据时不制造个性化。"}</p>
          )}
        </div>
        <div className="section-block evidence-column">
          <SectionHeading eyebrow="最新证据" title="证据与来源" action="打开研究" onClick={() => onNavigate("research")} />
          {evidence.length === 0 && <p className="evidence-group-empty">暂无证据记录</p>}
          {evidence.slice(0, 2).map((item) => (
            <EvidenceRow item={item} key={item.evidence_id} onOpen={onOpenEvidence} />
          ))}
        </div>
      </section>
      <CardSlot slot="today.brief">
        <div className="renderer-demo">
          {notifications.map((notice) => (
            <div key={notice.notification_id}>
              {renderCard({
                card_id: `notification-${notice.notification_id}`,
                title: notice.title,
                summary: notice.summary,
                severity: notice.action_mode === "research" ? "attention" : "info",
                evidence_refs: notice.evidence_refs,
                // 通知由内核条件引擎产生（非插件输出），来源如实标注为内核，不伪造插件来源。
                source_plugin: null,
                created_at: notice.created_at,
                action_mode: notice.action_mode,
                renderer: "card",
                limitations: [
                  `由已确认条件触发：${notice.triggered_by}`,
                  notice.delivery_status === "failed" ? `投递失败（第 ${notice.delivery_attempts ?? 0} 次）：${notice.last_delivery_error ?? "可重试"}` : "",
                ].filter(Boolean),
              })}
              <div className="notification-actions">
                {notice.evidence_refs[0] && <button className="text-btn" onClick={() => onOpenEvidence(notice.evidence_refs[0]!)}>查看证据</button>}
                {notice.action_mode === "research" && <button className="text-btn" onClick={() => onNavigate("research")}>去研究</button>}
                {notice.action_mode === "review_plan" && <button className="text-btn" onClick={() => onNavigate("review")}>去复核</button>}
                {notice.delivery_status === "failed" && <button className="text-btn" onClick={() => void onEvaluateNotifications()}>重试投递</button>}
                <button className="text-btn" onClick={() => void onMarkNotificationRead(notice.notification_id)}>标记已读</button>
              </div>
            </div>
          ))}
          {notifications.length === 0 && brief !== null && brief.items.length === 0 && (
            <p className="evidence-group-empty">{brief.empty_reason ?? "当前没有与已确认条件相匹配的新通知。"}</p>
          )}
        </div>
      </CardSlot>
      <CardSlot slot="today.learning">
        {learningUnit ? (
          <div className="learning-card">
            <div className="learning-card-head">
              <strong>{learningUnit.title}</strong>
              <span className="learning-source">
                来源 · {learningUnit.source_plugin_id} · {learningUnit.source_plugin_release}
                <span className="patch-tag ml-6">本地引擎</span>
              </span>
            </div>
            {learningUnit.bound_instrument_label && (
              <div className="learning-bound">
                绑定标的：{learningUnit.bound_instrument_label}（{learningUnit.bound_instrument}）
              </div>
            )}
            <p className="learning-objective">{learningUnit.objective}</p>
            <p className="learning-content">{learningUnit.content}</p>
            {learningUnit.related_conditions.length > 0 && (
              <ul className="learning-conditions">
                {learningUnit.related_conditions.map((condition) => (
                  <li key={condition}>{condition}</li>
                ))}
              </ul>
            )}
            <p className="learning-guidance">{learningUnit.guidance}</p>
            {saved ? (
              <p className="learning-saved">已保存本次练习与反思，目标进度已更新。</p>
            ) : (
              <div className="learning-form">
                <label>
                  <span>你的回答</span>
                  <textarea
                    value={answer}
                    onChange={(event) => setAnswer(event.target.value)}
                    placeholder="用自己的话重述条件并给出判断…"
                    rows={3}
                  />
                </label>
                <label>
                  <span>反思（可导出）</span>
                  <textarea
                    value={reflection}
                    onChange={(event) => setReflection(event.target.value)}
                    placeholder="记录你学到或想修正的地方…"
                    rows={3}
                  />
                </label>
                <button className="app-btn" onClick={handleSubmitLearning} disabled={submitting || !answer.trim()}>
                  {submitting ? "保存中…" : "保存练习与反思"}
                </button>
              </div>
            )}
          </div>
        ) : undefined}
      </CardSlot>
    </div>
  );
}
