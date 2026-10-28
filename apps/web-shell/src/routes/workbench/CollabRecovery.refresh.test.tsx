import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { CollabRecovery } from "./collab";
import type { SavedCollabRun } from "./researchTypes";

afterEach(cleanup);
const saved: SavedCollabRun = { run_id: "saved", symbol: "600001", question: "旧进度", model: "m",
  created_at: "2026-10-27", next_index: 1, done: false, recovery_state: "running_or_interrupted" };

it("refreshes a restored task after the server resolves interruption status", async () => {
  const load = vi.fn().mockResolvedValueOnce([saved]).mockResolvedValueOnce([
    { ...saved, recovery_state: "failed", stage_label: "首席初稿" },
  ]);
  render(<CollabRecovery load={load} restore={vi.fn().mockResolvedValue(undefined)} disabled={false} />);
  fireEvent.click(screen.getByRole("button", { name: "查看可恢复任务" }));
  fireEvent.click(await screen.findByRole("button", { name: "恢复查看" }));
  expect(await screen.findByText(/阶段失败：首席初稿/)).toBeTruthy();
  expect(screen.queryByText(/执行中或已中断/)).toBeNull();
});

it.each([false, true])("refreshes after execution; a failed refresh labels its old snapshot (failure=%s)", async failure => {
  const load = vi.fn().mockResolvedValueOnce([saved]);
  if (failure) load.mockRejectedValueOnce(new Error("合成列表刷新失败"));
  else load.mockResolvedValueOnce([]);
  const restore = vi.fn().mockResolvedValue(undefined);
  const view = render(<CollabRecovery load={load} restore={restore} disabled={false} />);
  fireEvent.click(screen.getByRole("button", { name: "查看可恢复任务" }));
  await screen.findByRole("button", { name: "恢复查看" });
  view.rerender(<CollabRecovery load={load} restore={restore} disabled />);
  expect(screen.getByText(/上次读取的任务快照/)).toBeTruthy();
  view.rerender(<CollabRecovery load={load} restore={restore} disabled={false} />);
  if (failure) {
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/上次读取的任务快照/)).toBeTruthy();
  } else {
    expect(await screen.findByText("没有待恢复任务。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "恢复查看" })).toBeNull();
  }
});
