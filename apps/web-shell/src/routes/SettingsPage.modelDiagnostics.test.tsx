import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ModelUsageSection } from "./SettingsPage";

afterEach(cleanup);

function pendingUsage() {
  let resolve!: (value: { status: number; data: unknown }) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<{ status: number; data: unknown }>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function usage(model: string) {
  return { status: 200, data: { summaries: [], recent_calls: [{
    call_id: model, model, returned_model: null, purpose: null, created_at: "2026-10-27T01:00:00Z",
    attempt_count: 1, outcome: "ok", error_kind: null, http_status: 200, latency_ms: 10, usage_complete: 1,
  }] } };
}

it.each(["success", "error"])("ignores a late old-window %s while the current window is loading", async outcome => {
  const old = pendingUsage(), current = pendingUsage();
  const coreRequest = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
  render(<ModelUsageSection coreRequest={coreRequest} />);
  fireEvent.click(screen.getByRole("button", { name: "最近 30 天" }));
  expect(coreRequest.mock.calls.map(([req]) => req.path)).toEqual(["/model-usage?window=7d", "/model-usage?window=30d"]);
  await act(async () => { if (outcome === "success") old.resolve(usage("old-7d")); else old.reject(new Error("old failure")); });
  expect(screen.queryByText("old-7d")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("button", { name: "刷新" }).hasAttribute("disabled")).toBe(true);
  await act(async () => current.resolve(usage("current-30d")));
  expect(screen.getByText("current-30d")).toBeTruthy();
});

it.each(["success", "error"])("ignores a late old-window %s after the current window completed", async outcome => {
  const old = pendingUsage(), current = pendingUsage();
  render(<ModelUsageSection coreRequest={vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)} />);
  fireEvent.click(screen.getByRole("button", { name: "最近 30 天" }));
  await act(async () => current.resolve(usage("current-30d")));
  await act(async () => { if (outcome === "success") old.resolve(usage("old-7d")); else old.reject(new Error("old failure")); });
  expect(screen.getByText("current-30d")).toBeTruthy();
  expect(screen.queryByText("old-7d")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("retains same-window data on refresh failure but never labels old data as a new window", async () => {
  const refresh = pendingUsage(), switched = pendingUsage();
  const coreRequest = vi.fn().mockResolvedValueOnce(usage("saved-7d"))
    .mockReturnValueOnce(refresh.promise).mockReturnValueOnce(switched.promise);
  render(<ModelUsageSection coreRequest={coreRequest} />);
  await screen.findByText("saved-7d");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await act(async () => refresh.reject(new Error("refresh failed")));
  expect(screen.getByText("saved-7d")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "最近 30 天" }));
  expect(screen.queryByText("saved-7d")).toBeNull();
  await act(async () => switched.reject(new Error("30d failed")));
  expect(screen.queryByText("saved-7d")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("30d failed");
});

it("does not resurrect either older success after a 7d→30d→7d switch ends in failure", async () => {
  const first = pendingUsage(), middle = pendingUsage(), latest = pendingUsage();
  render(<ModelUsageSection coreRequest={vi.fn().mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(middle.promise).mockReturnValueOnce(latest.promise)} />);
  fireEvent.click(screen.getByRole("button", { name: "最近 30 天" }));
  fireEvent.click(screen.getByRole("button", { name: "最近 7 天" }));
  await act(async () => latest.reject(new Error("latest failed")));
  await act(async () => {
    first.resolve(usage("old-7d"));
    const result = usage("old-30d");
    middle.resolve({ ...result, data: { ...result.data, summaries: [{ model: "old-summary", profile_id: null,
      purpose: "stock_report", calls: 1, retried_calls: 0, ok_calls: 1, errors: 0, timeouts: 0,
      prompt_tokens: 20, completion_tokens: 10, total_tokens: 30, usage_missing: 0,
      avg_latency_ms: 10, max_latency_ms: 10 }] } });
  });
  expect(screen.getByRole("alert").textContent).toContain("latest failed");
  expect(screen.queryByText("old-7d")).toBeNull();
  expect(screen.queryByText("old-30d")).toBeNull();
  expect(screen.queryByText("old-summary")).toBeNull();
  expect(screen.getByRole("button", { name: "刷新" }).hasAttribute("disabled")).toBe(false);
});

it("shows requested and reported models plus actionable diagnostics without guessing legacy metadata", async () => {
  const coreRequest = vi.fn().mockResolvedValue({ status: 200, data: {
    summaries: [], recent_calls: [
      { call_id: "new", model: "requested-model", returned_model: "reported-model", purpose: "研报",
        created_at: "2026-10-26T01:00:00Z", attempt_count: 2, outcome: "error", error_kind: "protocol",
        http_status: 200, latency_ms: 1000, usage_complete: 0,
        attempts: [{ index: 1, outcome: "error", error_kind: "reasoning_budget", http_status: null,
          latency_ms: 100, total_tokens: 30, max_tokens: 512, thinking_disabled: false }] },
      { call_id: "old", model: "legacy", returned_model: null, purpose: null,
        created_at: "2026-10-26T00:00:00Z", attempt_count: null, outcome: "ok", error_kind: null,
        http_status: null, latency_ms: 10, usage_complete: null },
    ],
  } });
  render(<ModelUsageSection coreRequest={coreRequest} />);
  fireEvent.click(screen.getByRole("button", { name: /刷新/ }));
  expect(await screen.findByText("requested-model")).toBeTruthy();
  expect(screen.getByText("reported-model")).toBeTruthy();
  expect(screen.getByText(/协议异常.*HTTP 200/)).toBeTruthy();
  expect(screen.getByText(/用量不完整/)).toBeTruthy();
  expect(screen.getAllByText("未记录").length).toBeGreaterThan(0);
  expect(screen.getByText(/思考耗尽输出预算/)).toBeTruthy();
  expect(screen.getByText(/已知总 token 30/)).toBeTruthy();
});
