export const HOST_BRIDGE_VERSION = "1.0" as const;

export interface HostInfo {
  host_version: string;
  bridge_version: typeof HOST_BRIDGE_VERSION;
  platform: string;
  is_packaged: boolean;
}

export interface CoreStatus {
  status: "starting" | "ready" | "degraded" | "stopped";
  base_url: string;
  version: string;
  pid?: number;
  last_error?: string | null;
  /**
   * A05（架构改进路线图 2026-09-25）：agent-worker 的运行状态。
   * 结构见 `agent-worker.status.json`（业务日历 + 每项巡查的最近成功 / 最近失败 / 下次运行）；
   * 文件尚未生成或已损坏时为 null——后台巡查状态缺失不应影响 Core 连接判定。
   */
  worker?: WorkerStatus | null;
}

export interface WorkerTaskRuntime {
  task: string;
  period: "daily" | "hourly";
  due_at?: string | null;
  next_run_at?: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_ok?: boolean | null;
  last_error?: string;
  consecutive_failures?: number;
  retry_at?: string | null;
  runs_ok?: number;
  runs_failed?: number;
}

export interface WorkerStatus {
  status: string;
  version: string;
  ts: string;
  tool_calls?: number;
  ledger_records?: number;
  business_calendar?: { utc_offset_minutes: number; daily_due_hour: number };
  tasks?: Record<string, WorkerTaskRuntime>;
  [key: string]: unknown;
}

export interface HostBridge {
  getHostInfo(): Promise<HostInfo>;
  getCoreStatus(): Promise<CoreStatus>;
  restartCore(): Promise<CoreStatus>;
  requestFileSelection(options?: { multiple?: boolean; directory?: boolean }): Promise<string[]>;
  /** 在系统文件管理器中打开目录（存储位置页「打开目录」）。返回是否成功。 */
  openPath(path: string): Promise<boolean>;
  showNotification(input: { title: string; body: string }): Promise<void>;
  requestAppRestart(): Promise<void>;
  coreRequest<T = unknown>(request: {
    method: "GET" | "POST" | "PUT" | "DELETE";
    path: `/` | `/${string}`;
    body?: unknown;
    /** C01（桌面端升级路线图 2026-09-18）：请求超时（毫秒）。宿主按此值 request.destroy()；
     * 未指定时由渲染层决定（仅 GET 有默认超时，长 AI 请求保持无超时——A02 的 525.5s 场景不回退）。 */
    timeoutMs?: number;
    /** C01：取消用请求标识（配合 coreCancel）。 */
    reqId?: string;
  }): Promise<{ status: number; data: T }>;
  /** C01：取消一个在途 Core 请求（宿主对该 TCP 连接 request.destroy()）。 */
  coreCancel(reqId: string): Promise<boolean>;
  runnerRequest(request: Record<string, unknown>): Promise<Record<string, unknown>>;
  /** E2：手动触发检查更新（仅打包态；状态推送经 onUpdateStatus）。 */
  checkForUpdates(): Promise<void>;
  onUpdateStatus(callback: (status: UpdateStatus) => void): void;
}
export type UpdateStatus =
  | { phase: "checking" }
  | { phase: "not-available" }
  | { phase: "available"; version: string }
  | { phase: "downloading"; percent: number }
  | { phase: "downloaded"; version: string }
  | { phase: "error"; message: string };
