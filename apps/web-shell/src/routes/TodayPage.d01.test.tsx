/**
 * D01（前端设计与架构优化任务路线图 2026-09-19）：今天页「优先处理」条目的动作分流。
 * 验收口径：点击前能分辨这次是「查看依据」还是「处理事项」；无证据的条目如实说明缺什么，
 * 不再静默跳去投资/研究页。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { TodayBrief } from "@investment-steward/domain-contracts";
import { TodayPage } from "./TodayPage";

const brief = {
  brief_id: "b1",
  user_id: "u1",
  generated_at: "2026-09-19T00:00:00Z",
  has_personalization: true,
  headline: "今日要点",
  items: [
    { display_order: 0, title: "宁德时代触发复核条件", summary: "价格跌破成本 8%", related_instrument: "300750", signal: "review_plan", evidence_refs: ["e1"], data_time: "2026-09-18T05:54:19.003586Z" },
    { display_order: 1, title: "观察组合集中度", summary: "简报未附证据的条目", signal: "observe", evidence_refs: [] },
  ],
  empty_reason: null,
  policy_version: "v1",
} as unknown as TodayBrief;

function renderPage() {
  const onNavigate = vi.fn();
  const onOpenEvidence = vi.fn();
  render(
    <TodayPage
      counts={{ evidence: 0, open: 0, learning: 0 }}
      evidence={[]}
      brief={brief}
      notifications={[]}
      learningUnit={null}
      learningGoal={null}
      isDemo={false}
      onNavigate={onNavigate}
      onOpenEvidence={onOpenEvidence}
      onMarkNotificationRead={vi.fn()}
      onEvaluateNotifications={vi.fn()}
      onSubmitLearning={vi.fn()}
      onGenerateBrief={vi.fn()}
      onboarding={{ hasPolicy: true, holdings: 1, research: 1 }}
    />,
  );
  return { onNavigate, onOpenEvidence };
}

describe("TodayPage 优先处理条目（D01 · 前端设计与架构优化任务路线图 2026-09-19）", () => {
  it("查看依据只打开证据，不跳页", () => {
    const { onNavigate, onOpenEvidence } = renderPage();
    fireEvent.click(screen.getByRole("button", { name: "查看依据" }));
    expect(onOpenEvidence).toHaveBeenCalledWith("e1");
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("处理事项按 signal 语义写明去处（review_plan → 去复核）", () => {
    const { onNavigate, onOpenEvidence } = renderPage();
    fireEvent.click(screen.getByRole("button", { name: "去复核" }));
    expect(onNavigate).toHaveBeenCalledWith("review");
    expect(onOpenEvidence).not.toHaveBeenCalled();
  });

  it("第二层如实交代依据条数与数据时间；无依据的条目不再静默跳页", () => {
    const { onOpenEvidence } = renderPage();
    expect(screen.getByText("依据 1 条")).toBeInTheDocument();
    expect(screen.getByText("数据截至 2026-09-18")).toBeInTheDocument();
    expect(screen.getByText("本条未附证据引用")).toBeInTheDocument();
    expect(screen.getByText("暂无依据可查")).toBeInTheDocument();
    // 只有「看持仓与逻辑」这一个处理按钮，没有「查看依据」按钮。
    expect(screen.getAllByRole("button", { name: "查看依据" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "看持仓与逻辑" }));
    expect(onOpenEvidence).not.toHaveBeenCalled();
  });
});
