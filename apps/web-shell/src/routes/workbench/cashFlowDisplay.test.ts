import { describe, expect, it } from "vitest";
import { cashFlowDisplayLines } from "./format";

describe("现金流统计口径披露", () => {
  it("资金余缺单独披露，避免用负自由现金流替代盈利质量判断", () => {
    const lines = cashFlowDisplayLines({ free_cash_flow: -2.63,
      funding_balance_note: "资本开支后资金缺口 2.63（亿元），不单独证明盈利质量。" });
    expect(lines).toContain("资本开支后资金余缺：资本开支后资金缺口 2.63（亿元），不单独证明盈利质量。");
  });
  it("附加分期不会隐藏顶层原值，零值也须保留", () => {
    const lines = cashFlowDisplayLines({ operating_cash_flow: 0, free_cash_flow_reported: 99,
      periods: [{ operating_cash_flow: 3.43 }] });
    expect(lines).toContain("原始顶层口径");
    expect(lines.some((line) => line.startsWith("经营现金流 0（"))).toBe(true);
    expect(lines.some((line) => line.includes("模型提供的自由现金流 99"))).toBe(true);
    expect(lines.some((line) => line.startsWith("经营现金流 3.43（"))).toBe(true);
    expect(lines.filter((line) => line.startsWith("现金流分期"))).toHaveLength(1);
  });
  it("多期间输入逐组展示，不把 TTM 经营现金流与半年资本开支排成同一组", () => {
    const lines = cashFlowDisplayLines({ periods: [
      { operating_cash_flow: 10.08, operating_cash_flow_period: "TTM-2026H1" },
      { operating_cash_flow: 3.43, operating_cash_flow_period: "2026H1", capex: 6.06, capex_period: "2026H1", free_cash_flow: -2.63, free_cash_flow_derived: true },
    ] });
    expect(lines.filter((line) => line.startsWith("现金流分期"))).toHaveLength(2);
    expect(lines.indexOf("现金流分期 2")).toBeGreaterThan(lines.findIndex((line) => line.includes("TTM-2026H1")));
    expect(lines.some((line) => line.includes("自由现金流 -2.63"))).toBe(true);
  });
  it("保留每项的期间、单位和来源，避免把跨期数字读成同一组", () => {
    const lines = cashFlowDisplayLines({
      operating_cash_flow: 10.08,
      capex: 6.06,
      metric_metadata: {
        operating_cash_flow: { period: "TTM-2026H1", unit: "亿元", sources: ["S4"] },
        capex: { period: "2026H1", unit: "亿元", sources: ["S4"] },
      },
    });
    expect(lines[0]).toContain("TTM-2026H1；单位：亿元；模型标注来源：S4");
    expect(lines[2]).toContain("2026H1；单位：亿元；模型标注来源：S4");
    expect(lines[1]).toContain("统计期：未标注；单位：未标注");
  });

  it("旧数据不补造金额单位或来源", () => {
    const lines = cashFlowDisplayLines({ operating_cash_flow: 10.08, operating_cash_flow_period: "TTM-2026H1" });
    expect(lines[0]).toContain("统计期：TTM-2026H1；单位：未标注；模型标注来源：未标注");
    expect(lines[3]).toContain("未取到");
    expect(lines[3]).toContain("复算状态未核验");
  });
});
