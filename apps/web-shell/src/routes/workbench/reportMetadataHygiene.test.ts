/**
 * J07 / J09 / J10（桌面端升级路线图 2026-09-18）：导出件的头部与表格不得有噪声行。
 *
 * 样报事实（002468 / 2026-09-18 18:30）：
 * - `样报:9`/`样报:10` 两行来源列表逐字相同；`样报:11` 与 `样报:6` 重复字数；
 *   `样报:12`/`样报:13` 两处写提示词版本；
 * - `样报:80-82` 乐观/中性/悲观三档概率全是「中」，看起来像给了概率；
 * - `样报:98`/`样报:99` 「验证时点」与「到期日」两列同值；
 * - `样报:106` A 级来源为 0，却写在第 100+ 行，而 `样报:105` 的口径自写「核心结论需 A/B 级及以上」。
 */
import { describe, expect, it } from "vitest";
import type { ReportScenario, StockReport } from "./researchTypes";
import { buildStockReportMarkdown } from "./export";
import { scenarioProbabilityWording } from "./format";

function report(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "002468",
    title: "申通快递：测试",
    report: "### 技术面\n收盘 14.90。",
    citations: ["S1", "S2", "S3", "S4", "S5"],
    available_citations: ["S1", "S2", "S3", "S4", "S5"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-18T18:30:53Z",
    report_chars: 1839,
    report_mode: "deep",
    executive_summary: "技术信号偏多未确认，PE 低分位、PB 中高分位分化。",
    prompt_policy_version: "2.8",
    generation_trace: { prompt_version: "2.8", stages_completed: ["draft"], report_mode: "deep" },
    ...overrides,
  } as StockReport;
}

function scenario(name: string, probabilityLevel: string) {
  return { name, trigger: "t", probability: null, probability_level: probabilityLevel, horizon: "10 个交易日", invalidates: "i", source: "S1" };
}

describe("J07 头部去噪", () => {
  it("生成留痕放入附录，结论限制无摘要也保留在正文前", () => {
    const md = buildStockReportMarkdown(report({ executive_summary: "", summary_downgrade_notes: ["测试限制：证据已过期，须先补证"] }));
    expect(md.indexOf("## 结论使用限制")).toBeLessThan(md.indexOf("## 研报正文"));
    expect(md).toContain("测试限制：证据已过期，须先补证");
    expect(md.indexOf("## 生成记录附录")).toBeGreaterThan(md.indexOf("## 研报正文"));
    expect(md.indexOf("- 生成留痕：")).toBeGreaterThan(md.indexOf("## 生成记录附录"));
    expect(md.match(/提示词 2\.8/g)).toHaveLength(1);
  });

  it("引用满可用来源时只有一行来源，不再逐字重复", () => {
    const md = buildStockReportMarkdown(report());
    // 只统计「引用来源」类行：「阅读前核对」里的 `- 数据来源：…` 是另一节的口径说明，
    // 用宽泛的 `includes("来源：")` 会把它误算成重复行（2026-10-26 起该行恒有数据基础）。
    const sourceLines = md
      .split("\n")
      .filter(
        (line) =>
          line.startsWith("- 引用来源：")
          || line.startsWith("- 未引用可得来源：")
          || line.startsWith("- 本次可引用来源："),
      );
    expect(sourceLines).toEqual(["- 引用来源：S1、S2、S3、S4、S5"]);
    expect(md).not.toContain("本次可引用来源");
  });

  it("真有未引用的可得来源时才提示差异", () => {
    const md = buildStockReportMarkdown(report({ citations: ["S1", "S2"], available_citations: ["S1", "S2", "S5"] }));
    expect(md).toContain("- 未引用可得来源：S5");
  });

  it("字数与提示词版本各只出现一次（留痕里已含版本时不再重复）", () => {
    const md = buildStockReportMarkdown(report());
    expect(md).not.toContain("- 正文字数：");
    expect(md.match(/提示词/g)).toHaveLength(1);
    expect(md).toContain("报告规格：深研版 · 1839 字");
  });

  it("留痕版本与策略版本不一致时仍单独暴露（版本错位是真信号）", () => {
    const md = buildStockReportMarkdown(report({ prompt_policy_version: "2.9" }));
    expect(md).toContain("- 提示词策略版本：2.9");
  });
});

describe("J09 概率三档同值", () => {
  it("三档同为「中」时按未给概率呈现并补口径说明", () => {
    const md = buildStockReportMarkdown(report({
      scenarios: [scenario("乐观", "中"), scenario("中性", "中"), scenario("悲观", "中")],
    }));
    expect(md).toContain("—（未给概率）");
    expect(md).toContain("情景倾向存在重复档（模型未做区分），按「未给概率」处理");
    expect(md).not.toMatch(/\| 乐观 \| t \| 中 \|/);
  });

  /**
   * G3（研报产出与交付链路路线图 2026-09-27）：降级阈值从「三档全同」放宽到「存在重复档」。
   * 样报 002468 是乐观=中 / 中性=中 / 悲观=低——非全同，旧口径不触发，两条「中」照原样印出，
   * 读起来仍像做了概率区分。现在两档相同即按「未给概率」处理。
   */
  it("两档相同（中/中/低）也按未给概率降级，不再照排两条「中」", () => {
    const md = buildStockReportMarkdown(report({
      scenarios: [scenario("乐观", "中"), scenario("中性", "中"), scenario("悲观", "低")],
    }));
    expect(md).toContain("—（未给概率）");
    expect(md).toContain("情景倾向存在重复档（模型未做区分），按「未给概率」处理");
    expect(md).not.toMatch(/\| 乐观 \| t \| 中 \|/);
  });

  /**
   * Q08（2026-09-19 路线图）：probability_basis 恒为 none（无后验基准），
   * 列名叫「概率」就会被读成上涨概率；未统计校准的取值只能叫「倾向（未统计校准）」，
   * 且屏显（panels.ScenarioFalsifiability）与导出共用 format.scenarioProbabilityWording。
   */
  it("概率列表头写「倾向（未统计校准）」，不再写成「概率」", () => {
    const md = buildStockReportMarkdown(report({
      scenarios: [scenario("乐观", "高"), scenario("中性", "中"), scenario("悲观", "低")],
    }));
    expect(md).toContain("| 情景 | 触发条件 | 倾向（未统计校准） | 预期区间 |");
    expect(md).toContain("不等于上涨概率");
    expect(scenarioProbabilityWording(undefined, [scenario("乐观", "高")] as unknown as ReportScenario[]).column).toBe("倾向（未统计校准）");
  });

  it("概率有差异时照排原值", () => {
    const md = buildStockReportMarkdown(report({
      scenarios: [scenario("乐观", "高"), scenario("中性", "中"), scenario("悲观", "低")],
    }));
    expect(md).toContain("| 乐观 | t | 高 |");
    expect(md).not.toContain("未给概率");
  });
});

