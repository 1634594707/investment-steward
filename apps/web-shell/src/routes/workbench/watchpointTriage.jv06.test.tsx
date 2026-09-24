/**
 * JV06：验证点优先级分诊的呈现契约。
 *
 * 这一层的验收点是**「排序与折叠」在界面上真的成立，且折叠不等于删除**：
 * 被判为「暂缓」档的验证点仍然渲染在 DOM 里（只是折叠），未分诊的验证点必须保持报告原序、
 * 不出现优先级列、也不出现折叠区——没评过的东西没有理由被挪动，更没有理由被藏起来。
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { watchpointTriageView, watchpointPriorityView } from "./format";
import { WatchpointTable } from "./panels";
import type { ReportWatchpoint, WatchpointTriage } from "./researchTypes";

function triage(rank: number, percent: number | null, deferred: boolean): WatchpointTriage {
  return {
    score: percent == null ? null : percent / 50,
    score_percent: percent,
    confidence: percent == null ? null : 0.88,
    level_label: deferred ? "暂缓" : percent === 100 ? "优先" : "可稍后",
    deferred,
    triage_state: deferred ? "deferred" : percent == null ? "unannotated" : "priority",
    rank,
    note: deferred ? "判为「暂缓」档且置信度 0.88，折叠进「暂缓」（仍可展开，不删除）" : "留在主线",
    model: "typesafe/jev-1.13-20260917",
    schema_version: "1.0",
  };
}

function point(signal: string, t?: WatchpointTriage | null): ReportWatchpoint {
  return {
    signal,
    verify_by: "2026-11-30",
    expected_if_true: "结论强化",
    triage: t === undefined ? null : t,
  } as ReportWatchpoint;
}

describe("验证点分诊呈现（JV06）", () => {
  it("未分诊：保持报告原序，不分组、不折叠", () => {
    const view = watchpointTriageView([point("甲"), point("乙"), point("丙")]);
    expect(view.triaged).toBe(false);
    expect(view.ordered.map((item) => item.signal)).toEqual(["甲", "乙", "丙"]);
    expect(view.main).toHaveLength(3);
    expect(view.deferred).toHaveLength(0);
  });

  it("已分诊：按服务端 rank 排序，暂缓档单独成组", () => {
    const view = watchpointTriageView([
      point("可稍后", triage(2, 50, false)),
      point("暂缓", triage(1, 0, true)),
      point("优先", triage(0, 100, false)),
      point("未评", triage(3, null, false)),
    ]);
    expect(view.ordered.map((item) => item.signal)).toEqual(["优先", "暂缓", "可稍后", "未评"]);
    expect(view.main.map((item) => item.signal)).toEqual(["优先", "可稍后", "未评"]);
    expect(view.deferred.map((item) => item.signal)).toEqual(["暂缓"]);
  });

  it("未分诊的条目不渲染优先级（不把「没评」说成「不急」）", () => {
    expect(watchpointPriorityView(point("甲"))).toBeNull();
    const unannotated = watchpointPriorityView(point("甲", triage(0, null, false)));
    expect(unannotated?.label).toBe("未分诊");
    expect(unannotated?.tone).toBe("gray");
    expect(unannotated?.percent).toBe("—");
  });

  it("已分诊的条目三者都可见（展示分 + 置信度 + 档位）", () => {
    const view = watchpointPriorityView(point("甲", triage(0, 100, false)));
    expect(view?.label).toBe("优先");
    expect(view?.percent).toBe("100");
    expect(view?.confidence).toBe("0.88");
  });

  it("未分诊时表格不出现优先级列、不出现折叠区", () => {
    render(<WatchpointTable points={[point("甲"), point("乙")]} />);
    expect(screen.queryByText("优先级")).toBeNull();
    expect(screen.queryByText(/暂缓 ·/)).toBeNull();
    // 原序
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells[0]).toBe("甲");
  });

  it("已分诊时按序渲染，暂缓档折叠但仍在 DOM 里（不删除）", () => {
    render(
      <WatchpointTable
        points={[
          point("可稍后", triage(2, 50, false)),
          point("暂缓条", triage(1, 0, true)),
          point("优先条", triage(0, 100, false)),
        ]}
      />,
    );
    // 主线表与暂缓表各有一个表头（同一份 `head`，不是两份口径）
    expect(screen.getAllByText("优先级").length).toBeGreaterThan(0);
    const fold = screen.getByText(/暂缓 · 1/).closest("details");
    expect(fold).toBeTruthy();
    expect(fold?.open).toBe(false);

    // 折叠 ≠ 删除：条目本体与判定说明都还在
    expect(screen.getByText("暂缓条")).toBeTruthy();
    // 判定说明挂 title（hover 可见），不是正文——但必须可查，不能没有理由地折起来
    expect(screen.getByTitle(/仍可展开，不删除/)).toBeTruthy();
    // 主线区按 rank 排：优先在前
    const rows = screen.getAllByRole("row").map((row) => row.textContent ?? "");
    expect(rows.findIndex((text) => text.includes("优先条"))).toBeLessThan(
      rows.findIndex((text) => text.includes("可稍后")),
    );
  });

  it("没有暂缓档时不渲染空的折叠区，也不留白", () => {
    render(<WatchpointTable points={[point("甲", triage(0, 100, false))]} />);
    expect(screen.queryByText(/暂缓 ·/)).toBeNull();
    expect(screen.getByText("优先级")).toBeTruthy();
    expect(screen.getByText(/「暂缓」档已折叠/)).toBeTruthy();
  });
});
