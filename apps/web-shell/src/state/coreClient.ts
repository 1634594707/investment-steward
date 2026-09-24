
export interface CoreRequest {
  method: "GET" | "POST" | "PUT" | "DELETE";
  path: `/${string}`;
  body?: unknown;
  /**
   * C01（桌面端升级路线图 2026-09-18）：显式超时（毫秒）。缺省时**仅 GET** 走默认超时
   * （DEFAULT_GET_TIMEOUT_MS）；POST/PUT（长 AI 请求）保持无超时——A02 修的深研档 525.5s
   * 场景不回退。超时统一以 504 形状返回，classifyCoreError 归为 timeout。
   */
  timeoutMs?: number;
  /** C01：取消信号。取消以 499 形状返回（detail「请求已取消」）。 */
  signal?: AbortSignal;
}

/** C01：只读 GET 的默认超时——Core 挂起时页面最多等这么久，而不是永远转骨架。 */
export const DEFAULT_GET_TIMEOUT_MS = 30_000;
/** C01：status() 健康探测的上限（探活必须比业务请求更快失败）。 */
const STATUS_TIMEOUT_MS = 10_000;
/** C01：取消请求的返回状态（非标准 HTTP 码，仅本应用内部语义）。 */
export const CANCELLED_STATUS = 499;

/** C03：核心健康事件——所有 client 实例上报超时/恢复，供 AppShell 把「重启本地 Core」变成常驻逃生口。 */
export type CoreHealthEvent = { kind: "timeout" | "recover"; path: string };
const healthListeners = new Set<(event: CoreHealthEvent) => void>();
export function onCoreHealthEvent(listener: (event: CoreHealthEvent) => void): () => void {
  healthListeners.add(listener);
  return () => {
    healthListeners.delete(listener);
  };
}
function reportHealth(event: CoreHealthEvent): void {
  for (const listener of healthListeners) listener(event);
}

function effectiveTimeoutMs(req: CoreRequest): number | undefined {
  if (req.timeoutMs !== undefined) return req.timeoutMs;
  return req.method === "GET" ? DEFAULT_GET_TIMEOUT_MS : undefined;
}

function timeoutDetail(timeoutMs: number | undefined): string {
  return `Core 请求超时（${Math.round((timeoutMs ?? DEFAULT_GET_TIMEOUT_MS) / 1000)}s 未响应）：命令面板或状态栏可重启本地 Core。`;
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`status timeout (${ms}ms)`)), ms);
    const settle = (fn: () => void) => {
      clearTimeout(timer);
      fn();
    };
    promise.then(
      (value) => settle(() => resolve(value)),
      (error) => settle(() => reject(error)),
    );
  });
}

/** "connecting" 仅为前端初始态（Core 状态尚未探明），client.status() 永不返回该值。 */
export type CoreConnection = "ready" | "demo" | "stopped" | "connecting";

/** 请求错误分类（F5-4）：连接条与 toast 统一口径，不再各自为政。 */
export type CoreErrorKind = "network" | "timeout" | "forbidden" | "conflict" | "server";

export interface CoreErrorInfo {
  kind: CoreErrorKind;
  message: string;
}

/** 从 Core 响应体提取 detail 文案（FastAPI 风格 {detail}）。 */
export function detailOf(data: unknown): string | null {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return null;
}

export function classifyCoreError(status: number, detail?: string | null): CoreErrorInfo {
  if (status === 0 || status === 502) return { kind: "network", message: detail || "无法连接本地 Core（进程未启动或端口不可达）" };
  if (status === 408 || status === 504) return { kind: "timeout", message: detail || "Core 请求超时，请稍后重试" };
  if (status === 401 || status === 403) return { kind: "forbidden", message: detail || "请求被拒绝（会话令牌或权限不足）" };
  if (status === 409) return { kind: "conflict", message: detail || "状态冲突：操作与本地当前状态不一致" };
  return { kind: "server", message: detail || `Core 返回错误（HTTP ${status}）` };
}

export interface CoreClient {
  /** 演示模式已移除（2026-09-09 用户拍板「不想要演示模式」）：属性保留以兼容页面 props，恒为 false。 */
  readonly isDemo: false;
  /** 是否存在真实数据通道（Electron Host 桥或 VITE_CORE_BASE 直连）。无通道时页面渲染启动指引而非应用内容。 */
  readonly hosted: boolean;
  /** Host 存在时读取真实 Core 状态；无通道时恒为 "stopped"。 */
  status(): Promise<CoreConnection>;
  request<T = unknown>(req: CoreRequest): Promise<{ status: number; data: T }>;
}

