import { useCallback, useEffect, useRef, useState } from "react";
import { detailOf, type CoreClient, type CoreRequest } from "./coreClient";

/**
 * B1（frontend-optimization-roadmap-2026-09-12）：coreClient 之上的通用数据层。
 * · useCoreQuery —— 只读查询：loading/error + 可选 interval 轮询 + 模块级内存缓存（先出缓存再刷新）。
 * · useCorePoll —— 统一轮询原语：interval + document.visibilityState 隐藏暂停 + 卸载清理。
 *   AppShell 里散落的 setInterval 分域刷新（通知 60s / 行情 30s）统一收敛到这里。
 * 缓存 key = method + path + body；进程生命周期内有效，供「同请求秒开」与乐观更新回填。
 */

const responseCache = new Map<string, unknown>();

export function coreCacheKey(req: CoreRequest): string {
  const body = req.body === undefined ? "" : ` ${JSON.stringify(req.body)}`;
  return `${req.method} ${req.path}${body}`;
}

export function readCoreCache<T>(req: CoreRequest): T | undefined {
  return responseCache.get(coreCacheKey(req)) as T | undefined;
}

export function writeCoreCache(req: CoreRequest, data: unknown): void {
  responseCache.set(coreCacheKey(req), data);
}

export function isDocumentVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

export interface UseCorePollOptions {
  intervalMs: number;
  /** false 时停表。 */
  enabled?: boolean;
  /** 挂载时立即先跑一次。 */
  immediate?: boolean;
}

export function useCorePoll(callback: () => void | Promise<void>, { intervalMs, enabled = true, immediate = false }: UseCorePollOptions): void {
  const callbackRef = useRef(callback);
  callbackRef.current = callback;
  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      // 托盘常驻/切后台时跳过本轮，避免不可见状态下的无谓请求。
      if (isDocumentVisible()) void callbackRef.current();
    };
    if (immediate) tick();
    const timer = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, enabled, immediate]);
}

export interface UseCoreQueryOptions {
  /** 轮询间隔毫秒；省略则只取一次。 */
  intervalMs?: number;
  enabled?: boolean;
}

export interface UseCoreQueryResult<T> {
  data: T | null;
  status: number | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<T | null>;
  /** 本地写入（乐观更新 / 启动装载回填），同步写入缓存。 */
  setData: (data: T | null) => void;
}

export function useCoreQuery<T>(client: CoreClient, req: CoreRequest, options: UseCoreQueryOptions = {}): UseCoreQueryResult<T> {
  const { intervalMs, enabled = true } = options;
  const reqRef = useRef(req);
  reqRef.current = req;
  const [data, setDataState] = useState<T | null>(() => readCoreCache<T>(req) ?? null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const refresh = useCallback(async (): Promise<T | null> => {
    if (!enabled) return null;
    const current = reqRef.current;
    setLoading(true);
    const response = await client.request<T>(current);
    if (!aliveRef.current) return null;
    setStatus(response.status);
    if (response.status < 400) {
      writeCoreCache(current, response.data);
      setDataState(response.data);
      setError(null);
    } else {
      setError(detailOf(response.data) ?? `HTTP ${response.status}`);
    }
    setLoading(false);
    return response.status < 400 ? response.data : null;
  }, [client, enabled, coreCacheKey(req)]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useCorePoll(() => void refresh(), { intervalMs: intervalMs ?? 0, enabled: enabled && intervalMs !== undefined });

  const setData = useCallback((next: T | null) => {
    setDataState(next);
    if (next !== null) writeCoreCache(reqRef.current, next);
  }, []);

  return { data, status, loading, error, refresh, setData };
}
