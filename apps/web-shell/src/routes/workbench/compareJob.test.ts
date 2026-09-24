/**
 * B01（2026-09-15 路线图）回归：批量与单份重试必须把用户所选档位 mode 透传到生成调用；
 * 失败条目把后端失败负载渲染为可读文案（不再统一「模型调用或证据解析未通过」）。
 */
import { describe, expect, it, vi } from "vitest";
import { startCompareJob, retryCompareEntry, compareJobSnapshot, subscribeCompareJob } from "./compareJob";
import type { StockReportResult } from "./reportErrors";

function okResult(symbol: string): StockReportResult {
  return { ok: true, report: { ok: true, symbol, title: "T", report: "正文", citations: ["S1"], limitations: [], confidence: "中", model: "m", source_keys: ["S1"], source_errors: {}, generated_at: "t" } };
}

function failureResult(): StockReportResult {
  return { ok: false, failure: { status: 200, stage: "model_call", detail: "模型调用失败：限流", sourceErrors: [] } };
}

/** 等待批量任务整体落定（busy=false）。 */
async function waitSettled(): Promise<void> {
  await vi.waitFor(() => {
    if (compareJobSnapshot.busy) throw new Error("job still busy");
  });
}

describe("startCompareJob / retryCompareEntry（B01 · 2026-09-15 方向研判路线图）", () => {
  it("mode 随每份生成调用透传（批量串行）", async () => {
    const generate = vi.fn(async (_code: string, _q: string, _b: number, _p?: string | null, _m?: string): Promise<StockReportResult> => okResult(_code));
    const onDone = vi.fn();
    startCompareJob(["001201", "002230"], "", 250, ["pid-1"], "serial", (id) => id, generate, onDone, "deep");
    await waitSettled();
    expect(generate).toHaveBeenCalledTimes(2);
    for (const call of generate.mock.calls) {
      expect(call[4]).toBe("deep");
    }
    expect(onDone).toHaveBeenCalled();
  });

  it("失败条目写入后端原因文案；单份重试保持同一 mode", async () => {
    const generate = vi.fn(async (_code: string, _q: string, _b: number, _p?: string | null, _m?: string): Promise<StockReportResult> => failureResult());
    startCompareJob(["001201"], "", 250, ["pid-1"], "parallel", (id) => id, generate, vi.fn(), "deep");
    await waitSettled();
    expect(compareJobSnapshot.results![0]!.error).toContain("模型调用失败：限流");
    expect(compareJobSnapshot.results![0]!.error).not.toBe("生成失败：模型调用或证据解析未通过。");

    const retryGenerate = vi.fn(async (_code: string, _q: string, _b: number, _p?: string | null, _m?: string): Promise<StockReportResult> => okResult(_code));
    // 替换重试上下文中的 generate：直接再启一个同参数任务后手动验证即可；
    // 这里通过重试入口验证 mode 透传（compareRetryContext 在模块内保存）。
    const unsub = subscribeCompareJob(() => {});
    retryCompareEntry(0);
    await waitSettled();
    // retryCompareEntry 复用的是第一次任务的 generate 引用（仍返回失败），此处仅验证重试调用发生且 mode 一致。
    expect(generate.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(generate.mock.calls[1]![4]).toBe("deep");
    void retryGenerate;
    unsub();
  });
});