export function createCoreClient(): CoreClient {
  const bridge = window.steward ?? null;
  const coreBase: string | undefined = import.meta.env.VITE_CORE_BASE;
  const coreToken: string | undefined = import.meta.env.VITE_CORE_TOKEN;
  const useHttp = !!coreBase && (import.meta.env.DEV || !bridge);
  const hosted = !!bridge || useHttp;

  async function status(): Promise<CoreConnection> {
    if (useHttp) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);
      try {
        const response = await fetch(`${coreBase}/health`, { signal: controller.signal });
        return response.ok ? "ready" : "stopped";
      } catch {
        return "stopped";
      } finally {
        clearTimeout(timer);
      }
    }
    if (bridge) {
      try {
        const coreStatus = await withTimeout(bridge.getCoreStatus(), STATUS_TIMEOUT_MS);
        return coreStatus.status === "ready" ? "ready" : "stopped";
      } catch {
        return "stopped";
      }
    }
    return "stopped";
  }

  async function request<T = unknown>(req: CoreRequest): Promise<{ status: number; data: T }> {
    if (!hosted) {
      return {
        status: 503,
        data: { detail: "未连接内核（演示模式已移除）：请通过桌面端 Investment Steward.exe 启动，或在开发态配置 VITE_CORE_BASE 直连本地 Core。" } as T,
      };
    }
    if (useHttp) {
      // C01：HTTP 分支——AbortController 合并「外部 signal」与「超时定时器」。
      const timeoutMs = effectiveTimeoutMs(req);
      const controller = new AbortController();
      let timedOut = false;
      const timer = timeoutMs
        ? setTimeout(() => {
            timedOut = true;
            controller.abort();
          }, timeoutMs)
        : undefined;
      const onExternalAbort = () => controller.abort();
      req.signal?.addEventListener("abort", onExternalAbort);
      try {
        if (req.signal?.aborted) {
          return { status: CANCELLED_STATUS, data: { detail: "请求已取消。" } as T };
        }
        const headers: Record<string, string> = {};
        if (coreToken) headers["X-Core-Session-Token"] = coreToken;
        if (req.body !== undefined) headers["Content-Type"] = "application/json";
        const response = await fetch(`${coreBase}${req.path}`, {
          method: req.method,
          headers,
          body: req.body !== undefined ? JSON.stringify(req.body) : undefined,
          signal: controller.signal,
        });
        const text = await response.text();
        let data: unknown = null;
        if (text) {
          try {
            data = JSON.parse(text);
          } catch {
            data = { detail: text };
          }
        }
        reportHealth({ kind: "recover", path: req.path });
        return { status: response.status, data: data as T };
      } catch (error) {
        if (req.signal?.aborted && !timedOut) {
          return { status: CANCELLED_STATUS, data: { detail: "请求已取消。" } as T };
        }
        if (timedOut) {
          reportHealth({ kind: "timeout", path: req.path });
          return { status: 504, data: { detail: timeoutDetail(timeoutMs) } as T };
        }
        return { status: 502, data: { detail: error instanceof Error ? error.message : "Core 请求失败" } as T };
      } finally {
        if (timer) clearTimeout(timer);
        req.signal?.removeEventListener("abort", onExternalAbort);
      }
    }
    if (bridge) {
      // C01：桥分支——signal 不能过 IPC，改为 reqId + host:core-cancel（宿主对 TCP destroy）；
      // 超时也交宿主执行（request.destroy()），渲染层只负责把 timeoutMs 带过去。
      const timeoutMs = effectiveTimeoutMs(req);
      const reqId = `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const onAbort = () => {
        void bridge.coreCancel(reqId);
      };
      if (req.signal) {
        if (req.signal.aborted) {
          return { status: CANCELLED_STATUS, data: { detail: "请求已取消。" } as T };
        }
        req.signal.addEventListener("abort", onAbort);
      }
      const { signal: _signal, ...ipcRequest } = req;
      try {
        const result = await bridge.coreRequest<T>({ ...ipcRequest, reqId, timeoutMs });
        reportHealth({ kind: "recover", path: req.path });
        return result;
      } catch (error) {
        if (req.signal?.aborted) {
          return { status: CANCELLED_STATUS, data: { detail: "请求已取消。" } as T };
        }
        return { status: 502, data: { detail: error instanceof Error ? error.message : "Core 请求失败" } as T };
      } finally {
        req.signal?.removeEventListener("abort", onAbort);
      }
    }
    return { status: 503, data: { detail: "未连接内核（演示模式已移除）。" } as unknown as T };
  }

  return { isDemo: false, hosted, status, request };
}
