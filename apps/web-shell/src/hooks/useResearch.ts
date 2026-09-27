import { useRef, useState } from "react";
import type { CoreClient } from "../state/coreClient";
import { classifyCoreError, detailOf } from "../state/coreClient";
import { taskCenterRemove, taskCenterUpsert } from "../state/taskCenter";
import type { StockReportResult } from "../routes/workbench/reportErrors";
import type {
  AiReportItem,
  StockReport,
  StockReportJobStatus,
  DirectionReport,
  CompareSynthesis,
  CollabRunView,
  ReviewQueue,
  AnalysisTurn,
} from "../routes/workbench/researchTypes";

/** T10：研报任务轮询间隔。与批量面板/战法扫描的 1.2s 同口径。 */
const STOCK_REPORT_POLL_MS = 1200;
/** T10 补：连续轮询失败上限（≈ 1.2s × 10 ≈ 12 秒）。超过即如实收工，不无限静默重试。 */
const STOCK_REPORT_MAX_POLL_FAILURES = 10;

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
  // 「历史产出」是否**成功加载过** + 最近一次失败原因。
  // 失败时 counts 停留在初值全 0，页面会把它当成「一条产出都没有」，渲染成
  // 「还没有保存的研究产出」——用户据此以为此前所有已付费的研报/方向研判都丢了，
  // 于是重新生成一遍，又是一轮真金白银的模型调用。这是最贵的一类重复计费。
  // 这两个状态把「读不出来」与「确实没有」分开，空态只在成功加载过之后才成立。
  const [aiReportsLoaded, setAiReportsLoaded] = useState(false);
  const [aiReportsError, setAiReportsError] = useState<string | null>(null);
  // 记住最近一次使用的筛选参数：生成/删除成功后的自动刷新沿用同一视图（不带参调用时）。
  const lastParamsRef = useRef<AiReportListParams | null>(null);
  // v27 待复盘验证点队列（由已落库研报派生），研究工作台页消费。
  // G01（桌面端升级路线图 2026-09-18）：服务端在读路径上把带确切日期的验证点**幂等**落成
  // `verifiable_judgments`，因此本队列自 G01 起可写——回填走 `verifyJudgment`（复用既有 verify 通道）。
  const [researchReviewQueue, setResearchReviewQueue] = useState<ReviewQueue | null>(null);
  // T10：在途研报任务 id。组件重挂载（KeepAlive 之外的情况）后据此重新挂接，
  // 而不是让用户重新点一次、白烧一份模型调用。
  const jobIdRef = useRef<string | null>(null);

  /** v21 AI 个股研究报告：手动选股 → 新闻/公告/财报 + K 线 → 模型报告。
   *
   *  T10（用户视角路线图 2026-09-26）：改为**后台任务 + 轮询**。
   *  改造前这是一次同步长请求（单份最坏约 24 分钟），切页面/崩溃/重启都会让已付费的
   *  调用凭空蒸发，界面只能提示「请勿关闭窗口」。现在：
   *  1) POST 只建任务、立即返回 job_id（服务端有 job 表，关页面不影响任务继续跑）；
   *  2) 轮询 GET /evidence/stock-research-report/{job_id} 取进度与终态；
   *  3) 任务登记进任务中心，状态栏可见进度并可停止；
   *  4) jobIdRef 让「离开页面再回来」能重新挂接，而不是从头再烧一次模型。
   *
   *  v29：mode = 研报字数预算档（quick/standard/deep），后端做软告警；不传走后端默认 standard。
   *  B02：不再把失败转 null——成功返回 {ok:true, report}，失败返回 {ok:false, failure:{status, stage, detail, sourceErrors}}。
   *  D02：末位增 signal（C01 请求层）——取消时中止在途请求。 */
  async function generateStockReport(symbol: string, question: string, barsLimit: number, profileId?: string | null, mode?: string, signal?: AbortSignal): Promise<StockReportResult> {
    let started;
    try {
      started = await client.request<{ ok: boolean; job_id?: string; state?: string; reused?: boolean; detail?: string; stage?: string }>({
        method: "POST",
        path: "/evidence/stock-research-report",
        body: { symbol, question, bars_limit: barsLimit, ...(profileId ? { profile_id: profileId } : {}), ...(mode ? { mode } : {}) },
        signal,
      });
    } catch (error) {
      return { ok: false, failure: { status: 502, stage: null, detail: error instanceof Error ? error.message : "报告生成请求失败", sourceErrors: [] } };
    }
    if (started.status >= 400) {
      return {
        ok: false,
        failure: {
          status: started.status,
          stage: null,
          detail: detailOf(started.data) ?? `Core 返回错误（HTTP ${started.status}）`,
          sourceErrors: [],
        },
      };
    }
    const startBody = started.data;
    if (!startBody) {
      return { ok: false, failure: { status: started.status, stage: null, detail: "Core 返回空响应，无法确认生成结果", sourceErrors: [] } };
    }
    // 出网总闸关闭等**建任务前**的拒绝：没有 job_id，如实回报。
    if (startBody.ok === false || !startBody.job_id) {
      return {
        ok: false,
        failure: {
          status: started.status,
          stage: startBody.stage ?? null,
          detail: startBody.detail ?? "生成未启动（后端未给出具体原因）",
          sourceErrors: [],
        },
      };
    }

    const jobId = startBody.job_id;
    jobIdRef.current = jobId;
    const taskId = `research-${jobId}`;
    const startedAt = new Date().toISOString();
    taskCenterUpsert({
      id: taskId,
      label: `个股研报 · ${symbol}`,
      detail: "已提交，正在取数…",
      progress: { done: 0, total: 4 },
      stop: () => { void client.request({ method: "POST", path: `/evidence/stock-research-report/${jobId}/cancel` }); },
      startedAt,
    });

    // 轮询到终态。间隔 1.2s 与批量研报（useQuant/useTactics 的任务轮询）同口径。
    //
    // 轮询失败**必须有上限**。改造前这里是裸 `continue`：没有次数上限、没有退避、
    // 也不清任务中心条目。Core 一旦重启（job 记录随之丢失，持续 404），界面就会
    // 永远停在「正在取数与生成…」、状态栏永远挂着那条任务、生成按钮永远转圈——
    // 用户分不清「模型还在跑」与「任务已失联」，已付费的那次调用结果无从得知。
    // 连续失败到阈值即如实收工：任务中心条目要清掉（否则状态栏永久残留），
    // 并明确告知「状态未知」而不是无限等。
    let consecutivePollFailures = 0;
    for (;;) {
      if (signal?.aborted) {
        taskCenterRemove(taskId);
        jobIdRef.current = null;
        return { ok: false, failure: { status: 499, stage: null, detail: "已取消。", sourceErrors: [] } };
      }
      await new Promise((resolve) => setTimeout(resolve, STOCK_REPORT_POLL_MS));
      const polled = await client.request<StockReportJobStatus>({
        method: "GET",
        path: `/evidence/stock-research-report/${jobId}`,
      });
      if (polled.status >= 400 || !polled.data) {
        consecutivePollFailures += 1;
        if (consecutivePollFailures < STOCK_REPORT_MAX_POLL_FAILURES) continue;
        taskCenterRemove(taskId);
        jobIdRef.current = null;
        return {
          ok: false,
          failure: {
            status: polled.status || 0,
            stage: null,
            detail:
              `进度查询连续失败 ${STOCK_REPORT_MAX_POLL_FAILURES} 次，任务状态未知。` +
              "服务端任务可能已随 Core 重启丢失；这份报告的生成结果请到「历史报告」确认。",
            sourceErrors: [],
          },
        };
      }
      consecutivePollFailures = 0;
      const body = polled.data;
      const state = body.state;
      if (state === "running") {
        taskCenterUpsert({
          id: taskId,
          label: `个股研报 · ${symbol}`,
          detail: body.job_stage === "saving" ? "正在保存报告…" : "正在取数与生成…",
          progress: { done: body.done ?? 0, total: body.total ?? 4 },
          stop: () => { void client.request({ method: "POST", path: `/evidence/stock-research-report/${jobId}/cancel` }); },
          startedAt,
        });
        continue;
      }
      taskCenterRemove(taskId);
      jobIdRef.current = null;
      if (state === "cancelled") {
        return { ok: false, failure: { status: 499, stage: null, detail: "已取消。", sourceErrors: [] } };
      }
      if (state === "error") {
        return {
          ok: false,
          failure: {
            status: 200,
            stage: body.stage ?? null,
            detail: body.detail ?? "生成失败。",
            sourceErrors: Object.values(body.source_errors ?? {}),
          },
        };
      }
      // done：终态响应体与改造前 POST 的报告体同形状（Core 把 summary 并回响应），
      // 但类型上多出 job_id/state/job_stage 等任务字段，故经 unknown 中转。
      return { ok: true, report: body as unknown as StockReport };
    }
  }

  /** T10：当前在途的研报任务 id（供离开页面后回来重新挂接）。 */
  function stockReportJobId(): string | null {
    return jobIdRef.current;
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
      setAiReportsLoaded(true);
      setAiReportsError(null);
    } else {
      // 如实记失败：页面据此渲染「读取失败 + 重新读取」，而不是伪装成「一条都没有」
      setAiReportsError(classifyCoreError(response.status, detailOf(response.data)).message);
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
  ): Promise<{ turn: AnalysisTurn; persisted: boolean } | null> {
    const response = await client.request<{ ok: boolean; turn: AnalysisTurn; persisted?: boolean }>({
      method: "POST",
      path: `/ai-research/reports/${reportId}/follow-ups`,
      body: { question, supplements: supplements.filter((item) => item.text.trim()), mode },
    });
    if (response.status >= 400 || !response.data || !response.data.ok) return null;
    // S3 同款：落库失败时 Core 返回 persisted=false，调用方据此提示「本次未保存」，
    // 而不是让用户读完就丢、且以为追问会一直留在附录里。
    return { turn: response.data.turn, persisted: response.data.persisted !== false };
  }

  /**
   * 读追问附录（append-only，按时间正序；报告不存在返回 null）。
   * 读追问附录。第四轮审计修正：**失败必须抛错，不能与「一条都没有」同形返回 null**。
   *
   * 此前失败返回 `null`，调用方 `items ?? []` 把它变成空数组，于是页面显示
   * 「还没有追问」。`coreClient.request` 在所有分支都 `return {status, data}`、**从不
   * reject**，所以调用方那个 `.catch(() => setFollowupError(...))` 是**死代码**——
   * 错误提示从未出现过。后果是：打开一份已有 3 条附录的研报，只要这一次 GET 超时
   * （30s）/ 401 / 502，界面就说「还没有追问」，用户以为此前分析丢了，于是把同样的
   * 问题再问一遍——**每次追问都是一次真实的模型调用**。
   *
   * 现在按 `classifyCoreError` 抛出可读错误，`followupError` 真的会亮起来。
   */
  async function loadFollowUpTurns(reportId: string): Promise<AnalysisTurn[] | null> {
    const response = await client.request<{ ok: boolean; count: number; items: AnalysisTurn[] }>({
      method: "GET",
      path: `/ai-research/reports/${reportId}/follow-ups`,
    });
    if (response.status >= 400 || !response.data || !response.data.ok) {
      throw new Error(classifyCoreError(response.status, detailOf(response.data)).message);
    }
    return response.data.items;
  }

  return {
    aiReports, aiReportsTotal, aiReportFacets, aiReportsLoaded, aiReportsError, researchReviewQueue,
    generateStockReport, generateDirectionReport, synthesizeCompareReports,
    createCollabRun, runNextCollabStage,
    loadAiReports, loadAiReportDetail, deleteAiResearchReport,
    loadResearchReviewQueue, verifyJudgment,
    createFollowUpTurn, loadFollowUpTurns,
  };
}
