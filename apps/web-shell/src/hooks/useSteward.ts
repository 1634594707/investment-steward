import { useRef, useState } from "react";
import type {
  AgentResponse,
  DecisionEntry,
  Evidence,
  Holding,
  InvestmentPolicyVersion,
  LearningActivity,
  LearningGoal,
  LearningUnit,
  Plan,
  ResearchRun,
  Thesis,
  TodayBrief,
  WeeklyReview,
} from "@investment-steward/domain-contracts";
import { classifyCoreError, detailOf, type CoreClient } from "../state/coreClient";
import { isDocumentVisible } from "../state/useCoreQuery";
import { toast } from "../state/toastStore";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：投资管家核心域数据，自 AppShell 原样下沉。
 * 政策 / 证据账本 / 持仓 / 投资逻辑 / 计划 / 决定 / 简报 / 周复盘 / 学习 / 研究运行。
 * loadAll 启动装载经同名 setter 回填；TodayPage / InvestmentPage / ResearchPage / ReviewPage
 * 的 props 仍由 AppShell 传递（解构同名，页面零改动）。
 */

/** 持有/证据统一的标的编码（如 CN:ETF:510300 → 510300），对齐行情/证据端点的 symbol。 */
function marketCode(instrument: string): string {
  return instrument.split(":").pop() ?? "";
}

