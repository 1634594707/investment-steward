import { describe, expect, it } from "vitest";
import type { MacroSnapshot } from "@investment-steward/domain-contracts";
import { macroFrequencyLabel, macroScoreCoverage } from "./macroPresentation";

function snapshot(missing: string[]): MacroSnapshot {
  return { region: "jp", label: "日本", background: [], generated_at: "2026-10-25T00:00:00Z",
    indicators: [{ indicator: "cpi", label: "居民消费价格同比", dim: "inflation", status: "ok", latest: 2,
      obs_date: "2025", as_of: "2026-10-25", unit: "%", source: "test", frequency: "annual", note: "" }],
    positioning: { region: "jp", weight_version: "v1", composite: 50, band: "放缓", limitations: [], generated_at: "2026-10-25",
      dims: ["growth", "employment", "inflation", "monetary"].map((dim) => ({ dim,
        score: missing.includes(dim) ? null : dim === "employment" ? 0 : 50, rules_fired: [],
        indicators_used: dim === "inflation" ? ["cpi"] : [] })) },
  };
}

describe("宏观评分覆盖与数据期", () => {
  it("缺增长与缺增长、货币分别披露，零分维度仍参与计算", () => {
    expect(macroScoreCoverage(snapshot(["growth"]))).toMatchObject({ active: ["就业", "通胀", "货币"], missing: ["增长"] });
    expect(macroScoreCoverage(snapshot(["growth", "monetary"]))).toMatchObject({ active: ["就业", "通胀"], missing: ["增长", "货币"] });
  });
  it("评分数据期来自实际使用的指标，刷新快照不把 2025 观测改为 2026", () => {
    const original = snapshot(["growth"]);
    const view = macroScoreCoverage(original);
    expect(view.periods).toEqual(["通胀 · 居民消费价格同比：2025（年度）"]);
    expect(macroScoreCoverage({ ...original, generated_at: "2026-10-26" }).periods).toEqual(view.periods);
  });
  it("未取得指标记录时明确缺观测期，不推测频率", () => {
    expect(macroScoreCoverage({ ...snapshot([]), indicators: [] }).periods).toEqual(["通胀 · cpi：观测期未标注（未标注）"]);
    expect(macroFrequencyLabel(null)).toBe("未标注");
    expect(macroFrequencyLabel("weekly")).toBe("weekly");
  });
});
