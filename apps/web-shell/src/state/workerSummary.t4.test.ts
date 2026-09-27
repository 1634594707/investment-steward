/**
 * T4（用户视角路线图 2026-09-26）：后台巡查状态摘要——口径保守，拿不到就写未知，不猜。
 */
import { describe, expect, it } from "vitest";
import type { WorkerStatus, WorkerTaskRuntime } from "@investment-steward/host-bridge";
import { summarizeWorker } from "./workerSummary";

const task = (over: Partial<WorkerTaskRuntime> = {}): WorkerTaskRuntime => ({
  task: "daily",
  period: "daily",
  ...over,
});

const worker = (over: Partial<WorkerStatus> = {}): WorkerStatus => ({
  status: "running",
  version: "0.2.0",
  ts: "2026-09-26T02:00:00Z",
  ...over,
});

describe("summarizeWorker · 基本口径", () => {
  it("无 worker 数据时返回 null（不渲染任何段，而不是假装正常）", () => {
    expect(summarizeWorker(null)).toBeNull();
    expect(summarizeWorker(undefined)).toBeNull();
  });

  it("进程非 running 时如实报该状态，不假装还在跑", () => {
    const s = summarizeWorker(worker({ status: "stopped" }));
    expect(s?.tone).toBe("error");
    expect(s?.text).toContain("stopped");
  });

  it("running 但无任务记录：报运行中并说明尚无记录", () => {
    const s = summarizeWorker(worker({ tasks: {} }));
    expect(s?.tone).toBe("ok");
    expect(s?.text).toBe("后台巡查运行中");
    expect(s?.detail).toContain("尚无任务记录");
  });
});

describe("summarizeWorker · 连续失败最优先报出", () => {
  it("单次失败用 warn，多���连续失败用 error", () => {
    const warn = summarizeWorker(worker({
      tasks: { daily: task({ last_ok: false, last_failure_at: "2026-09-26T01:00:00Z", consecutive_failures: 1 }) },
    }));
    expect(warn?.tone).toBe("warn");
    expect(warn?.text).toContain("日报");
    expect(warn?.text).toContain("连续失败 1 次");

    const error = summarizeWorker(worker({
      tasks: { daily: task({ last_ok: false, last_failure_at: "2026-09-26T01:00:00Z", consecutive_failures: 3 }) },
    }));
    expect(error?.tone).toBe("error");
    expect(error?.text).toContain("连续失败 3 次");
  });

  it("失败时带上最近失败时间与错误详情", () => {
    const s = summarizeWorker(worker({
      tasks: {
        daily: task({
          last_ok: false,
          consecutive_failures: 2,
          last_failure_at: "2026-09-26T01:30:00Z",
          last_error: "HTTP 503",
        }),
      },
    }));
    expect(s?.detail).toContain("最近失败");
    expect(s?.detail).toContain("HTTP 503");
  });

  it("多任务时优先报连续失败次数最多的那个", () => {
    const s = summarizeWorker(worker({
      tasks: {
        daily: task({ last_ok: false, consecutive_failures: 1 }),
        hourly: task({ last_ok: false, consecutive_failures: 5 }),
      },
    }));
    expect(s?.text).toContain("小时巡查");
    expect(s?.text).toContain("连续失败 5 次");
  });
});

describe("summarizeWorker · 全部正常时给有用信息", () => {
  it("报最近成功与最近一次到期时间", () => {
    const s = summarizeWorker(worker({
      tasks: {
        daily: task({ last_ok: true, last_success_at: "2026-09-26T00:05:00Z", next_run_at: "2026-09-27T00:00:00Z" }),
        hourly: task({ last_ok: true, last_success_at: "2026-09-26T01:00:00Z", next_run_at: "2026-09-26T02:00:00Z" }),
      },
    }));
    expect(s?.tone).toBe("ok");
    expect(s?.text).toContain("运行中");
    expect(s?.text).toContain("最近成功");
    expect(s?.text).toContain("下次");
    expect(s?.detail).toContain("2 项任务");
  });

  it("时间戳非法时静默省略该片段，而不是显示 Invalid Date", () => {
    const s = summarizeWorker(worker({
      tasks: { daily: task({ last_ok: true, last_success_at: "not-a-date", next_run_at: "???" }) },
    }));
    expect(s?.text).not.toContain("Invalid");
    expect(s?.text).toBe("后台巡查运行中");
  });

  it("未知任务名原样显示，不硬套中文标签", () => {
    const s = summarizeWorker(worker({
      tasks: { some_future_task: task({ task: "some_future_task", last_ok: false, consecutive_failures: 1 }) },
    }));
    expect(s?.text).toContain("some_future_task");
  });
});