export function useSteward(client: CoreClient, aiReady: boolean) {
  const [policy, setPolicy] = useState<InvestmentPolicyVersion | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [agentResponse, setAgentResponse] = useState<AgentResponse | null>(null);
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [holdings, setHoldings] = useState<Holding[]>([]);
  const [theses, setTheses] = useState<Thesis[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [decisions, setDecisions] = useState<DecisionEntry[]>([]);
  const [todayBrief, setTodayBrief] = useState<TodayBrief | null>(null);
  const [weeklyReview, setWeeklyReview] = useState<WeeklyReview | null>(null);
  const [learningUnit, setLearningUnit] = useState<LearningUnit | null>(null);
  const [learningGoal, setLearningGoal] = useState<LearningGoal | null>(null);
  const [activities, setActivities] = useState<LearningActivity[]>([]);
  const responseRequestSeq = useRef(0);

  /** 手动生成今日简报：POST /brief/today/generate（立即落库并返回，遵循「用户显式触发」边界）。 */
  async function generateTodayBrief(): Promise<TodayBrief | null> {
    const response = await client.request<TodayBrief>({ method: "POST", path: "/brief/today/generate" });
    if (response.status >= 400 || !response.data) return null;
    setTodayBrief(response.data);
    return response.data;
  }

  /** 证据时效巡检：POST /evidence/freshness/patrol（只审计标记过期，不改写账本）。 */
  async function patrolEvidenceFreshness(): Promise<{ checked: number; stale: number } | null> {
    const response = await client.request<{ checked: number; stale: number; stale_ids: string[] }>({ method: "POST", path: "/evidence/freshness/patrol" });
    return response.status < 400 ? response.data : null;
  }

  async function saveThesis(thesis: Thesis): Promise<boolean> {
    // F2-2 乐观更新：本地先行，Core 确认后以服务端记录对齐；失败回滚快照并 toast。
    const previous = theses;
    const optimisticId = thesis.thesis_id || `pending-thesis-${Date.now()}`;
    const optimistic: Thesis = { ...thesis, thesis_id: optimisticId };
    setTheses((current) => {
      const exists = current.some((item) => item.thesis_id === optimisticId);
      return exists ? current.map((item) => (item.thesis_id === optimisticId ? optimistic : item)) : [...current, optimistic];
    });
    const response = await client.request<Thesis>({
      method: thesis.thesis_id ? "PUT" : "POST",
      path: thesis.thesis_id ? `/thesis/${thesis.thesis_id}` : "/thesis",
      body: thesis,
    });
    if (response.status >= 400) {
      setTheses(previous);
      toast.err(`投资逻辑保存失败，本地已回滚（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    setTheses((current) => [...current.filter((item) => item.thesis_id !== optimisticId && item.thesis_id !== response.data.thesis_id), response.data]);
    toast.ok("投资逻辑已保存并写入本地账本。");
    return true;
  }

  async function createHolding(input: { instrument: string; label: string; status: "holding" | "watchlist"; strategy_note: string }): Promise<boolean> {
    const response = await client.request<Holding>({ method: "POST", path: "/holdings", body: input });
    if (response.status >= 400) {
      toast.err(`持仓登记失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    setHoldings((current) => [...current, response.data]);
    toast.ok(`已登记 ${response.data.label}（${response.data.status === "holding" ? "持仓" : "自选"}）。`);
    return true;
  }

  /** M5-F02：`note` 选填——有就随 DELETE 带上去（写成审计说明），没有则后端用固定句兜底。 */
  async function deleteHolding(holdingId: string, note?: string): Promise<boolean> {
    const query = note && note.trim() ? `?note=${encodeURIComponent(note.trim())}` : "";
    const response = await client.request<unknown>({ method: "DELETE", path: `/holdings/${holdingId}${query}` as `/${string}` });
    if (response.status >= 400) {
      toast.err("持仓删除失败，请稍后重试。");
      return false;
    }
    setHoldings((current) => current.filter((item) => item.holding_id !== holdingId));
    toast.ok("持仓已删除；历史证据与决策记录保留。");
    return true;
  }

  async function createThesis(input: { instrument: string; original_statement: string; core_assumptions: string[]; supporting_conditions: string[]; invalidation_conditions: string[]; observation_metrics: string[] }): Promise<boolean> {
    const response = await client.request<Thesis>({ method: "POST", path: "/thesis", body: input });
    if (response.status >= 400) {
      toast.err("投资逻辑创建失败，请稍后重试。");
      return false;
    }
    setTheses((current) => [...current, response.data]);
    toast.ok("投资逻辑已创建（草案状态，修改需显式确认）。");
    return true;
  }

  async function createPolicyDraft(input: object): Promise<InvestmentPolicyVersion | null> {
    const response = await client.request<InvestmentPolicyVersion>({ method: "POST", path: "/investment-policies", body: input });
    return response.status < 400 ? response.data : null;
  }

  async function confirmPolicy(policyId: string, summary: string): Promise<boolean> {
    const response = await client.request<InvestmentPolicyVersion>({
      method: "POST",
      path: `/investment-policies/${policyId}/confirm`,
      body: { confirmation_method: "explicit_ui", confirmation_summary: summary },
    });
    if (response.status >= 400) {
      toast.err(`原则确认失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    setPolicy(response.data);
    toast.ok(`投资原则 v${response.data.version} 已确认生效，确认说明进入审计。`);
    return true;
  }

  async function pullEvidence(
    instrument: string,
    kind: "announcements" | "news" | "financials",
  ): Promise<Evidence[] | null> {
    const response = await client.request<{ available: boolean; entries: Evidence[]; degraded_reason?: string }>({
      method: "GET",
      path: `/evidence/${kind}/${marketCode(instrument)}`,
    });
    if (response.status >= 400) return null;
    const entries = response.data.available && response.data.entries.length ? response.data.entries : [];
    if (entries.length) {
      // 拉取到的证据立即并入全局证据账本：各页计数、研究页证据流与证据抽屉同步可见。
      setEvidence((current) => {
        const known = new Set(current.map((item) => item.evidence_id));
        const fresh = entries.filter((item) => !known.has(item.evidence_id));
        return fresh.length ? [...fresh, ...current] : current;
      });
    }
    return entries;
  }

  /** v23 历史重复证据清理：按（类型+标题+发布日期）分组保留最早一条；有清理则刷新证据账本。 */
  async function dedupEvidence(): Promise<{ removed: number } | null> {
    const response = await client.request<{ ok: boolean; removed: number }>({
      method: "POST",
      path: "/evidence/dedup",
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    if (response.data.removed > 0) {
      const listResponse = await client.request<Evidence[]>({ method: "GET", path: "/evidence" });
      if (listResponse.status < 400 && listResponse.data) setEvidence(listResponse.data);
    }
    return { removed: response.data.removed };
  }

  async function submitLearningActivity(input: {
    unit_id: string;
    objective: string;
    bound_instrument?: string | null;
    bound_instrument_label?: string | null;
    user_answer: string;
    reflection: string;
  }): Promise<boolean> {
    // F2-2 乐观更新：目标进度本地先行，失败回滚。
    const previousGoal = learningGoal;
    if (learningGoal) {
      setLearningGoal({ ...learningGoal, completed_count: learningGoal.completed_count + 1, updated_at: new Date().toISOString() });
    }
    const response = await client.request<LearningActivity>({ method: "POST", path: "/learning/activities", body: input });
    if (response.status >= 400) {
      if (previousGoal) setLearningGoal(previousGoal);
      toast.err(`学习活动保存失败，进度已回滚（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    toast.ok("学习活动已保存；反思进入本机学习档案（学习不改投资原则）。");
    return true;
  }

  async function createResearchRun(question: string): Promise<AgentResponse | null> {
    if (!aiReady) {
      toast.err("AI 未接通：请先在 设置 → 模型配置 添加方案、粘贴密钥并设为「使用中」。");
      return null;
    }
    const runRes = await client.request<ResearchRun>({ method: "POST", path: "/research/runs", body: { user_question: question } });
    if (runRes.status >= 400) {
      toast.err(`研究创建失败（${classifyCoreError(runRes.status, detailOf(runRes.data)).message}）`);
      return null;
    }
    const run = runRes.data;
    setRuns((current) => [run, ...current]);
    // POST /run 同步执行整条链路（planning→collecting_evidence→analyzing→composing→completed）；
    // 期间轮询 run 状态让阶段实时可见，结束后回拉终态，修正列表中停留的「已创建」。
    const poll = setInterval(() => {
      if (!isDocumentVisible()) return; // B1：窗口隐藏（托盘常驻/切后台）时跳过本轮状态轮询。
      void client.request<ResearchRun>({ method: "GET", path: `/research/runs/${run.run_id}` }).then((res) => {
        if (res.status < 400) {
          setRuns((current) => current.map((item) => (item.run_id === run.run_id ? res.data : item)));
        }
      });
    }, 1500);
    try {
      const runResponse = await client.request<AgentResponse>({ method: "POST", path: `/research/runs/${run.run_id}/run` });
      if (runResponse.status >= 400) {
        toast.err("研究链路执行失败：本次不生成任何回答（不编造）。");
        return null;
      }
      setAgentResponse(runResponse.data);
      return runResponse.data;
    } finally {
      clearInterval(poll);
      const finalRes = await client.request<ResearchRun>({ method: "GET", path: `/research/runs/${run.run_id}` });
      if (finalRes.status < 400) {
        setRuns((current) => current.map((item) => (item.run_id === run.run_id ? finalRes.data : item)));
      }
    }
  }

  async function loadRunResponse(runId: string): Promise<AgentResponse | null> {
    const requestSeq = ++responseRequestSeq.current;
    setAgentResponse(null);
    const response = await client.request<AgentResponse | null>({ method: "GET", path: `/research/runs/${runId}/response` });
    if (requestSeq !== responseRequestSeq.current) return null;
    if (response.status >= 400 || !response.data) return null;
    setAgentResponse(response.data);
    return response.data;
  }

  async function transitionRun(runId: string, target: string): Promise<boolean> {
    const response = await client.request<ResearchRun>({ method: "POST", path: `/research/runs/${runId}/transition`, body: { target } });
    if (response.status >= 400) return false;
    setRuns((current) => current.map((item) => (item.run_id === runId ? response.data : item)));
    return true;
  }

  async function deleteResearchRun(runId: string): Promise<boolean> {
    ++responseRequestSeq.current;
    const response = await client.request<unknown>({ method: "DELETE", path: `/research/runs/${runId}` });
    if (response.status >= 400) return false;
    setRuns((current) => current.filter((item) => item.run_id !== runId));
    setAgentResponse((current) => (current?.run_id === runId ? null : current));
    return true;
  }

  async function createDecision(input: { theme: string; decision_summary: string; rationale: string; outcome: string; retrospective: string; linked_evidence_ids?: string[]; plan_id?: string | null }): Promise<boolean> {
    const response = await client.request<DecisionEntry>({ method: "POST", path: "/decisions", body: input });
    if (response.status >= 400) {
      toast.err("决定记录保存失败，请稍后重试。");
      return false;
    }
    setDecisions((current) => [response.data, ...current]);
    toast.ok("决定已记录；结果与事后评价进入复盘时间线。");
    return true;
  }

  /** 路线图 E2 研究后验最小闭环：到期补录决定的「结果 / 事后评价」（原始判断不可改写）。 */
  async function recordDecisionOutcome(decisionId: string, outcome: string, retrospective: string): Promise<boolean> {
    const response = await client.request<DecisionEntry>({
      method: "POST",
      path: "/evidence/decision-outcome",
      body: { decision_id: decisionId, outcome, retrospective },
    });
    if (response.status >= 400) {
      toast.err(`结果补录失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    setDecisions((current) => current.map((item) => (item.decision_id === decisionId ? response.data : item)));
    toast.ok("结果已补录；原始判断保持不变。");
    return true;
  }

  async function transitionPlan(planId: string, target: Plan["status"], confirmationSummary?: string): Promise<boolean> {
    const response = await client.request<Plan>({
      method: "POST",
      path: `/plans/${planId}/transition`,
      body: { target, ...(confirmationSummary ? { confirmation_summary: confirmationSummary } : {}) },
    });
    if (response.status >= 400) {
      toast.err(`计划状态切换失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      return false;
    }
    setPlans((current) => current.map((item) => (item.plan_id === planId ? response.data : item)));
    toast.ok(`计划已切换到「${target}」。`);
    return true;
  }

  async function deleteLearningActivity(activityId: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "DELETE", path: `/learning/activities/${activityId}` });
    if (response.status >= 400) {
      toast.err("学习活动删除失败，请稍后重试。");
      return false;
    }
    setActivities((current) => current.filter((item) => item.activity_id !== activityId));
    toast.ok("学习活动已删除。");
    return true;
  }

  async function proposePolicyChange(activityId: string): Promise<InvestmentPolicyVersion | null> {
    const response = await client.request<InvestmentPolicyVersion>({
      method: "POST",
      path: `/learning/activities/${activityId}/propose-policy-change`,
    });
    return response.status < 400 ? response.data : null;
  }

  async function exportReflections(): Promise<LearningActivity[] | null> {
    const response = await client.request<LearningActivity[]>({ method: "GET", path: "/learning/reflections/export" });
    return response.status < 400 ? response.data : null;
  }

  return {
    policy, setPolicy,
    evidence, setEvidence,
    agentResponse, setAgentResponse,
    runs, setRuns,
    holdings, setHoldings,
    theses, setTheses,
    plans, setPlans,
    decisions, setDecisions,
    todayBrief, setTodayBrief,
    weeklyReview, setWeeklyReview,
    learningUnit, setLearningUnit,
    learningGoal, setLearningGoal,
    activities, setActivities,
    generateTodayBrief, patrolEvidenceFreshness,
    saveThesis, createThesis, createPolicyDraft, confirmPolicy,
    createHolding, deleteHolding,
    pullEvidence, dedupEvidence,
    submitLearningActivity, deleteLearningActivity, proposePolicyChange, exportReflections,
    createResearchRun, loadRunResponse, transitionRun, deleteResearchRun,
    createDecision, recordDecisionOutcome, transitionPlan,
  };
}
