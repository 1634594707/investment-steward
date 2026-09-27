/**
 * T5（用户视角路线图 2026-09-26）：会话被拒（401/403）必须可被识别、可被恢复。
 *
 * 改造前的问题链：
 * 1. `classifyCoreError` 能把 401/403 翻成「会话令牌或权限不足」，但 `AppShell`
 *    的启动路径用的是自己写的 `toLoadError`，直出原始 HTTP 码——该函数在 AppShell
 *    里是**死 import**，从未被调用；
 * 2. `status()` 只会返回 "ready" / "stopped"。一个活着却持续 403 的 Core 会被判为
 *    "ready"，于是 `canRestartCore`（`connection === "stopped" || coreHung`）永远
 *    不成立，「重启本地 Core」按钮被藏起来——用户只剩一个必然继续失败的「重试加载」。
 *
 * 本文件锁定：401/403 上报 `unauthorized` 健康事件（而不是伪装成 `recover`）、
 * 恢复后上报 `recover`、且 `classifyCoreError` 把它归为 forbidden。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { classifyCoreError, createCoreClient, onCoreHealthEvent } from "./coreClient";

function respondWith(status: number, detail = "invalid local session") {
  return vi.fn(async () =>
    new Response(JSON.stringify({ detail }), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("T5 · 401/403 上报 unauthorized 健康事件", () => {
  beforeEach(() => {
    vi.stubEnv("VITE_CORE_BASE", "http://127.0.0.1:8900");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it.each([401, 403])("HTTP %i → unauthorized（不是 recover）", async (status) => {
    vi.stubGlobal("fetch", respondWith(status) as unknown as typeof fetch);
    const events: string[] = [];
    const off = onCoreHealthEvent((event) => events.push(event.kind));
    const client = createCoreClient();

    const result = await client.request({ method: "GET", path: "/holdings" });
    off();

    expect(result.status).toBe(status);
    expect(events).toContain("unauthorized");
    expect(events).not.toContain("recover");
  });

  it("鉴权失败后可由一次成功请求恢复（recover 会清掉失败态）", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "invalid local session" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const events: string[] = [];
    const off = onCoreHealthEvent((event) => events.push(event.kind));
    const client = createCoreClient();

    await client.request({ method: "GET", path: "/holdings" });
    await client.request({ method: "GET", path: "/holdings" });
    off();

    expect(events).toEqual(["unauthorized", "recover"]);
  });

  it("普通成功不误报 unauthorized", async () => {
    vi.stubGlobal("fetch", respondWith(200, "") as unknown as typeof fetch);
    const events: string[] = [];
    const off = onCoreHealthEvent((event) => events.push(event.kind));
    await createCoreClient().request({ method: "GET", path: "/holdings" });
    off();
    expect(events).not.toContain("unauthorized");
  });
});

describe("T5 · classifyCoreError 对 401/403 的口径", () => {
  it("归为 forbidden", () => {
    expect(classifyCoreError(401).kind).toBe("forbidden");
    expect(classifyCoreError(403).kind).toBe("forbidden");
  });

  it("Core 的固定英文字面量不透传给用户，换成可指路的中文", () => {
    const message = classifyCoreError(403, "invalid local session").message;
    expect(message).not.toContain("invalid local session");
    expect(message).toContain("重启");
  });

  it("更具体的 detail 仍保留（不一律覆盖）", () => {
    expect(classifyCoreError(403, "策略包未签名，拒绝执行").message).toBe("策略包未签名，拒绝执行");
  });
});

describe("T5 · workerStatus 无 Host 通道时恒为 null", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("纯浏览器模式返回 null，不抛错", async () => {
    const client = createCoreClient();
    await expect(client.workerStatus()).resolves.toBeNull();
  });
});
