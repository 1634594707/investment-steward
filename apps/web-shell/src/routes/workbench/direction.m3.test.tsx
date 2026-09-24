/**
 * M3（D01–D08）前端契约测试：报告形态徽章 / 前置行业比较表 / 双池分离 /
 * 反方审查定稿留痕 / 确定性检查复核块 / 旧报告兼容（缺字段不渲染）。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { DirectionReport } from "./researchTypes";
import {
  DirectionCounterCheckCard,
  DirectionIndustryComparison,
  DirectionPoolTable,
  DirectionQualityReview,
  DirectionResultTabs,
} from "./direction";

const noopSend = vi.fn();

function baseReport(overrides: Partial<DirectionReport> = {}): DirectionReport {
  return {
    ok: true,
    topic: "快递与航运",
    direction_summary: "正文",
    catalysts: [],
    risks: [],
    model: "m",
    generated_at: "2026-09-15T00:00:00Z",
    report_id: "dir-m3",
    stock_pool: [],
    ...overrides,
  } as DirectionReport;
}

describe("M3-D02：前置行业比较表", () => {
  it("渲染比较表全列（估值/盈利/催化与期限/反证/优先级/依据/完整度）", () => {
    render(<DirectionIndustryComparison report={baseReport({
      industry_comparison: [{
        industry: "快递",
        valuation_evidence: "PB 分位 22% [E1]",
        profit_change: "件量 +4.1%、单票收入 7.63 元",
        catalyst: "旺季提价",
        horizon: "2026Q4",
        counter_evidence: "价格战重启",
        research_priority: "高",
        priority_basis: "估值分位低且催化临近",
        evidence_completeness: "估值可得、分位缺失",
      }],
    })} />);
    const table = screen.getByTestId("direction-industry-comparison");
    for (const text of ["快递", "PB 分位 22% [E1]", "2026Q4", "高", "估值分位低且催化临近", "估值可得、分位缺失"]) {
      expect(table.textContent).toContain(text);
    }
  });

  it("优先级缺依据 → 显式提示须补（不编造评分）", () => {
    render(<DirectionIndustryComparison report={baseReport({
      industry_comparison: [{ industry: "航运", research_priority: "高", priority_basis: "" }],
    })} />);
    expect(screen.getByTestId("direction-industry-comparison").textContent).toContain("缺依据");
  });

  it("旧报告（无 industry_comparison）整块不渲染", () => {
    const { container } = render(<DirectionIndustryComparison report={baseReport()} />);
    expect(container.querySelector("[data-testid='direction-industry-comparison']")).toBeNull();
  });
});

describe("M3-D06：低估候选池 / 待验证研究池", () => {
  it("rows/title 生效：同一组件渲染两个视图，计数按传入子集", () => {
    const report = baseReport({
      stock_pool: [],
      low_valuation_pool: [{
        symbol_raw: "600233", symbol: "600233", name: "圆通速递", sector: "物流",
        business_link: "快递件量", profit_path: "单票收入", identity_status: "verified",
      }],
      research_pool: [
        {
          symbol_raw: "002352", symbol: "002352", name: "顺丰控股", sector: "物流",
          business_link: "时效件", profit_path: "单票", identity_status: "unverified",
        },
        {
          symbol_raw: "601006", symbol: "601006", name: "大秦铁路", sector: "煤炭行业",
          business_link: "货运", profit_path: "运价", identity_status: "verified",
        },
      ],
    } as Partial<DirectionReport>);
    render(<DirectionPoolTable report={report} rows={report.research_pool} title="待验证研究池（2 家）" note="不得当作已确认低估" onSendToTactics={noopSend} onStudySingle={vi.fn()} />);
    const table = screen.getByTestId("direction-pool-table");
    expect(table.textContent).toContain("待验证研究池（2 家）");
    expect(table.textContent).toContain("不得当作已确认低估");
    // 只渲染传入子集：低估池候选不出现在待验证表内
    expect(table.textContent).not.toContain("圆通速递");
  });
});

describe("M3-D05：反方审查定稿留痕", () => {
  it("已执行修订 → 展示撤下/降级清单与标注处数", () => {
    render(<DirectionCounterCheckCard report={baseReport({
      counter_check: { ok: true, strongest_counter: "需求前置透支" } as never,
      counter_check_application: {
        applied: true,
        removed_judgment_ids: ["J1"],
        downgraded_judgment_ids: ["J2", "J3"],
        annotated_sentences: 2,
        annotation_misses: [],
        catalysts_untouched_reason: "催化与判断无稳定映射",
      },
    })} />);
    const node = screen.getByTestId("direction-counter-application");
    expect(node.textContent).toContain("J1");
    expect(node.textContent).toContain("降级判断 2 条");
    expect(node.textContent).toContain("已在正文加行内标注 2 处");
  });

  it("v2：一条都没标注上时，不得声称「已标注」，且说明已改在正文开头集中列出", () => {
    render(<DirectionCounterCheckCard report={baseReport({
      counter_check: { ok: true, strongest_counter: "核心估值数据全缺" } as never,
      counter_check_application: {
        applied: true,
        removed_judgment_ids: ["J1", "J2", "J3"],
        downgraded_judgment_ids: [],
        annotated_sentences: 0,
        body_block_entries: 3,
        annotation_misses: ["甲判断", "乙判断", "丙判断"],
      },
    })} />);
    const node = screen.getByTestId("direction-counter-application");
    expect(node.textContent).toContain("正文未匹配到可标注的句子");
    expect(node.textContent).not.toContain("已在正文加行内标注");
    expect(node.textContent).toContain("在正文开头集中列出");
  });
});

describe("M3-D03/D04/D07/D08：确定性检查复核块", () => {
  it("无检查项时不渲染；有检查项时逐类展示", () => {
    const { container, rerender } = render(<DirectionQualityReview report={baseReport()} />);
    expect(container.querySelector("[data-testid='direction-quality-review']")).toBeNull();

    rerender(<DirectionQualityReview report={baseReport({
      cross_layer_claims: [{ section: "需求供给与价格", sentence: "低 CPI 推动利润上行", macro_terms: ["CPI"], profit_terms: ["利润"] }],
      cycle_normalization_gaps: [{ board: "煤炭行业", reason: "未披露正常化口径" }],
      reference_support_problems: [{ section: "景气阶段", refs: ["E1"], numbers: ["9.9"], sentence: "同比 9.9% [E1]" }],
      repeated_gaps: [{ gap: "分企量价未接入", sections: ["需求供给与价格", "景气阶段", "竞争与替代"] }],
      research_pool_mislabeled: ["600233（「需求供给与价格」小节）"],
    })} />);
    const block = screen.getByTestId("direction-quality-review");
    for (const text of ["跨层推断", "周期正常化", "引用不支持", "缺口重复", "待验证对象被表述为低估"]) {
      expect(block.textContent).toContain(text);
    }
  });
});
