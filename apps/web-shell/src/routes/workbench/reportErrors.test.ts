/**
 * B02/B03（2026-09-15 路线图）回归：失败负载 → 「原因 + 恢复动作」映射。
 * 覆盖：配置类错误不可原样重试；暂时性故障可重试；证据不足列出失败来源；网络层兜底。
 */
import { describe, expect, it } from "vitest";
import { formatStockReportFailure, presentStockReportFailure } from "./reportErrors";

describe("presentStockReportFailure", () => {
  it("model_access_disabled：指向设置，不可原样重试", () => {
    const view = presentStockReportFailure({ status: 200, stage: "model_access_disabled", detail: "模型出网已关闭", sourceErrors: [] });
    expect(view.message).toContain("模型出网已关闭");
    expect(view.hint).toContain("设置页");
    expect(view.canRetry).toBe(false);
  });

  it("evidence_unavailable：列出失败来源，允许重试", () => {
    const view = presentStockReportFailure({
      status: 200,
      stage: "evidence_unavailable",
      detail: "五类输入全部取数失败，无证据可依据，不生成报告（不编造）。",
      sourceErrors: ["K 线取数失败：超时", "新闻取数失败：上游 502"],
    });
    expect(view.message).toContain("新闻取数失败：上游 502");
    expect(view.hint).toContain("数据源");
    expect(view.canRetry).toBe(true);
  });

  it("model_call / parse / citation：暂时性故障，可重试", () => {
    for (const stage of ["model_call", "parse", "citation"]) {
      const view = presentStockReportFailure({ status: 200, stage, detail: `stage=${stage} 的 detail`, sourceErrors: [] });
      expect(view.canRetry).toBe(true);
      expect(view.message).toContain(stage);
    }
  });

  it("HTTP 409 / 404：配置类，指向设置或重新选择，不鼓励原样重试", () => {
    const conflict = presentStockReportFailure({ status: 409, stage: null, detail: "没有「使用中」的模型方案", sourceErrors: [] });
    expect(conflict.hint).toContain("设置页");
    expect(conflict.canRetry).toBe(false);
    const missing = presentStockReportFailure({ status: 404, stage: null, detail: "指定的模型方案不存在", sourceErrors: [] });
    expect(missing.hint).toContain("重新选择");
    expect(missing.canRetry).toBe(false);
  });

  it("502/0：网络层兜底文案与提示", () => {
    const view = presentStockReportFailure({ status: 502, stage: null, detail: "", sourceErrors: [] });
    expect(view.message).toContain("无法连接本地 Core");
    expect(view.canRetry).toBe(true);
  });

  it("formatStockReportFailure：单行文案包含 hint", () => {
    const line = formatStockReportFailure({ status: 200, stage: "model_call", detail: "模型调用失败：401", sourceErrors: [] });
    expect(line).toContain("模型调用失败：401");
    expect(line).toContain("（");
  });
});
