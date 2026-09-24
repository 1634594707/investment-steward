import { describe, expect, it } from "vitest";
import { coreSupportState } from "./format";
import type { ClaimFindings } from "./researchTypes";

/** J03（桌面端升级路线图 2026-09-18）：核心结论三态与分母的纯函数测试（导出与屏显共用）。 */
function findingsWith(claims: { importance?: string; support?: string }[]): ClaimFindings {
  return {
    claims: claims.map((claim, index) => ({
      claim_id: `C${index + 1}`,
      text: `判断 ${index + 1}`,
      sources: ["S4"],
      dropped_sources: [],
      requires: [],
      unknown_requires: [],
      missing_coverage: [],
      evidence_quality: "B",
      status: "supported",
      note: "",
      ...claim,
    })),
    total: claims.length,
    supported: claims.length,
    downgraded: [],
    no_source: [],
    core_conclusion_supported: null,
    warnings: [],
  };
}

describe("coreSupportState（J03）", () => {
  it("1/6 与 4/6 都是 partial，但分母计数可区分", () => {
    const make = (supported: number) =>
      findingsWith([
        ...Array.from({ length: supported }, () => ({ support: "full" })),
        ...Array.from({ length: 6 - supported }, () => ({ support: "none" })),
      ]);
    const one = coreSupportState(make(1));
    const four = coreSupportState(make(4));
    expect(one.state).toBe("partial");
    expect(four.state).toBe("partial");
    expect(one.counts).toEqual({ supported: 1, total: 6 });
    expect(four.counts).toEqual({ supported: 4, total: 6 });
    expect(one.counts).not.toEqual(four.counts);
  });

  it("未标 importance 时全部判断视为核心（保守兜底）", () => {
    const result = coreSupportState(findingsWith([{ support: "full" }, { support: "none" }]));
    expect(result.state).toBe("partial");
    expect(result.counts).toEqual({ supported: 1, total: 2 });
  });

  it("只统计 importance=high 的核心判断；非核心不撑住也不拖垮", () => {
    const result = coreSupportState(
      findingsWith([
        { importance: "high", support: "full" },
        { importance: "high", support: "full" },
        { importance: "low", support: "none" },
      ]),
    );
    expect(result.state).toBe("supported");
    expect(result.counts).toEqual({ supported: 2, total: 2 });
  });

  it("核心判断仅 partial（无一条 full）时不得为 supported/partial，判 unsupported", () => {
    const result = coreSupportState(findingsWith([{ importance: "high", support: "partial" }]));
    expect(result.state).toBe("unsupported");
  });

  it("无 claims 时为 null（不假装通过）", () => {
    expect(coreSupportState(null).state).toBeNull();
    expect(coreSupportState(findingsWith([])).state).toBeNull();
  });
});
