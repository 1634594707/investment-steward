import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelCompareJob,
  compareJobSnapshot,
  startCompareJob,
  subscribeCompareJob,
  updateCompareJob,
} from "./compareJob";
import { taskCenterRemove, taskCenterSnapshot } from "../../state/taskCenter";
import type { StockReportResult } from "./reportErrors";

/**
 * D02（桌面端升级路线图 2026-09-18）：批量研报生成的取消与任务中心契约。
 * 验收：批量生成中途可停（在途请求中止、未开始条目不再调度）、已成功份数保留
 * （可按条目 retryCompareEntry）、任务中心条目随任务结束/取消移除。
 */

function okReport(code: string): StockReportResult {
  return { ok: true, report: { symbol: code, model: "m1" } } as unknown as StockReportResult;
}

function reset(): void {
  updateCompareJob({ results: null, busy: false, pending: 0, activeIndex: 0 });
  taskCenterSnapshot().forEach((entry) => taskCenterRemove(entry.id));
}

beforeEach(() => {
  reset();
});

describe("compareJob · 取消与任务中心（D02）", () => {
  it("★ 中途取消：在途条目标注已取消、未开始条目不再调度、已成功份数保留", async () => {
    const generate = vi.fn(
      async (code: string, _q: string, _b: number, _p: string | null | undefined, _m: string | undefined, signal?: AbortSignal) => {
        if (code === "000001") {
          return okReport(code);
        }
        // 第二只在途挂起：取消（signal abort）后拒绝。
        return new Promise<StockReportResult>((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      },
    );
    const onDone = vi.fn();
    startCompareJob(["000001", "hung", "000003"], "q", 60, ["m1"], "serial", () => "m1", generate, onDone);
    expect(compareJobSnapshot.busy).toBe(true);

    // 等第一只成功落账后再取消。
    await vi.waitFor(() => expect(compareJobSnapshot.results?.[0]?.report).not.toBeNull());
    expect(taskCenterSnapshot().some((entry) => entry.id === "compare-batch-reports")).toBe(true);

    cancelCompareJob();
    await vi.waitFor(() => expect(compareJobSnapshot.busy).toBe(false));

    const results = compareJobSnapshot.results ?? [];
    expect(results[0]!.report).not.toBeNull(), "已成功份数保留";
    expect(results[1]!.pending).toBe(false);
    expect(results[1]!.error).toContain("已取消");
    expect(results[2]!.pending).toBe(false);
    expect(results[2]!.error).toContain("已取消");
    expect(onDone).toHaveBeenCalled();
    expect(taskCenterSnapshot().some((entry) => entry.id === "compare-batch-reports")).toBe(false);
  });

  it("★ 正常跑完：任务中心条目移除，onDone 触发", async () => {
    const generate = vi.fn(async (code: string) => okReport(code));
    const onDone = vi.fn();
    startCompareJob(["000001", "000002"], "q", 60, ["m1"], "serial", () => "m1", generate, onDone);
    await vi.waitFor(() => expect(compareJobSnapshot.busy).toBe(false));
    expect(onDone).toHaveBeenCalled();
    expect(taskCenterSnapshot().some((entry) => entry.id === "compare-batch-reports")).toBe(false);
  });

  it("★ 空闲时取消是空操作（不触发清扫、不误标条目）", () => {
    cancelCompareJob();
    expect(compareJobSnapshot.busy).toBe(false);
    expect(compareJobSnapshot.results).toBeNull();
  });

  it("★ 快照与任务中心经订阅保持一致（重挂载恢复语义不回归）", async () => {
    const generate = vi.fn(async (code: string) => okReport(code));
    let snapshotEvents = 0;
    const off = subscribeCompareJob(() => {
      snapshotEvents += 1;
    });
    startCompareJob(["000001"], "q", 60, ["m1"], "serial", () => "m1", generate, () => undefined);
    await vi.waitFor(() => expect(compareJobSnapshot.busy).toBe(false));
    expect(snapshotEvents).toBeGreaterThan(0);
    off();
  });
});
