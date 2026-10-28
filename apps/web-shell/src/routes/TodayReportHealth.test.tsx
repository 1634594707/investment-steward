import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TodayReportHealth } from "./TodayReportHealth";

afterEach(cleanup);
const data = { ok: true, total: 42, items: [{ report_id: "r1", subject: "600519", title: "测试报告", source_health: {
  checked_on: "2026-10-26", sources: [{ source_id: "S1", state: "stale" }, { source_id: "S2", state: "failed" }],
} }, { report_id: "old", subject: "000001" }] };

it("展示覆盖范围及缺口，断连保留旧快照，恢复后解除提示，点击仅打开指定报告", async () => {
  const coreRequest = vi.fn().mockResolvedValue({ status: 200, data });
  const onOpenReport = vi.fn();
  render(<TodayReportHealth active coreRequest={coreRequest} onOpenReport={onOpenReport} />);
  await screen.findByText(/当前展示 2\/42/);
  expect(screen.getByText(/S1：观测日期超过阈值；S2：取数失败/)).toBeTruthy();
  expect(screen.getByText("缺少来源记录，无法核验")).toBeTruthy();
  expect(coreRequest).toHaveBeenCalledWith({ method: "GET", path: "/ai-research/reports?kind=stock&limit=20&include_source_health=true" });
  fireEvent.click(screen.getByRole("button", { name: "查看报告并补研 · 600519" }));
  expect(onOpenReport).toHaveBeenCalledWith("r1");
  coreRequest.mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新研报健康" }));
  await screen.findByRole("alert");
  expect(screen.getByText(/旧快照，当前状态尚未确认/)).toBeTruthy();
  coreRequest.mockResolvedValue({ status: 200, data });
  fireEvent.click(screen.getByRole("button", { name: "刷新研报健康" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.queryByText(/旧快照/)).toBeNull();
});

it("隐藏后重新进入首页会重读，旧请求不能覆盖新结果", async () => {
  let old!: (value: unknown) => void;
  const coreRequest = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { old = resolve; }))
    .mockResolvedValue({ status: 200, data: { ok: true, items: [], total: 0 } });
  const props = { coreRequest, onOpenReport: vi.fn() };
  const { rerender } = render(<TodayReportHealth {...props} active />);
  rerender(<TodayReportHealth {...props} active={false} />);
  rerender(<TodayReportHealth {...props} active />);
  await screen.findByText("尚无个股研报记录。");
  await act(async () => old({ status: 200, data }));
  expect(screen.queryByText("测试报告")).toBeNull();
  expect(coreRequest).toHaveBeenCalledTimes(2);
});
