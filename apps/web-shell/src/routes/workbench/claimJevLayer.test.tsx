/**
 * JV04 前端测试：研报「证据质量与结论支撑」面板如何呈现**语义支撑层**（Jev 逐 claim 判定）。
 *
 * 要守住的三件事：
 * 1. **三态可区分**——「这一层没跑」/「跑过且有降级」/「跑过且通过」不能长一个样；
 * 2. **没跑 ≠ 通过**——未进语义比对的判断必须逐条说出原因，不能静默；
 * 3. **不改写原话**——语义判定与确定性结论**并列展示**，两者不一致时都要看得见。
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import type { ClaimFindings, ClaimJevLayer, StockReport } from "./researchTypes";
import { EvidenceQualityPanel } from "./panels";
import { buildStockReportMarkdown } from "./export";

function findings(overrides: Partial<ClaimFindings> = {}): ClaimFindings {
  return {
    claims: [
      {
        claim_id: "C1",
        text: "营收与现金流同步改善",
        sources: ["S4"],
        dropped_sources: [],
        requires: ["revenue", "cash_flow"],
        unknown_requires: [],
        missing_coverage: [],
        evidence_quality: "B",
        status: "supported",
        note: "",
        importance: "high",
        support: "partial",
        support_label: "部分支持",
        support_source: "jev_semantic",
        jev: {
          support: "unsupport",
          support_label: "语义不支撑",
          confidence: 0.88,
          invented: "no",
          invented_value: 0.05,
          sources_checked: ["S4"],
        },
      },
      {
        claim_id: "C2",
        text: "中标大单带动订单增长",
        sources: ["S2"],
        dropped_sources: [],
        requires: ["revenue"],
        unknown_requires: [],
        missing_coverage: [],
        evidence_quality: "C",
        status: "downgraded",
        note: "",
        importance: "high",
        support: "partial",
        support_label: "部分支持",
      },
    ],
    total: 2,
    supported: 0,
    downgraded: ["C2"],
    no_source: [],
    core_conclusion_supported: false,
    core_conclusion_state: "unsupported",
    core_support_counts: { supported: 0, total: 2 },
    warnings: [],
    ...overrides,
  };
}

function jevLayer(overrides: Partial<ClaimJevLayer> = {}): ClaimJevLayer {
  return {
    available: true,
    evaluated: 2,
    skipped: [],
    downgraded: ["C1"],
    weakened: [],
    invented: [],
    model: "jev-1.13.0",
    schema_version: "1.0",
    partial_downgrades: false,
    ...overrides,
  };
}

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "600000",
    title: "测试研报",
    report: "### 技术面\nMA20 之上运行。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-21T00:00:00Z",
    report_chars: 2153,
    report_mode: "standard",
    ...overrides,
  };
}

describe("EvidenceQualityPanel · JV04 语义支撑层", () => {
  it("可用且有降级：头部给出降级条数与模型版本，逐条标出语义判定", () => {
    const { container } = render(
      <EvidenceQualityPanel report={baseReport({ claim_findings: findings(), jev: jevLayer() })} />,
    );
    const head = container.querySelector(".wb-evidence-head")!.textContent!;
    expect(head).toContain("语义支撑 1 条降级");
    expect(head).toContain("语义比对 2 条 · jev-1.13.0");

    const text = container.querySelector(".wb-evidence-table")!.textContent!;
    // 语义判定与确定性结论并列（同一条判断上「完整支持/部分支持」与「语义不支撑」都要看得见）
    expect(text).toContain("语义 · 语义不支撑");
    expect(text).toContain("支撑强度由语义层改写");
    // 无编造风险时不加噪声标签
    expect(text).not.toContain("疑似编造事实");
  });

  it("疑似编造：头部与逐条都出强信号", () => {
    const claimFindings = findings();
    claimFindings.claims[0]!.jev = {
      support: "support", support_label: "语义支撑", confidence: 0.7,
      invented: "yes", invented_value: 0.93, sources_checked: ["S4"],
    };
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({ claim_findings: claimFindings, jev: jevLayer({ invented: ["C1"] }) })}
      />,
    );
    expect(container.querySelector(".wb-evidence-head")!.textContent).toContain("1 条疑似编造事实");
    expect(container.querySelector(".wb-evidence-table")!.textContent).toContain("疑似编造事实");
    expect(container.textContent).toContain("疑似编造：C1");
  });

  it("「未校验」必须说出是哪一种原因，不得静默当作通过", () => {
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({
          claim_findings: findings(),
          jev: jevLayer({
            evaluated: 1,
            skipped: [
              { claim_id: "C3", reason: "no_cited_source" },
              { claim_id: "C4", reason: "over_state_budget" },
            ],
            weakened: ["C2"],
          }),
        })}
      />,
    );
    const text = container.textContent!;
    expect(text).toContain("未进语义比对 2 条");
    expect(text).toContain("C3（无有效引用可比对）");
    expect(text).toContain("C4（超出单次评估预算）");
    expect(text).toContain("「未校验」不等于「通过」");
    // 弱信号只标注不降判定——这句必须说出来，否则用户以为它被降级了
    expect(text).toContain("语义认为强度不足但未改判定：C2");
  });

  it("该层未运行（available=false）：如实标注，并说明只跑了确定性层", () => {
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({
          claim_findings: findings({ claims: findings().claims.map(({ jev: _jev, support_source: _s, ...rest }) => rest) }),
          jev: jevLayer({
            available: false, evaluated: 0, downgraded: [], model: "",
            note: "语义支撑层未取得应答，判定按确定性口径如实保留。",
          }),
        })}
      />,
    );
    expect(container.querySelector(".wb-evidence-head")!.textContent).toContain("语义支撑层未运行");
    expect(container.textContent).toContain("本次未校验");
    expect(container.textContent).toContain("按未做语义核验对待");
    // 不可用时不得出现「语义比对 N 条」这种像跑过的措辞
    expect(container.textContent).not.toContain("语义比对");
  });

  it("旧档案没有 jev 字段：整块不出现语义层措辞（不猜、不补）", () => {
    const legacy = findings();
    legacy.claims = legacy.claims.map(({ jev: _jev, support_source: _s, ...rest }) => rest);
    const { container } = render(
      <EvidenceQualityPanel report={baseReport({ claim_findings: legacy })} />,
    );
    const text = container.textContent!;
    // 不得出现任何「像跑过语义层」的措辞（回执标签 / 逐条判定 / 编造标签）
    expect(text).not.toContain("语义比对");
    expect(text).not.toContain("语义支撑层未运行");
    expect(text).not.toContain("语义 · ");
    expect(text).not.toContain("疑似编造");
    // 但要如实告诉用户这一层没跑，不能留白让人以为已核验
    expect(text).toContain("支撑判定只跑了确定性层");
    expect(text).toContain("按未做语义核验对待");
  });
});

describe("导出件 · JV04 语义支撑层", () => {
  it("可用时写入语义列与来源说明，含降级与编造信号", () => {
    const claimFindings = findings();
    claimFindings.claims[0]!.jev = {
      support: "support", support_label: "语义支撑", confidence: 0.7,
      invented: "yes", invented_value: 0.93, sources_checked: ["S4"],
    };
    const md = buildStockReportMarkdown(
      baseReport({ claim_findings: claimFindings, jev: jevLayer({ invented: ["C1"] }) }),
    );
    expect(md).toContain("语义支撑（Jev）");
    expect(md).toContain("语义支撑层（Jev jev-1.13.0）");
    expect(md).toContain("内容**是否支撑");
    expect(md).toContain("疑似编造");
    // 逐条单元格写的是纯标签（不带「语义 ·」前缀，那是屏显的视觉分隔）
    expect(md).toContain("| 语义支撑 · 疑似编造 |");
    // C2 没有语义回执 → 写「未比对」（该层跑了但这条没进），不是「该层未运行」
    expect(md).toContain("| 未比对 |");
  });

  it("可用但该条没进语义比对：写「未比对」，与「该层未运行」严格区分", () => {
    const md = buildStockReportMarkdown(
      baseReport({ claim_findings: findings(), jev: jevLayer({ skipped: [{ claim_id: "C2", reason: "over_state_budget" }] }) }),
    );
    expect(md).toContain("| 未比对 |");
    expect(md).not.toContain("该层未运行 |");
    expect(md).toContain("C2（超出单次评估预算）");
  });

  it("未进该层的判断逐条写清原因，「未校验」不等于「通过」", () => {
    const md = buildStockReportMarkdown(
      baseReport({
        claim_findings: findings(),
        jev: jevLayer({ skipped: [{ claim_id: "C3", reason: "no_cited_source" }], weakened: ["C2"] }),
      }),
    );
    expect(md).toContain("未进语义比对 1 条：C3（无有效引用可比对）");
    expect(md).toContain("「未校验」不等于「通过」");
    expect(md).toContain("语义认为强度不足但**未改判定**：C2");
  });

  it("该层未运行时如实写「未运行」，不写「未比对」冒充跑过", () => {
    const md = buildStockReportMarkdown(
      baseReport({ claim_findings: findings(), jev: jevLayer({ available: false, evaluated: 0, downgraded: [], model: "" }) }),
    );
    expect(md).toContain("语义支撑层**未运行**");
    expect(md).toContain("未校验");
    expect(md).not.toContain("语义支撑层（Jev");
  });

  it("没有 jev 字段的旧档案：整块不出现语义措辞（不猜、不补）", () => {
    const legacy = findings();
    legacy.claims = legacy.claims.map(({ jev: _jev, support_source: _s, ...rest }) => rest);
    const md = buildStockReportMarkdown(baseReport({ claim_findings: legacy }));
    expect(md).not.toContain("语义支撑（Jev）");
    expect(md).not.toContain("语义支撑层");
  });
});
