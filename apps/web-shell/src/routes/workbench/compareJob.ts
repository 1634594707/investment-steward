/**
 * B3：批量对比任务模块级单例（与组件生命周期解耦），自 ResearchWorkbenchPage 原样搬出。
 */
import type { CompareEntry } from "./researchTypes";
import { taskCenterRemove, taskCenterUpsert } from "../../state/taskCenter";
import { formatStockReportFailure } from "./reportErrors";
import type { StockReportResult } from "./reportErrors";

/** —— 批量生成任务：模块级单例（与组件生命周期解耦） ——
 * 用户报告：生成中切换界面后报告不再继续/结果丢失。根因是任务状态只活在组件
 * useState 里，任何导致组件树重建的路径（ErrorBoundary 崩溃、跳转重挂载、刷新）
 * 都会把进行中的任务连根丢掉。提升到模块级后：生成循环不依赖组件存活，重挂载
 * 时从快照恢复，报告照常渐进回填。 */
export interface CompareJobSnapshot {
  results: CompareEntry[] | null;
  busy: boolean;
  pending: number;
  activeIndex: number;
}

export let compareJobSnapshot: CompareJobSnapshot = { results: null, busy: false, pending: 0, activeIndex: 0 };
const compareJobListeners = new Set<() => void>();

/** D02（桌面端升级路线图 2026-09-18）：任务中心条目 id 与取消标记/在途 abort 集合。 */
const COMPARE_TASK_ID = "compare-batch-reports";
let compareCancelled = false;
let compareStartedAt = new Date().toISOString();
const compareAbortControllers = new Set<AbortController>();

/** 单份失败重试所需的生成参数（2026-09-10 用户反馈：批量里某几份失败后无从恢复，只能整轮重跑）。
 * 与组件生命周期解耦存在模块级，重试时按条目回填，不动已成功的份数。
 * B01（2026-09-15 路线图）：mode（研报档位）随上下文透传——批量与重试保持用户所选档位，
 * 不再静默回落 standard。
 * D02：generate 末位增 signal（C01 请求层），取消时在途请求一并中止。 */
interface CompareRetryContext {
  question: string;
  barsLimit: number;
  mode?: string;
  generate: (code: string, question: string, barsLimit: number, profileId?: string | null, mode?: string, signal?: AbortSignal) => Promise<StockReportResult>;
  onDone: () => void;
}
let compareRetryContext: CompareRetryContext | null = null;

export function updateCompareJob(patch: Partial<CompareJobSnapshot>): void {
  compareJobSnapshot = { ...compareJobSnapshot, ...patch };
  compareJobListeners.forEach((listener) => listener());
}

export function subscribeCompareJob(listener: () => void): () => void {
  compareJobListeners.add(listener);
  return () => compareJobListeners.delete(listener);
}

/** D02：把当前批量任务登记/刷新到任务中心（进度 = 已定格条目 / 总条目）。 */
function syncTaskCenter(): void {
  const results = compareJobSnapshot.results ?? [];
  const done = results.filter((entry) => !entry.pending).length;
  const failed = results.filter((entry) => !entry.pending && !entry.report).length;
  taskCenterUpsert({
    id: COMPARE_TASK_ID,
    label: "批量研报生成",
    detail: `${done}/${results.length} 份${failed > 0 ? ` · ${failed} 份失败` : ""}`,
    progress: { done, total: results.length },
    stop: cancelCompareJob,
    startedAt: compareStartedAt,
  });
}

/** 执行单份对比报告并回填到任务快照（startCompareJob 与失败重试共用）。
 * D02：为每份生成建独立 AbortController，取消时统一中止在途请求。 */
async function runCompareOne(
  code: string,
  profileId: string | null,
  entryIndex: number,
  entryName: string,
  context: CompareRetryContext,
): Promise<void> {
  const controller = new AbortController();
  compareAbortControllers.add(controller);
  let result: StockReportResult | null = null;
  let errorMessage: string | null = null;
  try {
    result = await context.generate(code, context.question, context.barsLimit, profileId, context.mode, controller.signal);
  } catch (error) {
    errorMessage = controller.signal.aborted
      ? "已取消——可单独重试。"
      : error instanceof Error
        ? error.message
        : "请求失败（Host 桥或 Core 未就绪）。";
  } finally {
    compareAbortControllers.delete(controller);
  }
  const current = compareJobSnapshot.results ?? [];
  const nextPending = Math.max(0, compareJobSnapshot.pending - 1);
  updateCompareJob({
    results: current.map((entry, index) => (
      index === entryIndex
        ? (result?.ok
          ? { symbol: code, name: result.report.model, profileId, report: result.report, error: null, pending: false }
          : {
              symbol: code,
              name: entryName,
              profileId,
              report: null,
              // B02（2026-09-15 方向研判路线图）：保留后端 stage/detail/source_errors 的可读呈现，不再统一吞成固定文案。
              error: result ? formatStockReportFailure(result.failure) : (errorMessage ?? "生成失败：请求未完成。"),
              pending: false,
            })
        : entry
    )),
    pending: nextPending,
  });
  syncTaskCenter();
}

