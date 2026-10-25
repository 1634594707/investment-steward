/**
 * D01（前端设计与架构优化任务路线图 2026-09-19）：今天页「优先处理」条目的动作分流。
 * 验收口径：点击前能分辨这次是「查看依据」还是「处理事项」；无证据的条目如实说明缺什么，
 * 不再静默跳去投资/研究页。
 */
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState, type ComponentProps } from "react";
import type { TodayBrief } from "@investment-steward/domain-contracts";
import { SCHEMA_VERSION } from "@investment-steward/domain-contracts";
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

function renderPage(overrides: Partial<ComponentProps<typeof TodayPage>> = {}) {
  const onNavigate = vi.fn();
  const onOpenEvidence = vi.fn();
  function Harness() {
    const [unit, setUnit] = useState(overrides.learningUnit ?? null);
    return <><button onClick={() => setUnit({ ...unit!, unit_id: unit?.unit_id === "u1" ? "u2" : "u1" })}>切换单元</button>
    <TodayPage
      counts={{ evidence: 0, open: 0, learning: 0 }}
      evidence={[]}
      brief={brief}
      notifications={[]}
      learningGoal={null}
      isDemo={false}
      onNavigate={onNavigate}
      onOpenEvidence={onOpenEvidence}
      onMarkNotificationRead={vi.fn()}
      onEvaluateNotifications={vi.fn()}
      onSubmitLearning={vi.fn()}
      onGenerateBrief={vi.fn()}
      onboarding={{ hasPolicy: true, holdings: 1, research: 1 }}
      {...overrides}
      learningUnit={unit}
    /></>;
  }
  render(<Harness />);
  return { onNavigate, onOpenEvidence };
}

describe("TodayPage 优先处理条目（D01 · 前端设计与架构优化任务路线图 2026-09-19）", () => {
  it("处理事项将整条简报交给目标页，包含原始标的与证据", () => {
    const onOpenBriefItem = vi.fn();
    const { onNavigate } = renderPage({ onOpenBriefItem });
    fireEvent.click(screen.getByRole("button", { name: "去复核" }));
    expect(onOpenBriefItem).toHaveBeenLastCalledWith({ target: "review", item: brief.items[0] });
    fireEvent.click(screen.getByRole("button", { name: "看持仓与逻辑" }));
    expect(onOpenBriefItem).toHaveBeenLastCalledWith({ target: "investment", item: brief.items[1] });
    expect(onNavigate).not.toHaveBeenCalled();
  });
  it("只有自选时持仓为零，但录入标的引导已完成", () => {
    renderPage({ onboarding: { hasPolicy: false, holdings: 0, watchlist: 3, research: 0 } });
    expect(screen.getByText("0 个持仓标的")).toBeTruthy();
    expect(screen.getByText("3 个自选标的")).toBeTruthy();
    expect(screen.getByRole("button", { name: "② 录入第一个标的（已完成）" })).toBeTruthy();
  });

  it("保持页面挂载切换单元时，回答与反思独立保存", async () => {
    const unit: NonNullable<ComponentProps<typeof TodayPage>["learningUnit"]> = {
      unit_id: "u1", user_id: "user1", unit_type: "exercise", title: "练习", objective: "目标", content: "内容",
      guidance: "指导", related_conditions: [], source_plugin_id: "test", source_plugin_release: "test",
      data_time: "2026-10-25", schema_version: SCHEMA_VERSION,
    };
    renderPage({ learningUnit: unit });
    // CardSlot 完成异步加载后才挂载表单；保留真实插槽流程。
    fireEvent.change(await screen.findByLabelText("你的回答"), { target: { value: "第一单元草稿" } });
    fireEvent.change(screen.getByLabelText("反思（可导出）"), { target: { value: "第一单元反思" } });
    fireEvent.click(screen.getByRole("button", { name: "切换单元" }));
    expect(screen.getByLabelText("你的回答")).toHaveValue("");
    expect(screen.getByLabelText("反思（可导出）")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("你的回答"), { target: { value: "第二单元草稿" } });
    fireEvent.change(screen.getByLabelText("反思（可导出）"), { target: { value: "第二单元反思" } });
    fireEvent.click(screen.getByRole("button", { name: "切换单元" }));
    expect(screen.getByLabelText("你的回答")).toHaveValue("第一单元草稿");
    expect(screen.getByLabelText("反思（可导出）")).toHaveValue("第一单元反思");
    fireEvent.click(screen.getByRole("button", { name: "切换单元" }));
    expect(screen.getByLabelText("你的回答")).toHaveValue("第二单元草稿");
    expect(screen.getByLabelText("反思（可导出）")).toHaveValue("第二单元反思");
  });
  it("保存回执晚于单元切换时，仅更新发起单元，不清空新单元草稿", async () => {
    let complete!: (ok: boolean) => void;
    const pending = new Promise<boolean>((resolve) => { complete = resolve; });
    const onSubmitLearning = vi.fn(() => pending);
    const unit: NonNullable<ComponentProps<typeof TodayPage>["learningUnit"]> = {
      unit_id: "u1", user_id: "user1", unit_type: "exercise", title: "练习", objective: "目标", content: "内容",
      guidance: "指导", related_conditions: [], source_plugin_id: "test", source_plugin_release: "test",
      data_time: "2026-10-25", schema_version: SCHEMA_VERSION,
    };
    renderPage({ learningUnit: unit, onSubmitLearning });
    fireEvent.change(await screen.findByLabelText("你的回答"), { target: { value: "第一单元回答" } });
    fireEvent.click(screen.getByRole("button", { name: "保存练习与反思" }));
    expect(onSubmitLearning).toHaveBeenCalledWith(expect.objectContaining({ unit_id: "u1", user_answer: "第一单元回答" }));
    fireEvent.click(screen.getByRole("button", { name: "切换单元" }));
    fireEvent.change(screen.getByLabelText("你的回答"), { target: { value: "第二单元未保存草稿" } });
    await act(async () => { complete(true); await pending; });
    expect(screen.getByLabelText("你的回答")).toHaveValue("第二单元未保存草稿");
    expect(screen.queryByText("已保存本次练习与反思，可在复盘页查看记录。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "切换单元" }));
    expect(screen.getByText("已保存本次练习与反思，可在复盘页查看记录。")).toBeTruthy();
  });
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
