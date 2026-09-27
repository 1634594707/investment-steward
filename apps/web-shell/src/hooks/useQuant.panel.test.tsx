/**
 * QL10 / QL11 / QL12（《量化研究实验室质量提升任务路线图》2026-09-22，P2）前端契约。
 *
 * 被测硬要求：
 *   1. 面板构建走任务化通道：POST 建任务 → 轮询进度 → 终态取 summary；非终态期间不误判成功。
 *   2. 挖掘请求体逐字段对齐 Core 口径（横截面参数一个不能漏），否则 UI 上的数字与后端口径会脱节。
 *   3. 停止：先清 jobRef 再发取消，轮询循环必须自行退出并把结果标记为 cancelled（不当作失败）。
 *   4. 快照索引与「从快照重建」都走内容寻址键（content_hash + file_name），不带任何「按名字猜」的降级。
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import { useQuant } from "./useQuant";

/** 与仓库既有测试同构：`CoreResponse` 是各文件本地声明，coreClient 并不导出它。 */
interface CoreResponse<T> {
  status: number;
  data: T;
}

type Call = { method: string; path: string; body?: unknown };

function ok<T>(data: T): CoreResponse<T> {
  return { status: 200, data } as CoreResponse<T>;
}

/** 按调用序列依次回应的桩 client（同时记录全部调用）。 */
function stubClient(responses: Array<{ match: (path: string, method: string) => boolean; reply: (call: Call) => CoreResponse<unknown> }>) {
  const calls: Call[] = [];
  const client = {
    request: vi.fn(async (options: Call) => {
      calls.push(options);
      const handler = responses.find((item) => item.match(options.path, options.method));
      if (!handler) return { status: 404, data: null } as CoreResponse<unknown>;
      return handler.reply(options);
    }),
  } as unknown as CoreClient;
  return { client, calls };
}

const SNAPSHOT = {
  artifact_id: "panel-abc",
  kind: "panel_snapshots",
  stage: "panel",
  symbol: "600000,600001,600002",
  name: "横截面面板 30×250",
  created_at: "2026-09-22T00:00:00+00:00",
  file_name: "panel-deadbeef.json",
  content_hash: "deadbeef".repeat(8),
  payload_meta: {
    panel_spec_version: 1,
    alignment: "intersection",
    window: 250,
    universe_rule: "成交额前 30 名（榜单原始顺序,不做二次筛选）",
    universe_source: "cn-market-board:turnover",
    universe_observed_at: "2026-09-22T00:00:00+00:00",
    first_date: "2025-09-01",
    last_date: "2026-09-22",
    coverage: 1,
    failures: [],
  },
};

