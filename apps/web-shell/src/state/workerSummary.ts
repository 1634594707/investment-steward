/**
 * T4（用户视角路线图 2026-09-26）：后台巡查状态要能在状态栏看见。
 *
 * 改造前：宿主 `host:get-core-status` 的响应里**本来就带** `worker` 字段
 * （`apps/desktop-host/src/main.ts` 的 `readWorkerStatus()` 读 agent-worker 状态文件），
 * 但 `coreClient.status()` 只映射 `coreStatus.status`、把其余整个丢掉；
 * `StatusBar` 的 Props 里也没有 worker 字段。结果是「后台巡查在跑吗 / 今早日报出了没 /
 * 上次失败是什么时候 / 下次什么时候跑」四个问题**一个都答不了**——
 * 而这正是托盘常驻模式下唯一的常驻可见面。
 *
 * 本模块只做「状态文件 → 一行可读摘要」的纯函数，渲染在 StatusBar。
 */
import type { WorkerStatus } from "@investment-steward/host-bridge";

export type WorkerTone = "ok" | "warn" | "error" | "idle";

export interface WorkerSummary {
  /** 状态栏上显示的主文案。 */
  text: string;
  tone: WorkerTone;
  /** 悬浮提示里的补充细节（失败原因等），可为空。 */
  detail: string;
}

const TASK_LABEL: Record<string, string> = {
  daily: "日报",
  hourly: "小时巡查",
};

function labelOf(task: string): string {
  return TASK_LABEL[task] ?? task;
}

function fmtTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 把 worker 状态文件折算成一行摘要。
 *
 * 口径刻意保守——拿不到就写「未知」，不猜：
 * - `status` 非 running → 直接报该状态（不假装在跑）
 * - 有任务处于连续失败 → 报失败次数与最近失败时间（这是最需要被看见的信号）
 * - 否则报「运行中」+ 最近成功时间 + 最近一次到期时间
 */
export function summarizeWorker(worker: WorkerStatus | null | undefined): WorkerSummary | null {
  if (!worker) return null;
  const tasks = worker.tasks ?? {};
  const entries = Object.entries(tasks);

  if (worker.status && worker.status !== "running") {
    return { text: `后台巡查 ${worker.status}`, tone: "error", detail: "后台巡查进程未处于运行状态" };
  }
  if (entries.length === 0) {
    return { text: "后台巡查运行中", tone: "ok", detail: "尚无任务记录" };
  }

  // 最需要被看见的：连续失败。
  const failing = entries
    .filter(([, rt]) => rt.last_ok === false || (rt.consecutive_failures ?? 0) > 0)
    .sort((a, b) => (b[1].consecutive_failures ?? 0) - (a[1].consecutive_failures ?? 0));
  if (failing.length > 0) {
    const [name, rt] = failing[0]!;
    const times = rt.consecutive_failures ?? 1;
    const at = fmtTime(rt.last_failure_at);
    return {
      text: `后台巡查 · ${labelOf(name)}连续失败 ${times} 次`,
      tone: times > 1 ? "error" : "warn",
      detail: [at ? `最近失败 ${at}` : null, rt.last_error].filter(Boolean).join(" · ") || "无错误详情",
    };
  }

  // 全部正常：报最近成功与最近到期。
  const successes = entries
    .map(([, rt]) => rt.last_success_at)
    .filter((v): v is string => Boolean(v))
    .sort()
    .at(-1) ?? null;
  const nexts = entries
    .map(([, rt]) => rt.next_run_at)
    .filter((v): v is string => Boolean(v))
    .sort()
    .at(0) ?? null;
  const parts = ["后台巡查运行中"];
  const okAt = fmtTime(successes);
  if (okAt) parts.push(`最近成功 ${okAt}`);
  const nextAt = fmtTime(nexts);
  if (nextAt) parts.push(`下次 ${nextAt}`);
  return { text: parts.join(" · "), tone: "ok", detail: `${entries.length} 项任务` };
}
