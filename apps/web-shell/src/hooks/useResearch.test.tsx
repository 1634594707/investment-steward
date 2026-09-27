/**
 * B01/B02（2026-09-15 路线图）回归：generateStockReport 的参数透传与失败结构化返回。
 * - 显式 profileId / mode 必须进入请求体（模型选错、档位丢失的意图回归）；
 * - ok=false 时返回 stage/detail/source_errors，不再吞成 null；
 * - HTTP 4xx/5xx 时保留状态码与 detail。
 *
 * T10（用户视角路线图 2026-09-26）：端点改为后台任务后，调用形态变成两段——
 * POST 建任务拿 job_id，再轮询 GET 取终态。本文件的 mock 因此**按路径分流**，
 * 不再是「所有请求返回同一份」。三条断言的业务意图保持不变。
 */
import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import { useResearch } from "./useResearch";

/** 按 method+path 分流：POST 建任务，GET 轮询终态。 */
function jobClient(started: { status: number; data: unknown }, terminal?: { status: number; data: unknown }) {
  const request = vi.fn(async (req: { method: string; path: string; body?: unknown }) => {
    if (req.method === "POST") return started;
    return terminal ?? started;
  });
  return { client: { request } as unknown as CoreClient, request };
}

const DONE = {
  ok: true,
  job_id: "job-1",
  state: "done",
  done: 4,
  total: 4,
  symbol: "001201",
  report: "正文",
  citations: ["S1"],
  model: "m",
  generated_at: "t",
  source_errors: {},
  source_keys: ["S1"],
  title: "T",
  confidence: "中",
};

const STARTED = { status: 200, data: { ok: true, job_id: "job-1", state: "running" } };

describe("useResearch.generateStockReport（B01/B02 · 2026-09-15 方向研判路线图）", () => {
  it("显式 profileId 与 mode 进入请求体（单股选模型不再丢失）", async () => {
    const { client, request } = jobClient(STARTED, { status: 200, data: DONE });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250, "pid-1", "deep");
    expect(outcome.ok).toBe(true);
    expect(request.mock.calls[0]![0].body).toEqual({ symbol: "001201", question: "", bars_limit: 250, profile_id: "pid-1", mode: "deep" });
  });

  it("ok=false：保留 stage/detail/source_errors，不再返回 null", async () => {
    const { client, request } = jobClient(STARTED, {
      status: 200,
      data: {
        ok: false,
        job_id: "job-1",
        state: "error",
        // T10：业务阶段在 `stage`，任务生命周期阶段在 `job_stage`——两者不可互相覆盖。
        stage: "model_call",
        job_stage: "saving",
        detail: "模型调用失败：401",
        source_errors: { S2: "新闻取数失败" },
      },
    });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250, null, "deep");
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.stage).toBe("model_call");
      expect(outcome.failure.detail).toContain("模型调用失败");
      expect(outcome.failure.sourceErrors).toEqual(["新闻取数失败"]);
    }
    expect(request.mock.calls[0]![0].body).not.toHaveProperty("profile_id");
  });

  it("HTTP 500：保留状态码与 detail（不再统一转空结果）", async () => {
    const { client } = jobClient({ status: 500, data: { detail: "后端内部错误" } });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(500);
      expect(outcome.failure.detail).toBe("后端内部错误");
    }
  });
});

describe("useResearch.generateStockReport（T10 · 后台任务）", () => {
  it("建任务返回 ok=false 且无 job_id 时如实回报，不进入轮询", async () => {
    const { client, request } = jobClient({
      status: 200,
      data: { ok: false, stage: "model_access_disabled", detail: "模型出网已关闭（STEWARD_MODEL_ACCESS=0）" },
    });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.stage).toBe("model_access_disabled");
      expect(outcome.failure.detail).toContain("出网已关闭");
    }
    // 关键：没有 job_id 就绝不发起 GET 轮询（否则会对着 undefined 轮询）
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("建任务后按 job_id 轮询终态并取回报告", async () => {
    const { client, request } = jobClient(STARTED, { status: 200, data: DONE });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) expect(outcome.report.report).toBe("正文");
    const poll = request.mock.calls.find((call) => call[0].method === "GET");
    expect(poll![0].path).toBe("/evidence/stock-research-report/job-1");
  });

  it("cancelled 终态如实回报为取消，不当作失败", async () => {
    const { client } = jobClient(STARTED, { status: 200, data: { ok: false, job_id: "job-1", state: "cancelled", detail: "已取消" } });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.failure.status).toBe(499);
  });
});
