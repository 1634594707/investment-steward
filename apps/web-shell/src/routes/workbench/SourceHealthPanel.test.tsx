import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { SourceHealthPanel } from "./SourceHealthPanel";
import { ReportResultTabs } from "./panels";
import { buildStockReportHtml, buildStockReportMarkdown } from "./export";
import { formatTimestamp } from "./format";
import type { StockReport } from "./researchTypes";

const report: StockReport = {
  ok: true, symbol: "000001", report_id: "r1", title: "test", report: "body", citations: [], limitations: [],
  confidence: "low", model: "test", generated_at: "2026-10-26", source_keys: [], source_errors: {},
  source_health: { checked_on: "2026-10-26", note: "仅检查冻结来源", sources:
    (["failed", "stale", "unknown_date", "within_threshold"] as const).map((state, index) => ({
      source_id: `S${index + 1}`, state, data_date: null, age_days: null, max_age_days: 30,
      provider: null, retrieved_at: "2026-10-26T01:00:00Z", cache_age_days: null,
      error: state === "failed" ? "连接失败" : null,
    })),
  },
};
afterEach(cleanup);
describe("来源健康与补研入口", () => {
  it("页面和实际 MD/HTML 导出保留同样的日期、状态和失败原因，正文仍保留", () => {
    const { container } = render(<SourceHealthPanel report={report} />);
    const markdown = buildStockReportMarkdown(report);
    const html = new DOMParser().parseFromString(buildStockReportHtml(report), "text/html");
    for (const output of [container.textContent!, markdown, html.body.textContent!]) {
      for (const text of ["检查日期：2026-10-26", "取数失败", "观测日期超过阈值", "日期无法核验", "观测日期在阈值内", "缓存年龄：未知", "连接失败", formatTimestamp("2026-10-26T01:00:00Z")]) {
        expect(output).toContain(text);
      }
    }
    expect(markdown).toContain(report.report);
    expect(html.body.textContent).toContain(report.report);
    expect(html.querySelectorAll("button")).toHaveLength(0);
  });

  it("旧报告没有健康记录时页面与导出都明确无法核验", () => {
    const oldReport = { ...report, source_health: undefined };
    const { container } = render(<SourceHealthPanel report={oldReport} />);
    const html = new DOMParser().parseFromString(buildStockReportHtml(oldReport), "text/html");
    for (const output of [container.textContent!, buildStockReportMarkdown(oldReport), html.body.textContent!]) {
      expect(output).toContain("缺少来源健康记录，无法核验数据时效。");
    }
  });

  it("各类缺口有入口，近期采集也明确缓存年龄未知", () => {
    const prepare = vi.fn();
    render(<SourceHealthPanel report={report} onSupplement={prepare} />);
    expect(screen.getByText(/失败原因：连接失败/)).toBeTruthy();
    expect(screen.getByText(/日期无法核验/)).toBeTruthy();
    expect(screen.getAllByText(/缓存年龄：未知/)).toHaveLength(4);
    expect(screen.getAllByRole("button")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "准备补充研究 · S1" }));
    expect(prepare).toHaveBeenCalledWith(expect.stringContaining("连接失败"));
  });
  it("未保存报告和正在追问时不能准备新补研", () => {
    const prepare = vi.fn();
    const { rerender } = render(<SourceHealthPanel report={{ ...report, report_id: undefined }} onSupplement={prepare} />);
    expect((screen.getAllByRole("button")[0] as HTMLButtonElement).disabled).toBe(true);
    rerender(<SourceHealthPanel report={report} onSupplement={prepare} busy />);
    expect((screen.getAllByRole("button")[0] as HTMLButtonElement).disabled).toBe(true);
    expect(prepare).not.toHaveBeenCalled();
  });
  it("在报告概览准备问题后打开现有追问页签，由用户继续提交", () => {
    const prepare = vi.fn();
    render(<ReportResultTabs report={report} followup={<p>补研表单</p>} onPrepareSupplement={prepare} />);
    fireEvent.click(screen.getByRole("button", { name: "准备补充研究 · S2" }));
    expect(prepare).toHaveBeenCalledWith(expect.stringContaining("观测日期超过阈值"));
    expect(screen.getByText("补研表单")).toBeTruthy();
  });
});
