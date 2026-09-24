/**
 * C01/C03（桌面端升级路线图 2026-09-18）：Core 请求层超时/取消/健康事件的契约测试。
 *
 * 两条硬边界：
 * - 只读 GET 有默认超时（30s），POST/PUT 保持无超时——A02 修的深研档 525.5s 场景不回退；
 * - 取消以 499 返回、超时以 504 返回，classifyCoreError 分别可归为「可操作错误/timeout」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createCoreClient, classifyCoreError, DEFAULT_GET_TIMEOUT_MS, onCoreHealthEvent, type CoreRequest } from "./coreClient";

type FetchInit = RequestInit & { headers: Record<string, string> };

describe("coreClient · HTTP 分支（C01）", () => {
  beforeEach(() => {
    vi.stubEnv("VITE_CORE_BASE", "http://127.0.0.1:8900");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function hangOnAbortFetch() {
    return vi.fn((_url: string, init: FetchInit) =>
      new Promise<never>((_resolve, reject) => {
        init.signal!.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }),
    );
  }

  it("★ GET 超过默认超时 → 504 + 可读 detail，健康事件上报 timeout", async () => {
    vi.useFakeTimers();
    const fetchMock = hangOnAbortFetch();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    const events: string[] = [];
    onCoreHealthEvent((event) => events.push(event.kind));
    const client = createCoreClient();

    const promise = client.request({ method: "GET", path: "/holdings" });
    await vi.advanceTimersByTimeAsync(DEFAULT_GET_TIMEOUT_MS + 1);
    const result = await promise;
    expect(result.status).toBe(504);
    expect(String((result.data as { detail: string }).detail)).toContain("超时");
    expect(events).toContain("timeout");
    // 分类可归为 timeout（可操作错误，而非永久骨架）。
    expect(classifyCoreError(504).kind).toBe("timeout");
  });

  it("★ POST 不带 timeoutMs 时不设超时（长 AI 请求不回退 A02 结论）", async () => {
    vi.useFakeTimers();
    const fetchMock = hangOnAbortFetch();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    const client = createCoreClient();

    void client.request({ method: "POST", path: "/evidence/stock-research-report", body: {} });
    await vi.advanceTimersByTimeAsync(DEFAULT_GET_TIMEOUT_MS * 10);
    expect(fetchMock.mock.calls[0]![1]!.signal?.aborted).toBe(false), "POST 无超时：signal 不应被触发";
  });

  it("★ signal 取消 → 499「请求已取消」", async () => {
    const fetchMock = hangOnAbortFetch();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    const client = createCoreClient();
    const controller = new AbortController();
    const promise = client.request({ method: "GET", path: "/holdings", signal: controller.signal });
    controller.abort();
    const result = await promise;
    expect(result.status).toBe(499);
    expect(String((result.data as { detail: string }).detail)).toContain("取消");
  });

  it("★ 显式 timeoutMs 覆盖默认值；成功响应上报 recover", async () => {
    const events: string[] = [];
    onCoreHealthEvent((event) => events.push(event.kind));
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }))),
    );
    const client = createCoreClient();
    const result = await client.request({ method: "GET", path: "/holdings", timeoutMs: 5_000 });
    expect(result.status).toBe(200);
    expect(events).toContain("recover");
  });
});

describe("coreClient · 桥分支（C01）", () => {
  const coreCancel = vi.fn(async () => true);

  function installBridge(coreRequestImpl: (req: CoreRequest & { reqId?: string; timeoutMs?: number }) => Promise<{ status: number; data: unknown }>) {
    (window as unknown as { steward: unknown }).steward = {
      getCoreStatus: async () => ({ status: "ready" }),
      restartCore: async () => ({ status: "ready" }),
      coreRequest: coreRequestImpl,
      coreCancel,
    };
  }

  afterEach(() => {
    delete (window as unknown as { steward?: unknown }).steward;
    coreCancel.mockClear();
  });

  it("★ GET 经桥请求时携带默认 timeoutMs（宿主执行 destroy），POST 不携带", async () => {
    const seen: Array<number | undefined> = [];
    installBridge(async (req) => {
      seen.push(req.timeoutMs);
      return { status: 200, data: { ok: true } };
    });
    const client = createCoreClient();
    await client.request({ method: "GET", path: "/holdings" });
    await client.request({ method: "POST", path: "/evidence/stock-research-report", body: {} });
    expect(seen[0]).toBe(DEFAULT_GET_TIMEOUT_MS);
    expect(seen[1]).toBeUndefined();
  });

  it("★ 取消：signal 触发 coreCancel(reqId)，宿主拒绝后返回 499", async () => {
    let rejectInflight: ((error: Error) => void) | null = null;
    installBridge((_req) =>
      new Promise((_resolve, reject) => {
        rejectInflight = reject;
      }),
    );
    // 模拟宿主行为：收到 coreCancel 后对在途连接 destroy（promise 以错误告终）。
    coreCancel.mockImplementation(async () => {
      rejectInflight?.(new Error("socket destroyed"));
      return true;
    });
    const client = createCoreClient();
    const controller = new AbortController();
    const promise = client.request({ method: "GET", path: "/youzi/today", signal: controller.signal });
    controller.abort();
    const result = await promise;
    expect(result.status).toBe(499);
    expect(coreCancel).toHaveBeenCalledWith(expect.stringMatching(/^req-/));
  });

  it("★ 宿主超时返回的 504 形状原样透传（渲染层不吞）", async () => {
    installBridge(async () => ({ status: 504, data: { detail: "Core 请求超时（30s 未响应）" } }));
    const client = createCoreClient();
    const result = await client.request({ method: "GET", path: "/holdings" });
    expect(result.status).toBe(504);
    expect(classifyCoreError(504, "Core 请求超时（30s 未响应）").kind).toBe("timeout");
  });
});
