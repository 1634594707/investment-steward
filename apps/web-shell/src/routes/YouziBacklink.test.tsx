/**
 * G03（桌面端升级路线图 2026-09-18）前端契约：游资披露事实的原始页回链。
 *
 * 锁三件事：
 * ① 「跨日事实」页签的每条逐日证据都能点到原始披露页（href 逐字来自后端 `source_url`）；
 * ② 「案例复盘」页签的「来源定位」表里同样有可点回链；
 * ③ 后端拼不出回链（空串）时**显式写「未取得」**，绝不给半截链接或猜一个 URL。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const request = vi.fn();

vi.mock("../state/coreClient", () => ({
  createCoreClient: () => ({ request }),
}));

import { YouziPage } from "./YouziPage";

const URL_0916 = "https://data.eastmoney.com/stock/lhb,2026-09-16,000592.html";
const URL_0917 = "https://data.eastmoney.com/stock/lhb,2026-09-17,000592.html";

const TODAY_EMPTY = { available: true, trading_day: "20260917", watchlist_version: "v1", rows: [] };

function tacticsPayload(evidenceUrls: string[]) {
  return {
    request_day: "20260917",
    effective_day: "20260917",
    window_days: ["20260911", "20260914", "20260915", "20260916", "20260917"],
    coverage: { "20260916": "complete", "20260917": "complete" },
    available: true,
    items: [
      {
        fact_type: "adjacent_buy_sell",
        security_code: "000592",
        security_name: "平潭发展",
        operatedept_code: "10026937",
        operatedept_name: "某某证券股份有限公司某营业部",
        days: ["20260916", "20260917"],
        fact_id: "f".repeat(64),
        evidence: evidenceUrls.map((url, index) => ({
          trading_day: index === 0 ? "20260916" : "20260917",
          direction: index === 0 ? "buy" : "sell",
          amount: index === 0 ? 1e7 : 8e6,
          evidence_id: `e${index}`,
          explanation: "日涨幅偏离值达到7%的前5只证券",
          source_report: index === 0 ? "RPT_BILLBOARD_DAILYDETAILSBUY" : "RPT_BILLBOARD_DAILYDETAILSSELL",
          source_record_id: "a".repeat(64),
          source_url: url,
          amount_reason: null,
        })),
      },
    ],
    conflicts: [],
    excluded: {},
    limitations: [],
    boundary_notice: "榜外席位不可见，未检出未发生；同一营业部不等于同一账户。",
  };
}

function replayEvent() {
  return {
    event_id: "ev1",
    trading_day: "20260916",
    security_code: "600519",
    security_name: "贵州茅台",
    operatedept_code: "10026937",
    operatedept_name: "某某证券股份有限公司某营业部",
    direction: "buy",
    amount: 2.5e7,
    explanation: "日换手率达20%的证券",
    source_report: "RPT_BILLBOARD_DAILYDETAILSBUY",
    source_record_id: "b".repeat(64),
    source_url: URL_0916,
    fact_types: [],
    fact_ids: [],
    horizons: [],
    fact_omitted_reason: "no_fact_for_window",
  };
}

function mockRoutes(opts: { tactics?: unknown; replay?: unknown } = {}) {
  request.mockImplementation((req: { path: string }) => {
    if (req.path.startsWith("/youzi/tactics")) {
      return Promise.resolve({ status: 200, data: opts.tactics ?? tacticsPayload([URL_0916, URL_0917]) });
    }
    if (req.path.startsWith("/youzi/replay")) {
      return Promise.resolve({
        status: 200,
        data: opts.replay ?? {
          security_code: "600519",
          range_start: "20260907",
          range_end: "20260917",
          events: [replayEvent()],
          cursor: "",
          has_more: false,
          total_events: 1,
          coverage: { "20260916": "complete" },
          truncated: false,
          truncated_reason: "",
          fetched_at: 1758000000,
          limitations: ["榜单只覆盖进入榜单的股票与席位。"],
          available: true,
          boundary_notice: "榜外席位不可见，未检出未发生。",
          event_day_count: 1,
          context_days: [],
        },
      });
    }
    return Promise.resolve({ status: 200, data: TODAY_EMPTY });
  });
}

// ★ 必须用块体：`() => request.mockReset()` 会把 mock 本身当返回值，
//   vitest 将 hook 返回的函数视为 teardown 回调，会在测试后**无参调用**它 →
//   报 `Cannot read properties of undefined (reading 'path')`。
beforeEach(() => {
  request.mockReset();
});
afterEach(() => {
  cleanup();
});

describe("YouziPage · G03 跨日事实回链", () => {
  it("★ 每条逐日证据都带可点回链，href 逐字来自后端 source_url", async () => {
    mockRoutes();
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);

    fireEvent.click(screen.getByTitle("五交易日窗口内的已披露路径事实"));
    await waitFor(() => expect(screen.getByText("000592")).toBeTruthy());
    fireEvent.click(screen.getByText("看证据"));

    const links = await waitFor(() => screen.getAllByRole("link", { name: /原始页/ }));
    expect(links.map((link) => link.getAttribute("href"))).toEqual([URL_0916, URL_0917]);
    for (const link of links) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toContain("noreferrer");
    }
  });

  it("后端拼不出回链时显式写「未取得」，不给半截链接", async () => {
    mockRoutes({ tactics: tacticsPayload(["", URL_0917]) });
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);

    fireEvent.click(screen.getByTitle("五交易日窗口内的已披露路径事实"));
    await waitFor(() => expect(screen.getByText("000592")).toBeTruthy());
    fireEvent.click(screen.getByText("看证据"));

    await waitFor(() => expect(screen.getByText("未取得")).toBeTruthy());
    const links = screen.getAllByRole("link", { name: /原始页/ });
    expect(links).toHaveLength(1);
    expect(links[0]!.getAttribute("href")).toBe(URL_0917);
  });
});

describe("YouziPage · G03 案例复盘回链", () => {
  it("★ 案例复盘的「来源定位」里有可点原始披露页", async () => {
    mockRoutes();
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);

    fireEvent.click(screen.getByTitle("按证券代码与日期区间查看披露事件时间线"));
    await waitFor(() => expect(screen.getByPlaceholderText("如 600519")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("如 600519"), { target: { value: "600519" } });
    const dayInputs = screen.getAllByPlaceholderText("YYYYMMDD");
    fireEvent.change(dayInputs[0]!, { target: { value: "20260907" } });
    fireEvent.change(dayInputs[1]!, { target: { value: "20260917" } });
    fireEvent.click(screen.getByText("查询"));

    // 事件行折叠时只有一行；「来源定位」表在展开后才出现（点整行切换）。
    const reason = await waitFor(() => screen.getByText("日换手率达20%的证券"));
    expect(screen.queryByText("来源定位")).toBeNull();
    fireEvent.click(reason);

    await waitFor(() => expect(screen.getByText("来源定位")).toBeTruthy());
    const link = screen.getByRole("link", { name: /东财龙虎榜个股页/ });
    expect(link.getAttribute("href")).toBe(URL_0916);
    expect(link.getAttribute("target")).toBe("_blank");
  });
});
