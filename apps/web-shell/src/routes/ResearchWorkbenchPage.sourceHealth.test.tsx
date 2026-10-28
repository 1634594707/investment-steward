import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ResearchWorkbenchPage } from "./ResearchWorkbenchPage";
import { updateCompareJob } from "./workbench/compareJob";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it("首页请求直接打开指定研报，重新激活不重复打开已消费请求", async () => {
  updateCompareJob({ results: null, busy: false, pending: 0, activeIndex: 0 });
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  const paths: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input)).pathname;
    paths.push(path);
    const body = path === "/ai-research/reports/target" ? { ok: true, item: {
      report_id: "target", kind: "stock", subject: "600519", symbol: "600519", title: "首页目标研报",
      report: "正文", model: "test", citations: [], limitations: [], source_keys: [], source_errors: {},
      generated_at: "2026-10-26", confidence: "low",
    } } : { ok: true, items: [], models: [], total: 0, counts: { total: 0 } };
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response;
  }));
  const props = { holdings: [], aiReady: true, isDemo: false, modelProfiles: [], onCreateHolding: vi.fn(),
    onSendToTactics: vi.fn(), onNavigate: vi.fn(), reportHandoff: null, reportViewRequest: { requestId: "view-1", reportId: "target" } };
  const { rerender } = render(<ResearchWorkbenchPage {...props} active />);
  await screen.findByRole("heading", { name: "首页目标研报" });
  rerender(<ResearchWorkbenchPage {...props} active={false} />);
  rerender(<ResearchWorkbenchPage {...props} active />);
  expect(paths.filter((path) => path === "/ai-research/reports/target")).toHaveLength(1);
});

it.each([200, 503])("补研保留草稿材料，旧响应不影响新报告（HTTP %s）", async (lateStatus) => {
  localStorage.clear();
  updateCompareJob({ results: null, busy: false, pending: 0, activeIndex: 0 });
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  const items = ["a", "b"].map((id) => ({ report_id: id, kind: "stock", subject: "600519",
    title: `历史报告${id}`, model: "test", generated_at: "2026-10-26T00:00:00Z" }));
  const posts: Array<{ path: string; body: Record<string, unknown> }> = [];
  let release!: (value: Response) => void;
  const response = (body: unknown, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) }) as Response;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (init?.method === "POST") {
      posts.push({ path: url.pathname, body: JSON.parse(String(init.body)) });
      return new Promise<Response>((resolve) => { release = resolve; });
    }
    const item = items.find((row) => url.pathname === `/ai-research/reports/${row.report_id}`);
    if (item) return response({ ok: true, item: { ...item, ok: true, symbol: "600519", report: "原报告正文",
      citations: [], limitations: [], confidence: "low", source_keys: ["S1"], source_errors: {},
      source_health: { checked_on: "2026-10-26", note: "冻结证据", sources: [{ source_id: "S1", state: "stale",
        data_date: "2026-01-01", age_days: 298, max_age_days: 7, provider: null, retrieved_at: null,
        cache_age_days: null, error: null }] } } });
    return response({ ok: true, items: url.pathname === "/ai-research/reports" ? items : [], total: 2, models: [],
      counts: { total: 2, stock: 2, direction: 0, collab: 0, draft_stock: 0 } });
  }));
  render(<ResearchWorkbenchPage holdings={[]} aiReady isDemo={false} modelProfiles={[]} active
    onCreateHolding={vi.fn()} onSendToTactics={vi.fn()} onNavigate={vi.fn()} reportHandoff={null} />);
  async function open(id: string) {
    fireEvent.click(screen.getByRole("tab", { name: /历史报告/ }));
    fireEvent.click(await screen.findByText(`历史报告${id}`));
    await screen.findByRole("heading", { name: `历史报告${id}` });
    fireEvent.click(screen.getByRole("tab", { name: "概览" }));
    await screen.findByRole("button", { name: "准备补充研究 · S1" });
  }
  const question = () => screen.getByPlaceholderText(/追问本报告（原报告不可变/) as HTMLTextAreaElement;
  await open("a");
  fireEvent.click(screen.getByRole("tab", { name: /^追问/ }));
  fireEvent.change(question(), { target: { value: "已有问题" } });
  fireEvent.click(screen.getByRole("button", { name: /添加补充材料/ }));
  fireEvent.change(screen.getByPlaceholderText(/补充材料原文/), { target: { value: "用户原始材料" } });
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  const prepare = screen.getByRole("button", { name: "准备补充研究 · S1" }) as HTMLButtonElement;
  await waitFor(() => expect(prepare.disabled).toBe(false));
  fireEvent.click(prepare);
  expect(question().value).toContain("已有问题");
  expect(question().value).toContain("请补充核验来源 S1");
  expect((screen.getByPlaceholderText(/补充材料原文/) as HTMLTextAreaElement).value).toBe("用户原始材料");
  expect(screen.getByRole("radio", { name: "补充研究" }).getAttribute("aria-checked")).toBe("true");
  expect(posts).toEqual([]);
  const prepared = question().value;
  fireEvent.click(screen.getByRole("button", { name: "提交追问" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0]?.path).toContain("/a/");
  expect(posts[0]?.body).toMatchObject({ question: prepared, mode: "supplement_research", supplements: [{ text: "用户原始材料" }] });
  const releaseA = release;
  await open("b");
  const nextPrepare = screen.getByRole("button", { name: "准备补充研究 · S1" }) as HTMLButtonElement;
  await waitFor(() => expect(nextPrepare.disabled).toBe(false));
  fireEvent.click(nextPrepare);
  expect(question().value).not.toContain("已有问题");
  expect(screen.queryByPlaceholderText(/补充材料原文/)).toBeNull();
  const questionB = question().value;
  fireEvent.click(screen.getByRole("button", { name: "提交追问" }));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[1]?.path).toContain("/b/");
  const releaseB = release;
  await act(async () => releaseA(response(lateStatus === 200 ? { ok: true, turn: {}, persisted: true } : { detail: "旧报告失败" }, lateStatus)));
  expect(screen.queryByText(/旧报告失败/)).toBeNull();
  expect(question().value).toBe(questionB);
  expect((screen.getByRole("button", { name: "分析中…" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => releaseB(response({ detail: "新报告失败" }, 503)));
  expect((screen.getByRole("button", { name: "提交追问" }) as HTMLButtonElement).disabled).toBe(false);
  await open("a");
  fireEvent.click(screen.getByRole("tab", { name: /^追问/ }));
  expect(question().value).toBe(prepared);
  expect((screen.getByPlaceholderText(/补充材料原文/) as HTMLTextAreaElement).value).toBe("用户原始材料");
});
