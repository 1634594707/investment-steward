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
