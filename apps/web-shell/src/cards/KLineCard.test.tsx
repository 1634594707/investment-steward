import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { CandleSeries } from "@investment-steward/domain-contracts";
import { KLineCard } from "./KLineCard";

// lightweight-charts 依赖 canvas，jsdom 下打桩：只验证组件装配与卸载链路。
vi.mock("lightweight-charts", () => {
  const seriesStub = () => ({ setData: vi.fn() });
  const chartStub = () => ({
    addSeries: vi.fn(() => seriesStub()),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    subscribeCrosshairMove: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn(() => chartStub()),
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    CandlestickSeries: {},
    HistogramSeries: {},
    LineSeries: {},
  };
});

function seriesFixture(): CandleSeries {
  const bar = (day: string, close: number) => ({
    timestamp: `2026-09-${day}T00:00:00`,
    open: close - 0.1,
    high: close + 0.2,
    low: close - 0.3,
    close,
    volume: 1000,
  });
  return {
    instrument: "510300",
    timeframe: "1d",
    bars: [bar("01", 4.0), bar("02", 4.1), bar("03", 4.2)],
    source_plugin_id: "official.cn-market-data",
    source_plugin_release: "0.1.0",
    source_name: "中国市场行情",
    as_of: "2026-09-03",
    is_demo: false,
    limitations: ["日 K 数据存在延迟，以券商终端为准。"],
  };
}

describe("KLineCard", () => {
  it("渲染标的、来源行与三档周期切换", () => {
    const onSelectPeriod = vi.fn();
    render(<KLineCard series={seriesFixture()} onSelectPeriod={onSelectPeriod} onManage={() => undefined} />);
    expect(screen.getByRole("heading", { name: "510300" })).toBeInTheDocument();
    expect(screen.getByText(/中国市场行情/)).toBeInTheDocument();
    expect(screen.getByText(/正式数据/)).toBeInTheDocument();
    for (const label of ["日K", "周K", "月K"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("点击周期 chip 触发 onSelectPeriod", async () => {
    const user = userEvent.setup();
    const onSelectPeriod = vi.fn();
    render(<KLineCard series={seriesFixture()} period="day" onSelectPeriod={onSelectPeriod} onManage={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "周K" }));
    expect(onSelectPeriod).toHaveBeenCalledWith("week");
  });

  it("行情插件未启用时展示启用引导而非空数据", () => {
    render(<KLineCard series={null} marketPluginEnabled={false} onManage={() => undefined} />);
    expect(screen.getByText("行情插件未启用")).toBeInTheDocument();
  });

  it("加载中展示等待状态而非伪空态", () => {
    render(<KLineCard series={null} loading onManage={() => undefined} />);
    expect(screen.getByText("行情加载中…")).toBeInTheDocument();
  });
});
