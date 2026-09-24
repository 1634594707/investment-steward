/**
 * J01 / J02（桌面端升级路线图 2026-09-18）：估值节的屏显与导出不得自相矛盾、不得占空表。
 *
 * 样报事实（002468 / 2026-09-18 18:30）：
 * - `样报:56` 「结论 = 偏便宜」与 `样报:57`「估值状态 = 无法判断」并排；
 * - `样报:64-70` 三情景估值表三行六列全是 `—`。
 * 服务端 J01 已把冲突结论归一为「无数据」（`report_quality.normalize_valuation`），
 * 这里锁前端两件事：归一后的原因要跟着呈现；全空情景表不再占版面。
 */
import { describe, expect, it } from "vitest";
import type { StockReport } from "./researchTypes";
import { buildStockReportMarkdown } from "./export";

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "002468",
    title: "申通快递：测试研报",
    report: "### 技术面\n收盘 14.90 站上 MA20。\n### 综合判断\n多空交织。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-18T18:30:53Z",
    report_chars: 1839,
    report_mode: "deep",
    ...overrides,
  } as StockReport;
}

/** 服务端归一后的典型载荷：verdict 已是「无数据」，原话与原因在 verdict_raw / verdict_note。 */
const NORMALIZED_VALUATION = {
  pe_ttm: "11.70",
  pe_percentile: "近1250交易日 1.7%",
  peer_position: "PE 低于同业中位 12.22",
  verdict: "无数据",
  verdict_raw: "偏便宜",
  verdict_note: "模型给出的估值结论「偏便宜」缺少正常化盈利支撑，按「无法判断」口径归一为「无数据」。",
  basis: "PE/PS 偏低而 PB 分位 66.9%",
  valuation_status: "undetermined",
  normalized_earnings: { value: null, basis: "S4 未单列非经常性损益", confidence: "low" },
};

describe("J01 估值结论与状态的呈现", () => {
  it("导出把归一原因写进结论行（读者知道「无数据」是怎么来的）", () => {
    const md = buildStockReportMarkdown(baseReport({ valuation: NORMALIZED_VALUATION as StockReport["valuation"] }));
    expect(md).toContain("无法判断（未完成正常化验证）");
    expect(md).toContain("模型给出的估值结论「偏便宜」缺少正常化盈利支撑");
    expect(md).not.toContain("| 结论 | 偏便宜 |");
  });

  /**
   * Q05（2026-09-19 路线图）：PE(TTM) 11.70 / PB / 分位 / 同业位置明明都在，导出却只写「无数据」。
   * 现在按三态出词：指标已取得但正常化盈利未验证 = 「合理价值未评估」，
   * 「估值指标缺失」只在真的没取到指标时出现。
   */
  it("指标齐备但未做正常化验证 → 导出写「合理价值未评估」而不是「无数据」", () => {
    const md = buildStockReportMarkdown(baseReport({ valuation: NORMALIZED_VALUATION as StockReport["valuation"] }));
    expect(md).toContain("| 结论 | 合理价值未评估（指标已有，缺正常化盈利验证） |");
    expect(md).toContain("| 估值证据三态 | 指标已取得，合理价值尚未评估");
    expect(md).toContain("PE(TTM) 11.70");
    expect(md).not.toContain("| 结论 | 无数据");
  });

  it("后端下发 valuation_evidence 时原样搬运（前端不重判、不改词）", () => {
    const md = buildStockReportMarkdown(baseReport({
      valuation: {
        ...NORMALIZED_VALUATION,
        valuation_evidence: {
          state: "normalization_unverified",
          label: "指标已取得，合理价值尚未评估",
          verdict_display: "合理价值未评估（后端三态）",
          note: "后端说明",
          metrics: { pe_ttm: "11.70" },
          metric_count: 1,
        },
      } as StockReport["valuation"],
    }));
    expect(md).toContain("| 结论 | 合理价值未评估（后端三态） |");
    expect(md).toContain("| 估值证据三态 | 指标已取得，合理价值尚未评估（后端说明） |");
  });

  it("真的没取到指标时仍如实说「估值指标缺失」", () => {
    const md = buildStockReportMarkdown(baseReport({
      valuation: { pe_ttm: "无", pe_percentile: "—", peer_position: "", verdict: "无数据", basis: "" } as StockReport["valuation"],
    }));
    expect(md).toContain("| 估值证据三态 | 估值指标缺失");
    expect(md).toContain("| 结论 | 无估值指标数据 |");
  });
});

describe("J02 三情景估值空壳表", () => {
  it("三列全 null 时不再渲染表格，改出一行说明", () => {
    const md = buildStockReportMarkdown(baseReport({
      valuation: {
        ...NORMALIZED_VALUATION,
        valuation_cases: [
          { name: "bear", name_label: "悲观", earnings: null, multiple: null, fair_value: null },
          { name: "base", name_label: "基准", earnings: null, multiple: null, fair_value: null },
          { name: "bull", name_label: "乐观", earnings: null, multiple: null, fair_value: null },
        ],
      } as StockReport["valuation"],
    }));
    expect(md).not.toContain("### 三情景估值");
    expect(md).toContain("未做三情景估值");
    expect(md).toContain("S4 未单列非经常性损益");
  });

  it("任一数值列有值时表格照排（不因为收紧判据而丢掉有效分析）", () => {
    const md = buildStockReportMarkdown(baseReport({
      valuation: {
        ...NORMALIZED_VALUATION,
        valuation_cases: [
          { name: "bear", name_label: "悲观", earnings: null, multiple: null, fair_value: null },
          { name: "base", name_label: "基准", earnings: 18.2, multiple: 14, fair_value: 15.6 },
        ],
      } as StockReport["valuation"],
    }));
    expect(md).toContain("### 三情景估值");
    // 只有拿到数值的那行进表：全空行不再凑数
    expect(md).toContain("| 基准 | 18.2 | 14 | 15.6 |");
    expect(md).not.toContain("| 悲观 |");
  });
});
