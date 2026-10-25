/**
 * G1（研报产出与交付链路路线图 2026-09-27）：质量校验痕迹里的定向修复动作按钮。
 *
 * 要防住的两种退化：
 * - **死按钮**：点了没反应（未落库 / 当前视图没有入口 / 服务端判定不可修）——必须禁用并写明原因；
 * - **假按钮**：不可修的项（缺两节及以上）看起来能点——服务端返回 `enabled=false`，前端如实禁用。
 *
 * 按钮只是入口，真正的执行在服务端（`report_repair.py`，与自动修复同一套路径）；
 * 这里只验证「可点 / 不可点 + 原因」的呈现口径与回调契约。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { CategorizedWarning, StockReport } from "./researchTypes";
import { ReportQualityPanel, repairActionKey } from "./panels";

const SUMMARY_WARNING: CategorizedWarning = {
  category: "format",
  category_label: "形式告警",
  message: "执行摘要 118 字，低于目标区间 150–165 字，可能缺少结论/证据/反证/验证动作四要素之一。",
  impact: "不影响结论有效性，影响阅读与跟踪体验",
  action: "可在「质量详情」中查看；形式问题可在后续修订中处理",
  handled: false,
  repair: { strategy: "summary_only", label: "重写执行摘要", enabled: true, reason: "", section: "", version: "v1" },
};

const CONCLUSION_WARNING: CategorizedWarning = {
  category: "format",
  category_label: "形式告警",
  message: "模型未输出一句话结论（conclusion.statement），首屏结论卡只能留空。",
  handled: false,
  repair: { strategy: "conclusion_only", label: "补写一句话结论", enabled: true, reason: "", section: "", version: "v1" },
};

const TWO_SECTIONS_WARNING: CategorizedWarning = {
  category: "blocking",
  category_label: "阻断错误",
  message: "缺少必需小节：技术面、消息面（未生成，或仅有标题没有实质内容）。",
  handled: false,
  repair: {
    strategy: "full_rerun",
    label: "重跑完整初稿",
    enabled: false,
    reason: "缺 2 节（技术面、消息面）：逐节补写一致性差、成本高于重跑，请在标的页重新发起一次完整生成。",
    section: "",
    version: "v1",
  },
};

const PLAIN_WARNING: CategorizedWarning = {
  category: "format",
  category_label: "形式告警",
  message: "局限声明 2 条，少于约定的 3 条下限。",
  handled: false,
  repair: null,
};

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "002468",
    title: "样本标的研报",
    report_id: "r-1",
    report: "### 技术面\nMA20 之上运行。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-27T10:00:00Z",
    report_chars: 1793,
    report_mode: "standard",
    categorized_warnings: [SUMMARY_WARNING, CONCLUSION_WARNING],
    ...overrides,
  } as StockReport;
}

describe("G1 · 质量告警的定向修复按钮", () => {
  it("可修复告警渲染可点按钮，点击回传该动作", async () => {
    const onRepair = vi.fn();
    render(<ReportQualityPanel report={baseReport()} onRepair={onRepair} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["重写执行摘要", "补写一句话结论"]);
    await userEvent.click(screen.getByRole("button", { name: "补写一句话结论" }));
    expect(onRepair).toHaveBeenCalledTimes(1);
    expect(onRepair).toHaveBeenCalledWith(expect.objectContaining({ strategy: "conclusion_only", section: "" }));
  });

  it("服务端判定不可修（缺两节）→ 禁用并写明原因，不给死按钮", () => {
    render(
      <ReportQualityPanel report={baseReport({ categorized_warnings: [TWO_SECTIONS_WARNING] })} onRepair={vi.fn()} />,
    );
    const button = screen.getByRole("button", { name: "重跑完整初稿" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/不可自动修复：缺 2 节/)).toBeTruthy();
  });

  it("报告未落库（无 report_id）→ 禁用并说明「没有可回写的记录」", () => {
    render(<ReportQualityPanel report={baseReport({ report_id: undefined })} onRepair={vi.fn()} />);
    expect(screen.getByRole("button", { name: "重写执行摘要" })).toBeDisabled();
    expect(screen.getAllByText(/未落库（无 report_id）/)).toHaveLength(2);
  });

  it("当前视图不提供入口（如协同装配稿）→ 禁用并说明原因，而不是凭空少一个按钮", () => {
    render(<ReportQualityPanel report={baseReport()} />);
    expect(screen.getByRole("button", { name: "重写执行摘要" })).toBeDisabled();
    expect(screen.getAllByText(/当前视图不提供定向修复入口/)).toHaveLength(2);
  });

  it("与修复无关的告警不渲染任何按钮（无 repair 字段）", () => {
    render(<ReportQualityPanel report={baseReport({ categorized_warnings: [PLAIN_WARNING] })} onRepair={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/局限声明 2 条/)).toBeTruthy();
  });

  it("修复进行中：对应按钮禁用并显示「修复中…」（防重复点击）", () => {
    const key = repairActionKey(CONCLUSION_WARNING.repair!);
    render(<ReportQualityPanel report={baseReport()} onRepair={vi.fn()} repairingKey={key} />);
    const busy = screen.getByRole("button", { name: "修复中…" });
    expect(busy).toBeDisabled();
    // 另一个动作不受影响
    expect(screen.getByRole("button", { name: "重写执行摘要" })).toBeEnabled();
  });
});
