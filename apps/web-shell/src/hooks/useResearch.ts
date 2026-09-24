import { useRef, useState } from "react";
import type { CoreClient } from "../state/coreClient";
import { detailOf } from "../state/coreClient";
import type { StockReportResult } from "../routes/workbench/reportErrors";
import type {
  AiReportItem,
  StockReport,
  DirectionReport,
  CompareSynthesis,
  CollabRunView,
  ReviewQueue,
  AnalysisTurn,
} from "../routes/workbench/researchTypes";

/** 后端 ok=false 时的失败负载形状（与 StockReport 互斥，见 B02（2026-09-15 方向研判路线图））。 */
interface StockReportFailureBody {
  ok: false;
  stage?: string;
  detail?: string;
  source_errors?: Record<string, string>;
}

/** v32 追问补充材料（用户粘贴的外部信息；后端一律按「未独立验证」处理）。 */
export interface FollowUpSupplementInput {
  text: string;
  source_name?: string;
  url?: string;
  event_date?: string;
}

/** B01（桌面端升级路线图 2026-09-18）：档案列表筛选/分页参数（服务端过滤；缺省字段不传）。 */
export interface AiReportListParams {
  kind?: "direction" | "stock" | null;
  /** stock 内是否「协同流水线」；null=不区分（与后端 collab 三态对齐）。 */
  collab?: boolean | null;
  model?: string | null;
  q?: string | null;
  /** true=仅草稿 / false=仅正式 / null=不过滤。 */
  isDraft?: boolean | null;
  /** J06：`below_min` = 正文低于所选档位字数下限（「未达档位深度」一档，服务端过滤）。 */
  depth?: "below_min" | null;
  /** ISO 时间下界（含）；对应后端 created_at（写入时的 generated_at）文本比较。 */
  generatedFrom?: string | null;
  /** 从 0 起的页偏移。 */
  offset?: number;
  limit?: number;
  countOnly?: boolean;
}

/** B01：全局真值计数（不受筛选影响，来自 COUNT(*) 口径）与模型清单。 */
export interface AiReportFacets {
  counts: { total: number; direction: number; stock: number; collab: number; draft_stock: number };
  models: string[];
}

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：AI 研究产出领域数据，自 AppShell 原样下沉。
 * ResearchWorkbenchPage 直接消费本钩子；跨页联动（标的池送战法雷达 / 战法雷达跳回预填）
 * 仍由 AppShell 承担。产出全部落库（仅本机），列表/详情走落库 payload。
 */
