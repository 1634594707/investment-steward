/**
 * 第四轮审计的前端回归：web-1 / web-2 / web-3 / web-5 / web-8。
 *
 * 这一批的共同点是「**读不出来**被渲染成**确实没有**」，而每一处都会引导用户
 * 重新做一遍已付费的事（重新生成研报 / 重新追问 / 用默认值覆盖自己写的东西）。
 */
import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import { useResearch } from "./useResearch";
import { useMacro } from "./useMacro";

/** 按 method+path 分流：POST 建任务，GET 轮询终态。 */
function jobClient(started: { status: number; data: unknown }, terminal?: { status: number; data: unknown }) {
  const request = vi.fn(async (req: { method: string; path: string; body?: unknown }) => {
    if (req.method === "POST") return started;
    return terminal ?? started;
  });
  return { client: { request } as unknown as CoreClient, request };
}

const STARTED = { status: 200, data: { ok: true, job_id: "job-1", state: "running" } };
const DONE = { status: 200, data: { ok: true, job_id: "job-1", state: "done", done: 4, total: 4, report: "正文" } };

describe("web-3 · 历史产出读取失败不得渲染成「一条都没有」", () => {
  it("失败时记下错误，且不把 counts 刷成成功的 0", async () => {
    const request = vi.fn().mockResolvedValue({ status: 502, data: { detail: "Core 无响应" } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useResearch(client));

    await result.current.loadAiReports();

    await waitFor(() => expect(result.current.aiReportsError).toBeTruthy());
    // 关键：未成功加载过 ⇒ 页面不得展示「还没有保存的研究产出」
    expect(result.current.aiReportsLoaded).toBe(false);
  });

  it("成功加载后错误被清空", async () => {
    const request = vi.fn().mockResolvedValue({
      status: 200,
      data: { ok: true, items: [], total: 0, counts: { total: 0, direction: 0, stock: 0, collab: 0, draft_stock: 0 }, models: [] },
    });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useResearch(client));

    await result.current.loadAiReports();

    await waitFor(() => expect(result.current.aiReportsLoaded).toBe(true));
    expect(result.current.aiReportsError).toBeNull();
  });
});

describe("web-2 · 追问附录读取失败必须抛错（否则「还没有追问」会诱发重复计费）", () => {
  it("读取失败抛可读错误，而不是返回 null", async () => {
    const request = vi.fn().mockResolvedValue({ status: 500, data: { detail: "boom" } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useResearch(client));

    // 此前返回 null → 页面显示「还没有追问」→ 用户重复提问（每次都是一次计费）
    await expect(result.current.loadFollowUpTurns("r1")).rejects.toBeTruthy();
  });

  it("成功时返回列表（不抛）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: { ok: true, count: 1, items: [{ analysis_turn_id: "t1" }] } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useResearch(client));
    await expect(result.current.loadFollowUpTurns("r1")).resolves.toHaveLength(1);
  });
});

describe("web-5 · 宏观「我的判断」读取失败必须抛错（否则会静默覆盖原文）", () => {
  it("失败抛错而不是返回 null", async () => {
    const request = vi.fn().mockResolvedValue({ status: 502, data: {} });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useMacro(client, true, true));

    // 此前返回 null → 页面显示「写下我的判断」+ 默认值 → 保存走整字段 PUT 覆盖原文
    await expect(result.current.fetchMacroUserView("cn")).rejects.toBeTruthy();
  });

  it("成功且确实没写过时仍返回 null（「没有」与「读失败」必须可区分）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: { region: "cn", view: null } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useMacro(client, true, true));
    await expect(result.current.fetchMacroUserView("cn")).resolves.toBeNull();
  });
});

describe("web-8 · 研报任务轮询失败必须有上限（不得无限静默重试）", () => {
  it("连续失败达上限后收工，并说明状态未知", async () => {
    const START_OK = { status: 200, data: { ok: true, job_id: "job-1", state: "running" } };
    const request = vi.fn(async (req: { method: string }) =>
      req.method === "POST" ? START_OK : { status: 404, data: { detail: "任务不存在" } },
    );
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useResearch(client));

    const outcome = await result.current.generateStockReport("600519", "", 250);

    // 改造前这里是裸 continue：界面永远停在「正在取数与生成…」
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.detail).toContain("状态未知");
    }
    // 至少请求过一次 GET 才谈得上「轮询失败」
    expect(request.mock.calls.filter((c) => c[0].method === "GET").length).toBeGreaterThan(1);
  }, 40000);
});

describe("web-1 · 方向研判的模型选择不得被丢弃", () => {
  it("profileId 会被放进请求体（端点透传由 ResearchWorkbenchPage 侧的必填签名保证）", async () => {
    const { client, request } = jobClient(STARTED, DONE);
    const { result } = renderHook(() => useResearch(client));
    await result.current.generateDirectionReport("白酒", "还能买吗", "pid-1");
    const post = request.mock.calls.find((c) => c[0].method === "POST");
    expect(post![0].body).toMatchObject({ profile_id: "pid-1" });
  });
});
