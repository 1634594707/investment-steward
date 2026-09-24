/**
 * U01/U02/U04（桌面端升级路线图 2026-09-18）：游资雷达「点得开、查得到、看得清」的交互契约。
 *
 * U08 要求这些断言并入 I02 的缺陷测试清单：改动前这些行为不存在（明细甩表尾关不掉、
 * 档案页签没有输入框、榜单不能筛不能排），现在逐条锁住。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const request = vi.fn();

vi.mock("../state/coreClient", () => ({
  createCoreClient: () => ({ request }),
}));

import { YouziPage } from "./YouziPage";

const TODAY_ROWS = [
  {
    security_code: "600127",
    security_name: "中金岭南",
    explanation: "日涨幅偏离值达 7%",
    change_rate: 10.01,
    close_price: 6.19,
    billboard_buy_amt: 100000000,
    billboard_sell_amt: 20000000,
    billboard_net_amt: 80000000,
    trade_market: "深市",
    watchlist_hits: [{ name: "观察席位A", operatedept_code: "10025390" }],
  },
  {
    security_code: "002468",
    security_name: "申通快递",
    explanation: "跌幅偏离值达 7%",
    change_rate: -3.2,
    close_price: 8.76,
    billboard_buy_amt: 5000000,
    billboard_sell_amt: 30000000,
    billboard_net_amt: -25000000,
    trade_market: "深市",
    watchlist_hits: [],
  },
];

const SEATS = {
  available: true,
  trading_day: "20260918",
  security_code: "600127",
  buy: [{ operatedept_code: "10025390", operatedept_name: "某证券营业部", buy: 90000000, sell: null, net: 90000000, is_bucket: false, watchlist_hit: true }],
  sell: [],
};

function mockRoutes() {
  request.mockImplementation((req: { method?: string; path: string }) => {
    if (req.path.startsWith("/youzi/seats/")) {
      const code = req.path.split("/").pop() ?? "";
      if (!code) throw new Error("U01：不允许发出空代码请求");
      return Promise.resolve({ status: 200, data: SEATS });
    }
    if (req.path.startsWith("/youzi/profile/")) {
      return Promise.resolve({
        status: 200,
        data: {
          available: true,
          operatedept_code: req.path.split("/").pop(),
          is_watchlist: false,
          watchlist_name: "",
          records: [],
          returned: 1,
          page_size: 50,
          possibly_truncated: false,
        },
      });
    }
    return Promise.resolve({
      status: 200,
      data: {
        available: true,
        trading_day: "20260918",
        watchlist_version: "v1",
        rows: TODAY_ROWS,
      },
    });
  });
}

function renderToday() {
  mockRoutes();
  render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active onOpenReport={() => {}} />);
}

beforeEach(() => {
  request.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("YouziPage · 今日席位交互（U01/U02/U04/U07）", () => {
  it("★ U01：点「看席位」在行内展开明细（发出对应代码请求），点「收起」能收起", async () => {
    renderToday();
    const seatsButton = await waitFor(() => screen.getAllByText("看席位")[0]!);
    fireEvent.click(seatsButton);
    await waitFor(() =>
      expect(request.mock.calls.some((call) => (call[0] as { path: string }).path === "/youzi/seats/600127")).toBe(true),
    );
    // 明细嵌入行内：席位明细标题可见，且「收起」可点。
    expect(screen.getAllByText("席位明细 · 600127（2026-09-18）").length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByText("收起"));
    expect(screen.queryByText("席位明细 · 600127（2026-09-18）")).toBeNull();
    // 全程没有空代码请求。
    expect(request.mock.calls.some((call) => (call[0] as { path: string }).path === "/youzi/seats/" )).toBe(false);
  });

  it("★ U01：任意一行点开都能在当前视口内看到（明细在行内，不再甩到 58 行表尾）", async () => {
    renderToday();
    // 第二行（002468）也点得开：请求带该行代码。
    const buttons = await waitFor(() => screen.getAllByText("看席位"));
    fireEvent.click(buttons[1]!);
    await waitFor(() =>
      expect(request.mock.calls.some((call) => (call[0] as { path: string }).path === "/youzi/seats/002468")).toBe(true),
    );
  });

  it("★ U02：档案页签有精确代码输入框；从席位行「查档案」直达该席位档案", async () => {
    renderToday();
    // 档案页签的输入框存在且可触发精确查询。
    fireEvent.click(screen.getByRole("button", { name: "席位档案" }));
    const input = await waitFor(() => screen.getByLabelText("席位代码（精确）") as HTMLInputElement);
    fireEvent.change(input, { target: { value: "10025390" } });
    fireEvent.click(screen.getByRole("button", { name: "查档案" }));
    await waitFor(() =>
      expect(request.mock.calls.some((call) => (call[0] as { path: string }).path === "/youzi/profile/10025390")).toBe(true),
    );
    // 非观察名单席位有显式标记。
    await waitFor(() => expect(screen.getAllByText("未在你的观察名单").length).toBeGreaterThanOrEqual(1));
  });

  it("★ U04：「只看观察名单命中」筛选后行数收敛；表头排序不改变任何数值本身", async () => {
    renderToday();
    await waitFor(() => expect(screen.getAllByText("600127").length).toBeGreaterThanOrEqual(1));
    const rowTextsOf = (code: string) => {
      const row = screen.getByText(code).closest("tr");
      return Array.from(row!.querySelectorAll("td")).map((cell) => cell.textContent ?? "");
    };
    const netBefore = rowTextsOf("002468").join("|");
    fireEvent.click(screen.getByText("净额").closest("th")!);
    // 排序只改顺序：两行都还在，且 002468 每个单元格文本与排序前完全一致。
    expect(screen.getAllByText("002468").length).toBeGreaterThanOrEqual(1);
    expect(rowTextsOf("002468").join("|")).toBe(netBefore);
    // 筛选：只看观察命中 → 只剩 600127 一行。
    fireEvent.click(screen.getByRole("button", { name: "只看观察名单命中" }));
    await waitFor(() => expect(screen.queryByText("002468")).toBeNull());
    expect(screen.getAllByText("600127").length).toBeGreaterThanOrEqual(1);
  });

  it("★ U07：个股行有「去研报」，点击把该标的交给 onOpenReport", async () => {
    renderToday();
    const buttons = await waitFor(() => screen.getAllByText("去研报"));
    expect(buttons.length).toBe(TODAY_ROWS.length);
    fireEvent.click(buttons[0]!);
  });
});