describe("J10 验证点列与 A 级提示", () => {
  it("到期日与验证时点同值时只占一列", () => {
    const md = buildStockReportMarkdown(report({
      watchpoints: [{
        signal: "N 字上涨足额确认", verify_by: "2026-09-22", due_on: "2026-09-22", status: "pending_review", expected_if_true: "转确认",
      }],
    }));
    expect(md).toContain("| 观察信号 | 到期（含来源） | 状态 | 可回填 | 若成立则预期 |");
    expect(md).toContain("| N 字上涨足额确认 | 2026-09-22 | 待复盘 | 可回填 |");
    expect(md).not.toContain("2026-09-22 | 2026-09-22");
  });

  it("事件锚定（无确切日期）不被填成假日期", () => {
    const md = buildStockReportMarkdown(report({
      watchpoints: [{
        signal: "三季报增速与现金流", verify_by: "2026年三季报披露后", due_on: "", status: "pending_review", expected_if_true: "支撑增强",
      }],
    }));
    expect(md).toContain("| 三季报增速与现金流 | 2026年三季报披露后 | 待复盘 | 锚定事件·不可自动回填 |");
  });

  // G2（2026-09-27 路线图）：5 条验证点里 2 条锚定事件结构上永远无法自动回填，
  // 导出件此前只写「待复盘」，读者无从知道哪几条点不动。现在列里显式标出，
  // 并在表下写清口径（含「手工标记已复核」这个人工出口）。
  it("可回填列区分带日期与锚定事件，并给出计数口径", () => {
    const md = buildStockReportMarkdown(report({
      watchpoints: [
        { signal: "到期点", verify_by: "2026-09-28", due_on: "2026-09-28", status: "pending_review", expected_if_true: "a" },
        { signal: "锚定点一", verify_by: "三季报披露后", due_on: "", status: "pending_review", expected_if_true: "b" },
        { signal: "锚定点二", verify_by: "下次月度经营简报披露后", due_on: "", status: "pending_review", expected_if_true: "c" },
      ],
    }));
    expect(md).toContain("| 到期点 | 2026-09-28 | 待复盘 | 可回填 |");
    expect(md).toContain("| 锚定点一 | 三季报披露后 | 待复盘 | 锚定事件·不可自动回填 |");
    expect(md).toContain("| 锚定点二 | 下次月度经营简报披露后 | 待复盘 | 锚定事件·不可自动回填 |");
    expect(md).toContain("共 3 条验证点，1 条带确切日期");
    expect(md).toContain("2 条锚定事件无确切日期");
    expect(md).toContain("手工标记已复核");
  });

  it("A 级为 0 时在头部提示，A 级有条目时不打扰", () => {
    const noA = buildStockReportMarkdown(report({
      evidence_quality: { tiers: { A: [], B: ["S1", "S4"], C: ["S2", "S5"], D: [] } } as never,
    }));
    expect(noA).toContain("- 证据资格提示：**本次无一手公告/财报原文（A 级 0 条）**");
    expect(noA.indexOf("证据资格提示")).toBeLessThan(noA.indexOf("## 执行摘要"));

    const withA = buildStockReportMarkdown(report({
      evidence_quality: { tiers: { A: ["S3"], B: ["S1"], C: [], D: [] } } as never,
    }));
    expect(withA).not.toContain("证据资格提示：**本次无一手");
  });

  it("D1：拿到 A 级一手来源时头部正向说明，并点名是哪个来源", () => {
    const md = buildStockReportMarkdown(report({
      evidence_quality: { tiers: { A: ["S3A"], B: ["S1", "S3", "S4"], C: ["S2", "S5"], D: [] } } as never,
    }));
    expect(md).toContain("本次含 A 级一手来源 S3A");
    expect(md).toContain("经数据商转载");
    expect(md.indexOf("证据资格提示")).toBeLessThan(md.indexOf("## 执行摘要"));
  });

  it("D1：本次 A 级说明（note）随导出走——离开界面后仍能自证为什么有/没有 A 级", () => {
    const md = buildStockReportMarkdown(report({
      evidence_quality: {
        version: "v2",
        sources: [],
        tiers: { A: [], B: ["S1", "S3", "S4"], C: ["S2", "S5"], D: [] },
        coverage_vocab: [],
        core_support_qualities: ["A", "B"],
        note: "本次**未取到公告正文全文**（S3 仅标题口径，故按 B 级如实标注）",
      } as never,
    }));
    expect(md).toContain("## 证据质量与结论支撑");
    expect(md).toContain("- 本次 A 级说明：本次**未取到公告正文全文**");
  });
});
