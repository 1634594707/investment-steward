import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ResearchWorkbenchPage } from "./ResearchWorkbenchPage";
import { CollabRecovery } from "./workbench/collab";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it("恢复摘要区分失败与状态待确认，恢复查看不执行阶段", async () => {
  const restore = vi.fn().mockResolvedValue(undefined);
  render(<CollabRecovery active disabled={false} restore={restore} load={async () => [
    { run_id: "failed", symbol: "600001", question: "问题", model: "m", created_at: "2026-10-26", next_index: 1,
      done: false, recovery_state: "failed", stage_label: "初稿" },
    { run_id: "running", symbol: "600002", question: "问题", model: "m", created_at: "2026-10-26", next_index: 1,
      done: false, recovery_state: "running_or_interrupted" },
  ]} />);
  await screen.findByText(/阶段失败：初稿/);
  expect(screen.getByText(/执行中或已中断，恢复查看后确认状态/)).toBeTruthy();
  expect(restore).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole("button", { name: "恢复查看" })[0]!);
  await waitFor(() => expect(restore).toHaveBeenCalledWith("failed"));
});

it.each(["pending", "archive", "cancelled", "direct"])("restores saved view without POST and resumes only on request (%s)", async (state) => {
  const archive = state === "archive";
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  const run = { run_id: "saved-id", symbol: "600001", question: "原问题", model: "m", stage_models: {},
    created_at: "2026-10-26T00:00:00Z", done: archive, cancelled_at: state === "cancelled" ? "2026-10-26T01:00:00Z" : null,
    next_index: archive ? 4 : 1, source_keys: [], source_errors: {},
    stages: ["check", "draft", "risk", "revise"].map((stage, index) => ({ stage, label: stage,
      status: archive || index === 0 ? "done" : "pending", result: index === 0 ? { observations: ["已保存的核对结果"] } : null,
      error: null, latency_ms: null })) };
  const posts: Array<{ path: string; body: string }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (init?.method === "POST") posts.push({ path: url.pathname, body: String(init.body) });
    const body = url.pathname === "/evidence/collab-state"
      ? url.searchParams.has("run_id") ? { ok: true, run } : { ok: true, items: [run] }
      : url.pathname === "/evidence/collab-next"
        ? archive ? { ok: true, done: true, executed_stage: "revise", executed_status: "done", persisted: false, run }
          : { ok: true, done: false, executed_stage: "draft", executed_status: "failed",
              run: { ...run, stages: run.stages.map((stage, index) => index === 1 ? { ...stage, status: "failed", error: "测试失败" } : stage) } }
        : { ok: true, items: [], total: 0, models: [], counts: { total: 0, direction: 0, stock: 0, collab: 0, draft_stock: 0 } };
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response;
  }));
  render(<ResearchWorkbenchPage holdings={[]} aiReady isDemo={false} modelProfiles={[]} active
    onCreateHolding={vi.fn()} onSendToTactics={vi.fn()} onNavigate={vi.fn()} reportHandoff={null}
    collabViewRequest={state === "direct" ? { requestId: "home-view", runId: "saved-id" } : null} />);
  if (state !== "direct") {
    fireEvent.click(screen.getByRole("button", { name: "查看可恢复任务" }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复查看" }));
  }
  await screen.findByText("已保存的核对结果");
  expect(posts).toEqual([]);
  if (state === "cancelled") {
    expect(screen.queryByRole("button", { name: "继续执行（含失败阶段重试）" })).toBeNull();
    expect(screen.queryByRole("button", { name: "停止协同任务" })).toBeNull();
    expect(screen.getAllByText(/已本地取消/).length).toBeGreaterThan(0);
    return;
  }
  fireEvent.click(screen.getByRole("button", { name: archive ? "补存归档" : "继续执行（含失败阶段重试）" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]).toEqual({ path: "/evidence/collab-next", body: JSON.stringify({ run_id: "saved-id" }) });
  await screen.findAllByText(archive ? /未能保存/ : /测试失败/);
  expect(posts).toHaveLength(1);
});

it.each([[false, false], [false, true], [true, false]])("stops the queue and isolates later work (cancel failure=%s, late error=%s)", async (failure, lateError) => {
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  const run = { run_id: "saved-id", symbol: "600001", question: "原问题", model: "m", stage_models: {},
    created_at: "2026-10-26T00:00:00Z", done: false, next_index: 1, source_keys: [], source_errors: {},
    stages: ["check", "draft", "risk", "revise"].map((stage, index) => ({ stage, label: stage,
      status: index === 0 ? "done" : "pending", result: index === 0 ? { observations: ["已保存的核对结果"] } : null,
      error: null, latency_ms: null })) };
  const posts: string[] = [];
  let release!: (value: Response) => void;
  let rejectLate!: (reason: Error) => void;
  let releaseNew!: (value: Response) => void;
  let restored = run;
  const response = (body: unknown, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) }) as Response;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (init?.method === "POST") posts.push(url.pathname);
    if (url.pathname === "/evidence/collab-next") return new Promise<Response>((resolve, reject) => {
      if (JSON.parse(String(init?.body)).run_id === "new-id") releaseNew = resolve;
      else { release = resolve; rejectLate = reject; }
    });
    if (url.pathname === "/evidence/collab-cancel") {
      expect(JSON.parse(String(init?.body))).toEqual({ run_id: "saved-id" });
      if (!failure) restored = { ...run, run_id: "new-id", symbol: "600002" };
      return failure ? response({ detail: "停止请求失败" }, 503)
        : response({ ok: true, run: { ...run, cancelled_at: "2026-10-26T01:00:00Z" } });
    }
    return response(url.pathname === "/evidence/collab-state"
      ? url.searchParams.has("run_id") ? { ok: true, run: restored } : { ok: true, items: [restored] }
      : { ok: true, items: [], total: 0, models: [], counts: { total: 0, direction: 0, stock: 0, collab: 0, draft_stock: 0 } });
  }));
  render(<ResearchWorkbenchPage holdings={[]} aiReady isDemo={false} modelProfiles={[]} active
    onCreateHolding={vi.fn()} onSendToTactics={vi.fn()} onNavigate={vi.fn()} reportHandoff={null} />);
  fireEvent.click(screen.getByRole("button", { name: "查看可恢复任务" }));
  fireEvent.click(await screen.findByRole("button", { name: "恢复查看" }));
  fireEvent.click(await screen.findByRole("button", { name: "继续执行（含失败阶段重试）" }));
  await waitFor(() => expect(posts).toEqual(["/evidence/collab-next"]));
  fireEvent.click(screen.getByRole("button", { name: "停止协同任务" }));
  await screen.findAllByText(failure ? /停止请求失败/ : /已本地取消/);
  if (!failure) {
    const view = screen.getByRole("button", { name: "查看可恢复任务" }) as HTMLButtonElement;
    await waitFor(() => expect(view.disabled).toBe(false));
    fireEvent.click(view);
    await screen.findByText(/600002 · 原问题/);
    fireEvent.click(await screen.findByRole("button", { name: "恢复查看" }));
    await screen.findByText(/协同流水线.*600002/);
    fireEvent.click(screen.getByRole("button", { name: "继续执行（含失败阶段重试）" }));
    await waitFor(() => expect(posts).toHaveLength(3));
  }
  await act(async () => {
    if (lateError) rejectLate(new Error("旧请求迟到错误"));
    else release(response({ ok: true, done: false, executed_stage: "draft", executed_status: "done",
      run: { ...run, next_index: 2 } }));
  });
  expect(screen.queryByText(/旧请求迟到错误/)).toBeNull();
  expect(posts).toEqual(failure ? ["/evidence/collab-next", "/evidence/collab-cancel"]
    : ["/evidence/collab-next", "/evidence/collab-cancel", "/evidence/collab-next"]);
  if (!failure) {
    expect(screen.queryByRole("button", { name: "继续执行（含失败阶段重试）" })).toBeNull();
    expect(screen.getByText(/协同流水线.*600002/)).toBeTruthy();
    await act(async () => releaseNew(response({ ok: true, done: false, executed_stage: "draft", executed_status: "failed", run: restored })));
  }
  expect(screen.queryByText("当前角色执行中…", { exact: false })).toBeNull();
  expect(screen.getByRole("button", { name: "继续执行（含失败阶段重试）" })).toBeTruthy();
});