/** 重试单份失败的对比报告（按原标的/原模型方案重跑，已成功份数不动）。 */
export function retryCompareEntry(entryIndex: number): void {
  const context = compareRetryContext;
  const snapshot = compareJobSnapshot;
  const entry = snapshot.results?.[entryIndex];
  if (!context || !entry || entry.pending || entry.report || snapshot.busy) return;
  updateCompareJob({
    results: snapshot.results!.map((item, index) => (index === entryIndex ? { ...item, error: null, pending: true } : item)),
    pending: snapshot.pending + 1,
    busy: true,
  });
  void runCompareOne(entry.symbol, entry.profileId, entryIndex, entry.name, context).then(() => {
    const allSettled = (compareJobSnapshot.results ?? []).every((item) => !item.pending);
    if (allSettled) {
      updateCompareJob({ busy: false });
      context.onDone();
    }
  });
}

export function startCompareJob(
  symbols: string[],
  question: string,
  barsLimit: number,
  profileIds: string[],
  execMode: "parallel" | "serial",
  nameOf: (profileId: string) => string,
  generate: (code: string, question: string, barsLimit: number, profileId?: string | null, mode?: string, signal?: AbortSignal) => Promise<StockReportResult>,
  onDone: () => void,
  mode?: string,
): void {
  const plan: CompareEntry[] = [];
  symbols.forEach((code) => {
    profileIds.forEach((profileId) => {
      plan.push({ symbol: code, name: nameOf(profileId), profileId, report: null, error: null, pending: true });
    });
  });
  compareRetryContext = { question, barsLimit, mode, generate, onDone };
  compareCancelled = false;
  compareStartedAt = new Date().toISOString();
  updateCompareJob({ results: plan, busy: true, pending: plan.length, activeIndex: 0 });
  syncTaskCenter();

  void (async () => {
    // 标的之间按队列顺序逐只；同股内按执行方式并行/串行。
    for (const code of symbols) {
      // D02：取消后不再调度新条目（在途请求已由 abort 中止）。
      if (compareCancelled) break;
      if (execMode === "parallel") {
        const indexes = profileIds.map((profileId) => plan.findIndex((entry) => entry.symbol === code && entry.profileId === profileId));
        await Promise.all(profileIds.map((profileId, i) => runCompareOne(code, profileId, indexes[i]!, nameOf(profileId), { question, barsLimit, mode, generate, onDone })));
      } else {
        for (const profileId of profileIds) {
          if (compareCancelled) break;
          const entryIndex = plan.findIndex((entry) => entry.symbol === code && entry.profileId === profileId);
          await runCompareOne(code, profileId, entryIndex, nameOf(profileId), { question, barsLimit, mode, generate, onDone });
        }
      }
    }
    if (compareCancelled) {
      // D02：取消清扫——未开始的 pending 条目如实标注（已成功份数保留，可按条目重试）。
      const current = compareJobSnapshot.results ?? [];
      updateCompareJob({
        results: current.map((entry) => (entry.pending ? { ...entry, pending: false, error: "已取消——可单独重试" } : entry)),
        pending: 0,
        busy: false,
      });
      taskCenterRemove(COMPARE_TASK_ID);
      onDone();
      return;
    }
    taskCenterRemove(COMPARE_TASK_ID);
    updateCompareJob({ busy: false });
    onDone();
  })();
}

/** D02：取消批量生成——在途请求中止、未开始条目不再调度；已成功份数保留，可按条目重试。 */
export function cancelCompareJob(): void {
  if (!compareJobSnapshot.busy) return;
  compareCancelled = true;
  compareAbortControllers.forEach((controller) => controller.abort());
  compareAbortControllers.clear();
}
