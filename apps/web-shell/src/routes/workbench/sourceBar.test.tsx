/**
 * D02（前端设计与架构优化任务路线图 2026-09-19）：单股研究来源条与返回操作的回归。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReportHandoff } from "../../state/handoff";
import { ReportSourceBar } from "./sourceBar";

function handoff(overrides: Partial<ReportHandoff> = {}): ReportHandoff {
  return {
    requestId: "req-1",
    symbol: "600519",
    question: "该标的当前战法信号状态如何？",
    sourceLabel: "战法雷达",
    sourceView: "tactics",
    createdAt: "2026-09-19T00:00:00Z",
    ...overrides,
  };
}

describe("ReportSourceBar（D02 · 前端设计与架构优化任务路线图 2026-09-19）", () => {
  it("常驻显示当前标的、来源与带入的研究问题，返回按钮写明去向", () => {
    render(<ReportSourceBar handoff={handoff()} onNavigate={vi.fn()} />);
    const bar = screen.getByTestId("report-source-bar");
    expect(bar.textContent).toContain("600519");
    expect(bar.textContent).toContain("战法雷达");
    expect(bar.textContent).toContain("已带入研究问题");
    expect(screen.getByRole("button", { name: "返回 战法雷达" })).toBeInTheDocument();
  });

  it("点返回 → 导航回发起交接的视图", () => {
    const onNavigate = vi.fn();
    render(<ReportSourceBar handoff={handoff({ sourceView: "youzi", sourceLabel: "游资雷达" })} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "返回 游资雷达" }));
    expect(onNavigate).toHaveBeenCalledWith("youzi");
  });

  it("无跨页返回落点的交接（工作台内方向→单股）不渲染来源条", () => {
    const { container } = render(<ReportSourceBar handoff={handoff({ sourceView: undefined })} onNavigate={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