export function useResearch(client: CoreClient) {
  // v22 AI 研究产出历史（方向研判 / 个股研报落库），研究工作台页消费。
  // B01：列表改为服务端筛选分页的轻量投影；真值计数与模型清单随同一次请求返回。
  const [aiReports, setAiReports] = useState<AiReportItem[]>([]);
  const [aiReportsTotal, setAiReportsTotal] = useState(0);
  const [aiReportFacets, setAiReportFacets] = useState<AiReportFacets>({
    counts: { total: 0, direction: 0, stock: 0, collab: 0, draft_stock: 0 },
    models: [],
  });
  // 记住最近一次使用的筛选参数：生成/删除成功后的自动刷新沿用同一视图（不带参调用时）。
  const lastParamsRef = useRef<AiReportListParams | null>(null);
  // v27 待复盘验证点队列（由已落库研报派生），研究工作台页消费。
  // G01（桌面端升级路线图 2026-09-18）：服务端在读路径上把带确切日期的验证点**幂等**落成
  // `verifiable_judgments`，因此本队列自 G01 起可写——回填走 `verifyJudgment`（复用既有 verify 通道）。
  const [researchReviewQueue, setResearchReviewQueue] = useState<ReviewQueue | null>(null);

  /** v21 AI 个股研究报告：手动选股 → 新闻/公告/财报 + K 线 → 模型即时报告（v22 落库 + 可调回看区间）。
   * v29：mode = 研报字数预算档（quick/standard/deep），后端做软告警；不传走后端默认 standard。
   * B02（2026-09-15 路线图）：不再把失败转 null——成功返回 {ok:true, report}，
   * 失败返回 {ok:false, failure:{status, stage, detail, sourceErrors}}，由调用方如实呈现。
   * D02（桌面端升级路线图 2026-09-18）：末位增 signal（C01 请求层）——批量任务取消时中止在途请求。 */
  async function generateStockReport(symbol: string, question: string, barsLimit: number, profileId?: string | null, mode?: string, signal?: AbortSignal): Promise<StockReportResult> {
    let response;
    try {
      response = await client.request<StockReport | StockReportFailureBody>({
        method: "POST",
        path: "/evidence/stock-research-report",
        body: { symbol, question, bars_limit: barsLimit, ...(profileId ? { profile_id: profileId } : {}), ...(mode ? { mode } : {}) },
        signal,
      });
    } catch (error) {
      return { ok: false, failure: { status: 502, stage: null, detail: error instanceof Error ? error.message : "报告生成请求失败", sourceErrors: [] } };
    }
    if (response.status >= 400) {
      return {
        ok: false,
        failure: {
          status: response.status,
          stage: null,
          detail: detailOf(response.data) ?? `Core 返回错误（HTTP ${response.status}）`,
          sourceErrors: [],
        },
      };
    }
    const data = response.data;
    if (!data) {
      return { ok: false, failure: { status: response.status, stage: null, detail: "Core 返回空响应，无法确认生成结果", sourceErrors: [] } };
    }
    if (!data.ok) {
      const failure = data as StockReportFailureBody;
      return {
        ok: false,
        failure: {
          status: response.status,
          stage: failure.stage ?? null,
          detail: failure.detail ?? "生成失败（后端未给出具体原因）",
          sourceErrors: Object.values(failure.source_errors ?? {}),
        },
      };
    }
    return { ok: true, report: data };
  }

  /** v21 AI 方向研判；v33（2026-09-14 路线图 C02/B08）：profileId 为**本次请求**指定模型
   * （缺省 undefined = 跟随全局「使用中」方案），mode 为方向独立篇幅档（quick/standard/deep）。
   * D02：末位增 signal（C01 请求层）——任务中心「停止」即中止在途 POST。 */
  async function generateDirectionReport(topic: string, question: string, profileId?: string | null, mode?: string, signal?: AbortSignal): Promise<DirectionReport | null> {
    const response = await client.request<DirectionReport>({
      method: "POST",
      path: "/evidence/direction-research",
      body: {
        topic,
        question,
        ...(profileId ? { profile_id: profileId } : {}),
        ...(mode ? { mode } : {}),
      },
      signal,
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return response.data;
  }

  /** v23 阶段 0：多模型报告跨报告综合（共识/分歧/待核验，分歧如实陈列）。 */
  async function synthesizeCompareReports(
    reports: { symbol: string; model: string; confidence: string; executive_summary: string; report: string }[],
    question: string,
  ): Promise<CompareSynthesis | null> {
    const response = await client.request<CompareSynthesis>({ method: "POST", path: "/evidence/compare-synthesis", body: { reports, question } });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return response.data;
  }

  /** v23 协同流水线：创建运行（定稿证据包，角色随后逐个推进；stageProfiles=角色→方案 id）。 */
  async function createCollabRun(symbol: string, question: string, barsLimit: number, stageProfiles?: Record<string, string> | null): Promise<CollabRunView | null> {
    const response = await client.request<{ ok: boolean; run: CollabRunView }>({
      method: "POST",
      path: "/evidence/collab-run",
      body: { symbol, question, bars_limit: barsLimit, ...(stageProfiles && Object.keys(stageProfiles).length ? { stage_profiles: stageProfiles } : {}) },
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return response.data.run;
  }

  /** v23 协同流水线：执行下一个角色（严格串行：一次调一个；失败阶段再次调用即重试）。
   * executedStatus="failed" 时调用方必须停止循环并把失败原因交给用户（2026-09-10 真机教训：
   * 失败后继续自动重试会把 collabBusy 永久锁死，用户连手动重试都点不了）。
   * collabReportId = 修订稿完成落库后的报告 id（v32：追问附录入口需要）。 */
  async function runNextCollabStage(runId: string): Promise<{ done: boolean; run: CollabRunView; executedStage: string; executedStatus: string; collabReportId?: string } | null> {
    const response = await client.request<{ ok: boolean; done: boolean; executed_stage: string; executed_status: string; run: CollabRunView; collab_report_id?: string }>({
      method: "POST",
      path: "/evidence/collab-next",
      body: { run_id: runId },
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return { done: response.data.done, run: response.data.run, executedStage: response.data.executed_stage, executedStatus: response.data.executed_status, collabReportId: response.data.collab_report_id };
  }

  // v22 AI 研究产出持久化：列表 / 详情查看走落库 payload，删除即物理删除（仅本机）。
  // B01：params 缺省时沿用最近一次筛选（生成/删除后的自动刷新不丢当前视图）。
  async function loadAiReports(params?: AiReportListParams): Promise<void> {
    const effective = params ?? lastParamsRef.current ?? {};
    lastParamsRef.current = effective;
    const query = new URLSearchParams();
    if (effective.kind) query.set("kind", effective.kind);
    if (effective.collab === true) query.set("collab", "true");
    else if (effective.collab === false) query.set("collab", "false");
    if (effective.model) query.set("model", effective.model);
    const q = effective.q?.trim();
    if (q) query.set("q", q);
    if (effective.isDraft === true) query.set("is_draft", "true");
    else if (effective.isDraft === false) query.set("is_draft", "false");
    if (effective.depth) query.set("depth", effective.depth);
    if (effective.generatedFrom) query.set("generated_from", effective.generatedFrom);
    if (typeof effective.offset === "number") query.set("offset", String(effective.offset));
    if (typeof effective.limit === "number") query.set("limit", String(effective.limit));
    if (effective.countOnly) query.set("count_only", "true");
    const suffix = query.toString();
    const response = await client.request<{
      ok: boolean;
      items: AiReportItem[];
      total: number;
      counts: AiReportFacets["counts"];
      models: string[];
    }>({
      method: "GET",
      path: `/ai-research/reports${suffix ? `?${suffix}` : ""}`,
    });
    if (response.status < 400 && response.data?.ok) {
      setAiReports(response.data.items);
      setAiReportsTotal(response.data.total);
      setAiReportFacets({ counts: response.data.counts, models: response.data.models });
    }
    void loadResearchReviewQueue();
  }

  /** B01：按 id 取完整落库 payload（列表是轻量投影，回看全文走这里）。 */
  async function loadAiReportDetail(reportId: string): Promise<AiReportItem | null> {
    const response = await client.request<{ ok: boolean; item: AiReportItem }>({
      method: "GET",
      path: `/ai-research/reports/${reportId}`,
    });
    if (response.status >= 400 || !response.data?.ok) return null;
    return response.data.item;
  }

  /** v27 待复盘验证点队列：与历史产出同源（都由已落库研报派生），一起刷新即可保持一致。
   * G01：端点会把带确切日期的验证点幂等落成可回填判断（不改写原研报），刷新即可看到新落库的
   * `judgment_id`（幂等派生自 report_id + 序号，重复刷新不会长出重复记录）。 */
  async function loadResearchReviewQueue(): Promise<ReviewQueue | null> {
    const response = await client.request<ReviewQueue>({ method: "GET", path: "/ai-research/review-queue" });
    if (response.status < 400 && response.data?.ok) {
      setResearchReviewQueue(response.data);
      return response.data;
    }
    return null;
  }

  /** G01：回填来源研报的判断（成立/失效/无数据）。复用既有 `POST /judgments/{id}/verify`——
   * 只**追加**验证结果并推状态，判断原文与研报 payload 零改动（append-only，与 report_quality 既有约定一致）。
   * 失败如实返回错误文案（不吞），由调用方展示；成功后刷新队列以带回新状态与闭环计数。 */
  async function verifyJudgment(judgmentId: string, result: "verified" | "refuted" | "insufficient_data", outcome = ""): Promise<string | null> {
    const response = await client.request<{ judgment: { status?: string }; verification: { result?: string } }>({
      method: "POST",
      path: `/judgments/${judgmentId}/verify`,
      body: { result, outcome: outcome.trim() },
    });
    if (response.status >= 400) return detailOf(response.data) ?? `回填失败（HTTP ${response.status}）`;
    await loadResearchReviewQueue();
    return null;
  }

  async function deleteAiResearchReport(reportId: string): Promise<void> {
    await client.request({ method: "DELETE", path: `/ai-research/reports/${reportId}` });
    void loadAiReports();
  }

  /** v32 研报追问（2026-09-13 方案 §三）：原报告不可变，结果保存为追加的分析附录。
   * 返回 null = 请求失败/未完成（后端 stage 在 detail 里，由调用方如实提示）。
   * v39 Q10：`mode` 显式选择入口——`interpret`（解读本报告，不获取新事实）与
   * `supplement_research`（补充研究，本轮真的去取数）。缺省 interpret：不悄悄加钱加时延。 */
  async function createFollowUpTurn(
    reportId: string,
    question: string,
    supplements: FollowUpSupplementInput[],
    mode = "interpret",
  ): Promise<AnalysisTurn | null> {
    const response = await client.request<{ ok: boolean; turn: AnalysisTurn }>({
      method: "POST",
      path: `/ai-research/reports/${reportId}/follow-ups`,
      body: { question, supplements: supplements.filter((item) => item.text.trim()), mode },
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return response.data.turn;
  }

  /** v32 某报告的追问附录列表（append-only，按时间正序；报告不存在返回 null）。 */
  async function loadFollowUpTurns(reportId: string): Promise<AnalysisTurn[] | null> {
    const response = await client.request<{ ok: boolean; count: number; items: AnalysisTurn[] }>({
      method: "GET",
      path: `/ai-research/reports/${reportId}/follow-ups`,
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    return response.data.items;
  }

  return {
    aiReports, aiReportsTotal, aiReportFacets, researchReviewQueue,
    generateStockReport, generateDirectionReport, synthesizeCompareReports,
    createCollabRun, runNextCollabStage,
    loadAiReports, loadAiReportDetail, deleteAiResearchReport,
    loadResearchReviewQueue, verifyJudgment,
    createFollowUpTurn, loadFollowUpTurns,
  };
}
