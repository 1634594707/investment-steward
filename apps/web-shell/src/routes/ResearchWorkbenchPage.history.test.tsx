/**
 * A04 / D02 交叉回归：研究工作台「报告历史」区（筛选走服务端参数、回看走详情端点、定位到阅读区顶部）。
 *
 * 口径：jsdom + 受控 fetch（走 coreClient 的 HTTP 分支）。这里证明的是**行为**：筛选条件是否真的进入请求参数、
 * 回看是否按 id 取全文、回看后是否把壳层滚动容器复位；不证明真实数据下的列表内容与视觉。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ResearchWorkbenchPage } from "./ResearchWorkbenchPage";

const LIST = {
  ok: true,
  items: [
    { report_id: "d1", kind: "direction", subject: "固态电池", title: "固态电池方向研判", model: "gpt-x", generated_at: "2026-09-18T02:00:00Z", is_draft: false },
    { report_id: "s1", kind: "stock", subject: "600519", title: "600519 研究报告", model: "gpt-x", generated_at: "2026-09-17T02:00:00Z", is_draft: false },
  ],
  total: 2,
  counts: { total: 2, direction: 1, stock: 1, collab: 0, draft_stock: 0 },
  models: ["gpt-x"],
};

function channel(): { queries: string[] } {
  const queries: string[] = [];
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const path = new URL(url).pathname + new URL(url).search;
    queries.push(path);
    const body = path.startsWith("/ai-research/reports?") || path === "/ai-research/reports"
      ? LIST
      : path === "/ai-research/reports/s1"
        ? { ok: true, item: {
            ...LIST.items[1],
            ok: true,
            symbol: "600519",
            report: "### 技术面 MA20 之上运行，量能温和。 ### 综合判断 总体偏多。",
            citations: ["S1"],
            limitations: [],
            confidence: "medium",
            source_keys: ["S1"],
            source_errors: {},
            report_chars: 2153,
            report_mode: "standard",
            quality_status: "complete",
            quality_status_label: "完整",
          } }
        : null;
    return {
      ok: true,
      status: 200,
      text: async () => (body === null ? "" : JSON.stringify(body)),
    } as Response;
  });
  globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
  return { queries };
}

function renderPage() {
  const channelHandles = channel();
  render(
    <main id="mainScroll">
      <ResearchWorkbenchPage
        holdings={[{ instrument: "600519", label: "贵州茅台" }]}
        aiReady
        isDemo={false}
        modelProfiles={[]}
        active
        onCreateHolding={vi.fn()}
        onSendToTactics={vi.fn()}
        onNavigate={vi.fn()}
        reportHandoff={null}
      />
    </main>,
  );
  return channelHandles;
}

beforeEach(() => {
  localStorage.clear();
});

describe("研究工作台 历史产出区（A04/D02）", () => {
  it("历史分区列出落库产出并按 kind 标注，计数口径分开", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /历史报告/ }));
    const list = await screen.findByText("600519 研究报告");
    expect(list).toBeInTheDocument();
    expect(screen.getByText("方向研判 · 固态电池")).toBeInTheDocument();
    expect(document.querySelector(".wb-history")?.textContent).toContain("历史研究产出（2/2 条 · 正式 2 · 不完整草稿 0");
  });

  it("分类与关键字筛选以请求参数下发（服务端筛选，不在前端二次过滤）", async () => {
    const { queries } = renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /历史报告/ }));
    fireEvent.click(screen.getByRole("button", { name: "方向研判（1）" }));
    await waitFor(() => expect(queries.some((q) => q.includes("kind=direction"))).toBe(true));

    fireEvent.change(screen.getByLabelText("历史产出关键字过滤"), { target: { value: "固态" } });
    await waitFor(() => expect(queries.some((q) => q.includes("kind=direction") && q.includes("q=%E5%9B%BA%E6%80%81"))).toBe(true), { timeout: 3000 });
  });

  it("回看按 id 取全文，并把壳层滚动容器复位到顶部", async () => {
    const { queries } = renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: /历史报告/ }));
    const scroller = document.getElementById("mainScroll");
    if (!scroller) throw new Error("缺少滚动容器");
    scroller.scrollTop = 240;

    fireEvent.click(screen.getByText("600519 研究报告"));
    await waitFor(() => expect(queries).toContain("/ai-research/reports/s1"));
    expect(scroller.scrollTop).toBe(0);
  });
});
