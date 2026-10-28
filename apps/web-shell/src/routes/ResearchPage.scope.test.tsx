import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ResearchPage } from "./ResearchPage";
import { useSteward } from "../hooks/useSteward";
import type { CoreClient } from "../state/coreClient";

describe("研究证据范围", () => {
  it("实际研究钩子把范围送入创建请求", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: { run_id: "scope-run" } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, true));
    const scope = { subject_refs: ["600001"], source_names: ["filing"], financial_period_ends: ["2026-06-30"] };
    await act(async () => { await result.current.createResearchRun("cash flow", scope); });
    expect(request).toHaveBeenCalledWith({ method: "POST", path: "/research/runs",
      body: { user_question: "cash flow", evidence_scope: scope } });
  });
  it("发送精确范围、拒绝倒置日期，清空后恢复默认请求", async () => {
    const create = vi.fn().mockResolvedValue(null);
    render(<ResearchPage evidence={[]} response={null} runs={[]} onNavigate={() => {}} onOpenEvidence={() => {}}
      onCardAction={() => {}} onCreateRun={create} onSelectRun={async () => null} onTransitionRun={async () => false}
      onDeleteRun={async () => false} onPatrolFreshness={async () => null} />);
    fireEvent.click(screen.getByRole("button", { name: /新建研究/ }));
    fireEvent.change(screen.getByLabelText("你的问题"), { target: { value: "现金流是否可靠" } });
    fireEvent.click(screen.getByText("证据范围（可选）"));
    fireEvent.change(screen.getByLabelText("标的标识"), { target: { value: "600001，600002" } });
    fireEvent.change(screen.getByLabelText("来源名称"), { target: { value: "财报" } });
    fireEvent.change(screen.getByLabelText("财务口径"), { target: { value: "cumulative" } });
    fireEvent.change(screen.getByLabelText("报告期末（可多项）"), { target: { value: "2026-02-30" } });
    const submit = screen.getByRole("button", { name: /创建|开始研究/ });
    fireEvent.click(submit);
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByText(/报告期末请填写有效/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("报告期末（可多项）"), { target: { value: "2026-06-30，2026-03-31,2026-06-30" } });
    fireEvent.change(screen.getByLabelText("发布日起点（UTC）"), { target: { value: "2026-07-01" } });
    fireEvent.change(screen.getByLabelText("发布日终点（UTC）"), { target: { value: "2026-06-30" } });
    fireEvent.click(submit);
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByText("发布日期起点不得晚于终点。")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("发布日起点（UTC）"), { target: { value: "2026-06-01" } });
    fireEvent.click(submit);
    await waitFor(() => expect(create).toHaveBeenCalledWith("现金流是否可靠", {
      subject_refs: ["600001", "600002"], source_names: ["财报"],
      financial_period_ends: ["2026-06-30", "2026-03-31"],
      financial_period_basis: "cumulative",
      published_from: "2026-06-01T00:00:00Z", published_to: "2026-06-30T23:59:59.999Z",
    }));
    await waitFor(() => expect(submit).not.toBeDisabled());
    expect(screen.getByLabelText("报告期末（可多项）")).toHaveValue("2026-06-30，2026-03-31,2026-06-30");
    expect(screen.getByLabelText("财务口径")).toHaveValue("cumulative");
    fireEvent.click(screen.getByRole("button", { name: "清空范围" }));
    fireEvent.click(submit);
    await waitFor(() => expect(create).toHaveBeenLastCalledWith("现金流是否可靠"));
  });
  it("保存范围显示报告期末，与发布日分列", () => {
    render(<ResearchPage evidence={[]} response={null} runs={[{ run_id: "period-run", user_id: "u1", status: "created",
      user_question: "cash flow", evidence_refs: [], supporting_refs: [], contradicting_refs: [],
      created_at: "2026-10-27T00:00:00Z", updated_at: "2026-10-27T00:00:00Z",
      evidence_scope: { financial_period_ends: ["2026-06-30"], published_from: "2026-08-01T00:00:00Z" } }]}
      onNavigate={() => {}} onOpenEvidence={() => {}} onCardAction={() => {}} onCreateRun={async () => null}
      onSelectRun={async () => null} onTransitionRun={async () => false} onDeleteRun={async () => false}
      onPatrolFreshness={async () => null} />);
    expect(screen.getByText(/报告期末 2026-06-30/)).toBeTruthy();
    expect(screen.getByText(/2026-08-01T00:00:00Z/)).toBeTruthy();
  });
});