describe("fetchQuantPanelSnapshots / loadQuantPanel（QL10 内容寻址）", () => {
  it("快照索引落到 state，并与后端返回一致", async () => {
    const { client } = stubClient([
      { match: (path, method) => path === "/quant/panel/snapshots" && method === "GET", reply: () => ok([SNAPSHOT]) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    await act(async () => {
      await result.current.fetchQuantPanelSnapshots();
    });
    expect(result.current.quantPanelSnapshots).toHaveLength(1);
    expect(result.current.quantPanelSnapshots[0]!.content_hash).toBe(SNAPSHOT.content_hash);
    expect(result.current.quantPanelSnapshots[0]!.payload_meta.universe_rule).toContain("成交额前 30 名");
  });

  it("从快照重建带内容寻址键；哈希复核失败返回 null（不做降级）", async () => {
    const { client, calls } = stubClient([
      { match: (path) => path === "/quant/panel/load", reply: () => ({ status: 409, data: { detail: "面板快照不可用" } } as CoreResponse<unknown>) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    let loaded: unknown = "sentinel";
    await act(async () => {
      loaded = await result.current.loadQuantPanel(SNAPSHOT.content_hash, SNAPSHOT.file_name);
    });
    expect(loaded).toBeNull();
    expect(calls[0]!.body).toEqual({ content_hash: SNAPSHOT.content_hash, file_name: SNAPSHOT.file_name });
  });
});

describe("buildQuantPanel（QL10 任务化 + 轮询）", () => {
  it("轮询到 done 才返回 summary，运行中不误判成功", async () => {
    const summary = { available: true, symbol_count: 30, date_count: 250, failures: [], steps: [] };
    let polls = 0;
    const { client, calls } = stubClient([
      {
        match: (path, method) => path === "/quant/panel/build" && method === "POST",
        reply: () => ok({ ok: true, job_id: "job-1", state: "running", reused: false }),
      },
      {
        match: (path, method) => path === "/quant/panel/build/job-1" && method === "GET",
        reply: () => {
          polls += 1;
          if (polls < 2) return ok({ ok: true, state: "running", done: 12, total: 30, summary: null, error: null });
          return ok({ ok: true, state: "done", done: 30, total: 30, summary, error: null });
        },
      },
      { match: (path, method) => path === "/quant/panel/snapshots" && method === "GET", reply: () => ok([SNAPSHOT]) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    let outcome: { ok: boolean; summary?: unknown } = { ok: false };
    await act(async () => {
      outcome = await result.current.buildQuantPanel({ universeSize: 30, board: "turnover", window: 250 });
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.summary).toEqual(summary);
    expect(polls).toBe(2);
    // 构建完成后自动刷新快照索引
    expect(calls.filter((call) => call.path === "/quant/panel/snapshots")).toHaveLength(1);
  }, 15000);

  it("建任务失败时把 Core 的 detail 原样带回，不当成功", async () => {
    const { client } = stubClient([
      {
        match: (path, method) => path === "/quant/panel/build" && method === "POST",
        reply: () => ({ status: 422, data: { detail: "board 仅支持 turnover / gainers / losers / turnover_rate" } } as CoreResponse<unknown>),
      },
    ]);
    const { result } = renderHook(() => useQuant(client));
    let outcome: { ok: boolean; detail?: string } = { ok: false };
    await act(async () => {
      outcome = await result.current.buildQuantPanel({ universeSize: 30 });
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("board 仅支持");
  });

  it("任务终态为 cancelled 时标记取消而不是失败", async () => {
    const { client } = stubClient([
      { match: (path, method) => path === "/quant/panel/build" && method === "POST", reply: () => ok({ ok: true, job_id: "job-2", state: "running" }) },
      { match: (path) => path === "/quant/panel/build/job-2", reply: () => ok({ ok: true, state: "cancelled", done: 4, total: 30, summary: null, error: null }) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    let outcome: { ok: boolean; cancelled?: boolean } = { ok: true };
    await act(async () => {
      outcome = await result.current.buildQuantPanel({});
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.cancelled).toBe(true);
  }, 15000);
});

describe("mineQuantPanel（QL11 + QL12 请求体口径）", () => {
  it("横截面参数逐字段落到请求体，且绑定内容寻址键", async () => {
    const { client, calls } = stubClient([
      { match: (path, method) => path === "/quant/panel/mine" && method === "POST", reply: () => ok({ ok: true, job_id: "mine-1", state: "running" }) },
      { match: (path) => path === "/quant/panel/mine/mine-1", reply: () => ok({ ok: true, state: "done", done: 2, total: 2, summary: { board: { top: [], marginal: [] }, reports: [] }, error: null }) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    await act(async () => {
      await result.current.mineQuantPanel(SNAPSHOT.content_hash, SNAPSHOT.file_name, {
        forwardDays: 5, topN: 2, quantiles: 4, folds: 3, neutralise: "rank",
        industryGroups: true, labelShuffleSeed: 7,
      });
    });
    expect(calls[0]!.body).toEqual({
      content_hash: SNAPSHOT.content_hash,
      file_name: SNAPSHOT.file_name,
      forward_days: 5,
      top_n: 2,
      quantiles: 4,
      folds: 3,
      neutralise: "rank",
      industry_groups: true,
      label_shuffle_seed: 7,
    });
  }, 15000);

  it("未给出的可选参数不进请求体（由 Core 用默认口径，避免前端写死口径）", async () => {
    const { client, calls } = stubClient([
      { match: (path, method) => path === "/quant/panel/mine" && method === "POST", reply: () => ok({ ok: true, job_id: "mine-2", state: "running" }) },
      { match: (path) => path === "/quant/panel/mine/mine-2", reply: () => ok({ ok: true, state: "done", done: 2, total: 2, summary: { board: { top: [], marginal: [] }, reports: [] }, error: null }) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    await act(async () => {
      await result.current.mineQuantPanel(SNAPSHOT.content_hash, SNAPSHOT.file_name, { topN: 1 });
    });
    expect(calls[0]!.body).toEqual({
      content_hash: SNAPSHOT.content_hash,
      file_name: SNAPSHOT.file_name,
      top_n: 1,
    });
  }, 15000);
});

describe("stopQuantPanelJob", () => {
  it("先清在途标记再发取消，两个取消端点都发（不猜任务属于哪条链路）", async () => {
    const { client, calls } = stubClient([
      { match: (path, method) => (path === "/quant/panel/build" || path === "/quant/panel/mine") && method === "POST", reply: (call) => ok({ ok: true, job_id: call.path.endsWith("mine") ? "mine-3" : "job-3", state: "running" }) },
      { match: (path) => path.startsWith("/quant/panel/build/"), reply: () => ok({ ok: true, state: "running", done: 1, total: 30, summary: null, error: null }) },
      { match: (path) => path.endsWith("/cancel"), reply: () => ok({ ok: true }) },
    ]);
    const { result } = renderHook(() => useQuant(client));
    let outcome: { ok: boolean; cancelled?: boolean } = { ok: true };
    await act(async () => {
      const pending = result.current.buildQuantPanel({});
      // 让「POST 建任务 → 响应返回 → jobRef 落位」这一段微任务跑完
      await new Promise((resolve) => setTimeout(resolve, 0));
      await result.current.stopQuantPanelJob();
      outcome = await pending;
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.cancelled).toBe(true);
    const cancelPaths = calls.filter((call) => call.path.endsWith("/cancel")).map((call) => call.path).sort();
    expect(cancelPaths).toEqual(["/quant/panel/build/job-3/cancel", "/quant/panel/mine/job-3/cancel"]);
  }, 15000);
});
