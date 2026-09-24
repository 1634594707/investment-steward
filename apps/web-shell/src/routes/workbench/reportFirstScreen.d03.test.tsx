/**
 * D03（前端设计与架构优化任务路线图 2026-09-19）：研报首屏的判断边界层。
 * 「局限与口径声明」与「验证点」须与结论同屏；原文缺哪一项就写缺哪一项，不从别处补写。
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { StockReport } from "./researchTypes";
import { ReportFirstScreen } from "./panels";

function reportWith(overrides: Partial<StockReport>): StockReport {
  return {
    symbol: "600519",
    title: "T",
    limitations: [],
    generated_at: "2026-09-19T00:00:00Z",
    ...overrides,
  } as unknown as StockReport;
}

describe("ReportFirstScreen 判断边界（D03）", () => {
  it("局限与验证点与结论同屏，验证点最多三条并注明其余去处", () => {
    render(<ReportFirstScreen report={reportWith({
      limitations: ["财报口径为单季，未含关联交易"],
      watchpoints: [
        { signal: "批价回升", verify_by: "中秋前", due_on: "2026-09-30", expected_if_true: "渠道提价能力恢复" },
        { signal: "库存去化", verify_by: "三季报披露后", expected_if_true: "批价企稳" },
        { signal: "直销占比", verify_by: "2026 年内", expected_if_true: "吨价上移" },
        { signal: "海外收入", verify_by: "2027 年内", expected_if_true: "第二曲线" },
      ],
    })} />);
    const guard = screen.getByTestId("report-firstscreen-guard");
    expect(guard.textContent).toContain("局限与口径声明");
    expect(guard.textContent).toContain("财报口径为单季，未含关联交易");
    expect(guard.textContent).toContain("批价回升 · 验证时点 2026-09-30");
    // due_on 缺失时回落到 verify_by 原文，不猜日期。
    expect(guard.textContent).toContain("库存去化 · 验证时点 三季报披露后");
    expect(guard.textContent).toContain("另有 1 项，见「情景与失效条件」。");
    expect(guard.textContent).not.toContain("海外收入");
  });

  it("原文缺限制与验证点：如实说明缺什么，不补写", () => {
    render(<ReportFirstScreen report={reportWith({ limitations: [], watchpoints: [] })} />);
    const guard = screen.getByTestId("report-firstscreen-guard");
    expect(guard.textContent).toContain("原报告未给出局限声明。");
    expect(guard.textContent).toContain("原报告未给出验证点。");
  });

  it("旧报告两类字段都没有（undefined）时同样不报错、不编造", () => {
    render(<ReportFirstScreen report={reportWith({})} />);
    expect(screen.getByTestId("report-firstscreen-guard").textContent).toContain("原报告未给出局限声明。");
  });
});
