/**
 * G2（研报产出与交付链路路线图 2026-09-27）：验证点「可回填 / 不可自动回填」的呈现口径。
 *
 * 要防住的退化：
 * - **角标说谎**：报告 5 条验证点里有 2 条是锚定事件（「三季报披露后」），结构上永远落不了库，
 *   角标却只报 5——用户点进去才发现 2 条没有入口；
 * - **导出件说不出话**：导出只写「状态：待复盘」，离开界面后无从知道哪几条点不动；
 * - **判定分叉**：角标、验证点表、导出件各写一套「能不能回填」的判断，迟早对不上账。
 *
 * 判定只有一份实现（`format.watchpointRefill*`），屏显、导出、角标全部消费它。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReportWatchpoint, ReviewQueue } from "./researchTypes";
import {
  watchpointRefillLabel,
  watchpointRefillSummary,
  watchpointRefillable,
} from "./format";
import { WatchpointTable, ReviewQueuePanel } from "./panels";

const DATED: ReportWatchpoint = {
  signal: "回踩不破 MA20",
  verify_by: "2026-09-28",
  due_on: "2026-09-28",
  expected_if_true: "均线多头保持",
};

const ANCHORED: ReportWatchpoint = {
  signal: "三季报毛利率环比回升",
  verify_by: "三季报披露后",
  due_on: "",
  expected_if_true: "支撑增强",
};

describe("G2 · 可回填判定的唯一实现", () => {
  it("只有能解析出确切日期的验证点算「可回填」", () => {
    expect(watchpointRefillable(DATED)).toBe(true);
    expect(watchpointRefillable(ANCHORED)).toBe(false);
    // 空值不当作「可回填」——拿不准的事不承诺入口。
    expect(watchpointRefillable(undefined)).toBe(false);
    expect(watchpointRefillable({ ...ANCHORED, due_on: "  " })).toBe(false);
  });

  it("计数区分可回填与锚定事件", () => {
    const summary = watchpointRefillSummary([DATED, ANCHORED, ANCHORED]);
    expect(summary).toEqual({ total: 3, refillable: 1, anchored: 2 });
    expect(watchpointRefillSummary(undefined)).toEqual({ total: 0, refillable: 0, anchored: 0 });
  });

  it("文案点明「锚定事件不可自动回填」，不写成一个含糊的破折号", () => {
    expect(watchpointRefillLabel(DATED).text).toBe("可回填");
    expect(watchpointRefillLabel(ANCHORED).text).toBe("锚定事件·不可自动回填");
    // 锚定那一档的说明必须点出「不猜日期」与人工出口，否则读者只看到一句禁用。
    expect(watchpointRefillLabel(ANCHORED).hint).toContain("不猜日期");
    expect(watchpointRefillLabel(ANCHORED).hint).toContain("手工标记已复核");
  });
});

describe("G2 · 验证点表的「可回填」列", () => {
  it("withRefill 打开时逐条标出，并在表头前给出计数口径", () => {
    render(<WatchpointTable points={[DATED, ANCHORED, ANCHORED]} withRefill />);
    // 表头一格 + 带日期那条的取值一格。
    expect(screen.getAllByText("可回填")).toHaveLength(2);
    expect(screen.getAllByText("锚定事件·不可自动回填")).toHaveLength(2);
    expect(screen.getByText(/共 3 条验证点：1 条可自动回填，2 条锚定事件/)).toBeTruthy();
    expect(screen.getByText(/手工标记已复核/)).toBeTruthy();
  });

  it("默认不渲染该列——追问附录的 new_watchpoints 当前不落库，不能承诺入口", () => {
    render(<WatchpointTable points={[ANCHORED]} />);
    expect(screen.queryByText("锚定事件·不可自动回填")).toBeNull();
    expect(screen.queryByText(/可自动回填/)).toBeNull();
  });

  it("全部可回填时不渲染那行计数说明（不显示 0 条噪声）", () => {
    render(<WatchpointTable points={[DATED]} withRefill />);
    expect(screen.queryByText(/锚定事件（无确切日期/)).toBeNull();
  });
});

function queue(overrides: Partial<ReviewQueue> = {}): ReviewQueue {
  return {
    ok: true,
    today: "2026-09-28",
    counts: { overdue: 1, scheduled: 0, event_anchored: 2 },
    item_count: 3,
    items: [
      {
        report_id: "rep-1",
        symbol: "002468",
        signal: "回踩不破 MA20",
        verify_by: "2026-09-28",
        due_on: "2026-09-28",
        bucket: "overdue",
        days_until: 0,
        status: "pending",
        judgment_id: "11111111-1111-5111-8111-111111111111",
        verification: null,
        watchpoint_index: 0,
        scenarios: [],
      },
      {
        report_id: "rep-1",
        symbol: "002468",
        signal: "三季报毛利率环比回升",
        verify_by: "三季报披露后",
        due_on: "",
        bucket: "event_anchored",
        days_until: null,
        status: "pending_review",
        judgment_id: null,
        watchpoint_index: 1,
        materialization_note: "验证时点锚定事件、无确切日期，而判断必须带到期时间——不猜日期，故不自动落库。",
        scenarios: [],
      },
      {
        report_id: "rep-1",
        symbol: "002468",
        signal: "下次月度经营简报披露后",
        verify_by: "下次月度经营简报披露后",
        due_on: "",
        bucket: "event_anchored",
        days_until: null,
        status: "pending_review",
        judgment_id: null,
        watchpoint_index: 2,
        manual_review: {
          result: "insufficient_data",
          result_label: "无数据",
          outcome: "简报未披露该口径",
          reviewed_at: "2026-09-27T00:00:00+00:00",
          history_count: 2,
        },
        scenarios: [],
      },
    ],
    closure: { materialized: 1, filled: 0, overdue_unfilled: 1, not_materialized: 2, manually_reviewed: 1 },
    ...overrides,
  };
}

describe("G2 · 复盘队列的手工复核", () => {
  it("锚定事件给「手工标记已复核」入口，带确切日期的不给（那条走回填）", () => {
    render(<ReviewQueuePanelHarness />);
    // 2 条锚定事件各一个入口；带日期那条只有「回填结论」。
    expect(screen.getAllByRole("button", { name: "手工标记已复核" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "重新标记已复核" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "回填结论" })).toBeTruthy();
  });

  it("复核计数与已复核结论如实展示（不混进「已回填」）", () => {
    render(<ReviewQueuePanelHarness />);
    expect(screen.getByText("锚定事件已复核 1/2")).toBeTruthy();
    expect(screen.getByText(/人工复核：无数据 · 简报未披露该口径/)).toBeTruthy();
    expect(screen.getByText(/共复核 2 次/)).toBeTruthy();
  });

  it("提交时回传 (report_id, 序号, 三态, 说明)，成功后收起表单", async () => {
    const onManualReview = vi.fn(async () => null);
    render(<ReviewQueuePanelHarness onManualReview={onManualReview} />);
    await userEvent.click(screen.getByRole("button", { name: "手工标记已复核" }));
    await userEvent.click(screen.getByRole("button", { name: "失效" }));
    await userEvent.type(screen.getByLabelText("人工复核说明"), "事件未发生");
    await userEvent.click(screen.getByRole("button", { name: "提交复核" }));
    expect(onManualReview).toHaveBeenCalledTimes(1);
    expect(onManualReview).toHaveBeenCalledWith("rep-1", 1, "refuted", "事件未发生");
    expect(screen.queryByRole("button", { name: "提交复核" })).toBeNull();
  });

  it("复核失败如实显示错误且表单不收起（不把失败当成功）", async () => {
    const onManualReview = vi.fn(async () => "该验证点带确切日期（2026-09-28）");
    render(<ReviewQueuePanelHarness onManualReview={onManualReview} />);
    await userEvent.click(screen.getByRole("button", { name: "手工标记已复核" }));
    await userEvent.click(screen.getByRole("button", { name: "提交复核" }));
    expect(screen.getByText(/复核失败：该验证点带确切日期/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "提交复核" })).toBeTruthy();
  });

  it("没有注入复核回调时不渲染入口（不给点了没反应的死按钮）", () => {
    render(<ReviewQueuePanelHarness withManualReview={false} />);
    expect(screen.queryByRole("button", { name: "手工标记已复核" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新标记已复核" })).toBeNull();
  });
});

/** 直接渲染面板（queue 构造器在上方，保持用例只关心契约）。 */
function ReviewQueuePanelHarness({
  onManualReview = vi.fn(async () => null),
  withManualReview = true,
}: {
  onManualReview?: (reportId: string, index: number, result: "verified" | "refuted" | "insufficient_data", outcome: string) => Promise<string | null>;
  withManualReview?: boolean;
}) {
  return (
    <ReviewQueuePanel
      queue={queue()}
      onRefill={vi.fn(async () => null)}
      {...(withManualReview ? { onManualReview } : {})}
    />
  );
}
