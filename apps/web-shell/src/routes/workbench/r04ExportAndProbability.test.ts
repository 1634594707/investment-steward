/**
 * R04（2026-09-18 排版与研报呈现一致性路线图）：批量 ZIP 与单份导出共用文件名构造；
 * 屏显与导出共用同一「三档概率同值 = 未给概率」判定。
 */
import { describe, expect, it } from "vitest";
import type { ReportScenario } from "./researchTypes";
import { reportFileBase } from "./export";
import { scenarioProbabilityDisplay } from "./format";

function scenario(level: string | undefined, probability: number | null = null): ReportScenario {
  return { name: "x", probability_level: level, probability } as unknown as ReportScenario;
}

describe("reportFileBase（单导与批量共用）", () => {
  it("带标的名与日期", () => {
    expect(reportFileBase("000060", "中金岭南", "deepseek-v4", "2026-09-18")).toBe(
      "研报-000060-中金岭南-deepseek-v4-2026-09-18",
    );
  });
  it("label 缺失只留 6 位代码，不出现空段", () => {
    expect(reportFileBase("000060", "", "deepseek-v4", "2026-09-18")).toBe(
      "研报-000060-deepseek-v4-2026-09-18",
    );
  });
});

describe("scenarioProbabilityDisplay（屏显/导出同一同值判定）", () => {
  it("三档同值 → uniform，逐档返回「未给概率」", () => {
    const mid = scenario("中");
    const { uniform, displayOf } = scenarioProbabilityDisplay([mid, mid, mid]);
    expect(uniform).toBe(true);
    expect(displayOf(mid)).toBe("—（未给概率）");
  });
  it("档位不同 → 非 uniform，展示各自等级", () => {
    const high = scenario("高");
    const { uniform, displayOf } = scenarioProbabilityDisplay([high, scenario("中"), scenario("低")]);
    expect(uniform).toBe(false);
    expect(displayOf(high)).toBe("高");
  });
  it("单一情景不触发同值判定", () => {
    const { uniform } = scenarioProbabilityDisplay([scenario("中")]);
    expect(uniform).toBe(false);
  });
});
