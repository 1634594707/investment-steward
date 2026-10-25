import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeepAnalysisBlocks } from "./panels";
import { buildStockReportMarkdown, markdownToHtml } from "./export";
import type { CashFlowQualityBlock, StockReport } from "./researchTypes";

function delivery(cash: CashFlowQualityBlock) {
  const report: StockReport = {
    ok: true, symbol: "600618", title: "分期现金流验收", report: "测试正文。", citations: [], limitations: [],
    confidence: "medium", model: "test", source_keys: [], source_errors: {}, generated_at: "2026-10-25T00:00:00Z",
    cash_flow_quality: cash,
  };
  // 多期间会重复缺口提示；实际渲染不能因此产生重复 key 或在更新时吞掉行。
  const errors = vi.spyOn(console, "error");
  let pageText: string;
  try {
    const { container, rerender } = render(<DeepAnalysisBlocks report={report} />);
    rerender(<DeepAnalysisBlocks report={{ ...report, cash_flow_quality: { ...cash } }} />);
    pageText = container.textContent ?? "";
    expect(errors.mock.calls.filter((args) => args.some((arg) => typeof arg === "string" && arg.includes("same key")))).toEqual([]);
  } finally {
    errors.mockRestore();
  }
  const markdown = buildStockReportMarkdown(report);
  const html = new DOMParser().parseFromString(markdownToHtml(markdown), "text/html");
  return [pageText, markdown, html.body.textContent ?? ""];
}

describe("R01/R02 页面与两种导出的现金流交付口径", () => {
  it("TTM 与半年逐组保留期间、单位和来源，不把资金缺口当盈利质量结论", () => {
    const outputs = delivery({ verdict: "分期列示", periods: [
      { operating_cash_flow: 10.08, net_income: 7.93, coverage_ratio: 1.27,
        metric_metadata: {
          operating_cash_flow: { period: "TTM-2026H1", unit: "亿元", sources: ["S4"] },
          net_income: { period: "TTM-2026H1", unit: "亿元", sources: ["S4"] },
        } },
      { operating_cash_flow: 3.43, net_income: 3.73, coverage_ratio: 0.92, capex: 6.06,
        free_cash_flow: -2.63, free_cash_flow_derived: true, free_cash_flow_reported: 99,
        verdict: "不足以判断盈利质量", funding_balance_note: "资本开支后资金缺口 2.63（亿元），不单独证明盈利质量。",
        metric_metadata: {
          operating_cash_flow: { period: "2026H1", unit: "亿元", sources: ["S4"] },
          net_income: { period: "2026H1", unit: "亿元", sources: ["S4"] },
          capex: { period: "2026H1", unit: "亿元", sources: ["S4"] },
          free_cash_flow: { period: "2026H1", unit: "亿元", sources: ["S4"] },
        } },
    ] });
    for (const output of outputs) {
      const first = output.slice(output.indexOf("现金流分期 1"), output.indexOf("现金流分期 2"));
      const second = output.slice(output.indexOf("现金流分期 2"));
      expect(first).toContain("经营现金流 10.08（统计期：TTM-2026H1；单位：亿元；模型标注来源：S4）");
      expect(first).toContain("覆盖倍数 1.27");
      expect(first).not.toContain("资本开支 6.06");
      expect(second).toContain("经营现金流 3.43（统计期：2026H1；单位：亿元；模型标注来源：S4）");
      expect(second).toContain("覆盖倍数 0.92");
      expect(second).toContain("自由现金流 -2.63（统计期：2026H1；单位：亿元；模型标注来源：S4；同期间输入复算）");
      expect(second).toContain("模型提供的自由现金流 99（原值留痕，不代表核验通过）");
      expect(second).toContain("不单独证明盈利质量");
      expect(second).toContain("不足以判断盈利质量");
    }
  });

  it("停止跨期计算的结果如实为空，不从模型原值补回派生结论", () => {
    for (const output of delivery({ operating_cash_flow: 10.08, capex: 6.06,
      operating_cash_flow_period: "TTM-2026H1", capex_period: "2026H1",
      free_cash_flow: null, free_cash_flow_derived: false, free_cash_flow_reported: 99,
      verdict: "不足以判断盈利质量", downgrade_note: "统计期不一致，停止自由现金流复算。",
    })) {
      expect(output).toContain("自由现金流 未取到");
      expect(output).toContain("单位：未标注；模型标注来源：未标注");
      expect(output).toContain("模型提供的自由现金流 99（原值留痕，不代表核验通过）");
      expect(output).toContain("统计期不一致，停止自由现金流复算。");
      expect(output).not.toContain("自由现金流 4.02");
      expect(output).not.toContain("同期间输入复算");
    }
  });

  it("分期下的降档说明也必须保留，不能只保留顶层限制", () => {
    for (const output of delivery({ verdict: "分期列示", periods: [{
      operating_cash_flow: 3.43, verdict: "不足以判断盈利质量",
      downgrade_note: "本分期缺少折旧摊销及营运资本变动证据。",
    }] })) expect(output).toContain("本分期缺少折旧摊销及营运资本变动证据。");
  });
});
