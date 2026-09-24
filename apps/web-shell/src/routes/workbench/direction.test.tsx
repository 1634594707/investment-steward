/**
 * v33（2026-09-14 路线图）E05/E07 前端自动化：候选股表的选择范围与单股目标。
 * E05：原池含无效/重复 → 发送集合只含可扫描代码；已选 ≠ 筛选结果，两个按钮分开计数。
 * E07：「研究该股」只针对当前行标的，并预填业务关联作为研究问题。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { DirectionReport } from "./researchTypes";
import {
  BoardJudgmentSummary,
  DirectionAnswerFirst,
  DirectionFoldedReport,
  DirectionPoolTable,
  DirectionResultTabs,
} from "./direction";

function poolReport(): DirectionReport {
  return {
    ok: true,
    topic: "测试主题",
    direction_summary: "正文",
    catalysts: [],
    risks: [],
    model: "m",
    generated_at: "2026-09-14T00:00:00Z",
    report_id: "dir-1",
    stock_pool: [
      { symbol_raw: "600001", symbol: "600001", name: "甲股", sector: "上游", business_link: "算力", profit_path: "订单", identity_status: "verified", identity_status_label: "身份已核验" },
      { symbol_raw: "000002", symbol: "000002", name: "乙股", sector: "下游", business_link: "封测", profit_path: "产能", identity_status: "verified", identity_status_label: "身份已核验" },
      { symbol_raw: "300003", symbol: "300003", name: "丙股", sector: "下游", business_link: "设备", profit_path: "验收", identity_status: "verified", identity_status_label: "身份已核验" },
      { symbol_raw: "999999", symbol: "999999", name: "无效股", sector: "上游", business_link: "", identity_status: "invalid", identity_status_label: "无效代码" },
      { symbol_raw: "600001", symbol: "600001", name: "重复甲股", sector: "上游", business_link: "", identity_status: "verified", duplicate_of_pool: true },
    ],
  } as DirectionReport;
}

const noopSend = vi.fn();

describe("DirectionPoolTable（A17/D02，E05/E07）", () => {
  it("有效候选计数正确：重复与无效不计入可发送", () => {
    const { container } = render(<DirectionPoolTable report={poolReport()} onSendToTactics={noopSend} onStudySingle={vi.fn()} />);
    expect(container.querySelector(".wb-pool-toolbar")!.textContent).toContain("有效 3 家");
    expect(screen.getByText((_, el) => (el?.textContent ?? "").includes("无效代码 1（不随发送）") && el?.children.length === 0)).toBeTruthy();
    // 无效与重复行没有复选框（只有「—」占位）
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBe(3);
  });

  it("E05：勾选 2 只 → 送已选只到 2 只；送筛选结果 → 3 只（去重保序）", () => {
    const onSend = vi.fn();
    const { container } = render(<DirectionPoolTable report={poolReport()} onSendToTactics={onSend} onStudySingle={vi.fn()} />);
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[0]!);
    fireEvent.click(checkboxes[1]!);
    expect(container.querySelector(".wb-pool-counts")!.textContent).toContain("已选 2");
    fireEvent.click(screen.getByRole("button", { name: "送已选到雷达（2）" }));
    const [selectedPool] = onSend.mock.calls[0]!;
    expect(selectedPool).toEqual(["600001", "000002"]);
    // 送筛选结果：当前过滤集合中的可扫描项（600001 去重一次）
    fireEvent.click(screen.getByRole("button", { name: "送筛选结果到雷达（3）" }));
    const [filteredPool, context] = onSend.mock.calls[1]!;
    expect(filteredPool).toEqual(["600001", "000002", "300003"]);
    expect(context.sourceLabel).toContain("筛选结果");
    expect(context.sourceReportId).toBe("dir-1");
  });

  it("E05：勾选后切换板块筛选，已选集合收敛到当前可见范围", () => {
    const onSend = vi.fn();
    const { container } = render(<DirectionPoolTable report={poolReport()} onSendToTactics={onSend} onStudySingle={vi.fn()} />);
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    fireEvent.click(checkboxes[0]!); // 600001（上游）
    fireEvent.click(checkboxes[1]!); // 000002（下游）
    // 切到「上游」板块 → 000002 被隐藏，已选收敛为 1
    fireEvent.click(screen.getByRole("button", { name: "上游" }));
    expect(container.querySelector(".wb-pool-counts")!.textContent).toContain("已选 1");
    fireEvent.click(screen.getByRole("button", { name: "送已选到雷达（1）" }));
    expect(onSend.mock.calls[0]![0]).toEqual(["600001"]);
  });

  it("E07：研究该股只针对当前行标的，并预填业务关联", () => {
    const onStudy = vi.fn();
    render(<DirectionPoolTable report={poolReport()} onSendToTactics={noopSend} onStudySingle={onStudy} />);
    const studyButtons = screen.getAllByRole("button", { name: "研究该股" });
    fireEvent.click(studyButtons[1]!); // 第二行 = 乙股
    expect(onStudy).toHaveBeenCalledTimes(1);
    const [symbol, question] = onStudy.mock.calls[0]!;
    expect(symbol).toBe("000002");
    expect(question).toContain("封测");
  });
});

/**
 * v37 排版优化：结论优先三问速览 / 报告分节折叠 / 分类行颜色标注。
 * 用户反馈「报告容易抓不住重点」——分类总表原埋在正文第 5 节，核心判断在第 10 节之后。
 */
function judgmentReport(): DirectionReport {
  return {
    ok: true,
    topic: "被低估的板块",
    direction_summary: "正文",
    catalysts: [],
    risks: [],
    model: "m",
    generated_at: "2026-09-14T00:00:00Z",
    report_id: "dir-2",
    stock_pool: [],
    board_judgments: {
      银行Ⅱ: "高位",
      轨道交通设备Ⅱ: "低估候选",
      房地产开发: "深跌未反转",
      保险Ⅱ: "盈利周期顶",
      某待定板块: "待定",
    },
  } as unknown as DirectionReport;
}

describe("v37 排版（DirectionAnswerFirst / DirectionFoldedReport）", () => {
  it("三问速览按分类把板块分到三问下，并单列「待定」", () => {
    const { container } = render(<DirectionAnswerFirst report={judgmentReport()} />);
    const rows = container.querySelectorAll(".wb-answer-first-row");
    // 三问 + 待定 = 4 行
    expect(rows.length).toBe(4);
    const text = container.textContent ?? "";
    expect(text).toContain("满足低估判定");
    expect(text).toContain("只是跌得久");
    expect(text).toContain("已处高位");
    // 各板块归入正确篮子（高位的银行Ⅱ / 低估候选的轨交 / 深跌的地产 / 周期顶的保险）
    const lowRow = container.querySelector(".wb-answer-first-row.low")!;
    expect(lowRow.textContent).toContain("轨道交通设备Ⅱ");
    const deepRow = container.querySelector(".wb-answer-first-row.deep")!;
    expect(deepRow.textContent).toContain("房地产开发");
    const highRow = container.querySelector(".wb-answer-first-row.high")!;
    expect(highRow.textContent).toContain("银行Ⅱ");
    expect(highRow.textContent).toContain("保险Ⅱ"); // 盈利周期顶并入「已处高位」问
  });

  it("低估候选为空时给出「本期无」而非空白，并附合法性说明", () => {
    const report = {
      ...judgmentReport(),
      board_judgments: { 银行Ⅱ: "高位", 房地产开发: "深跌未反转" },
    } as unknown as DirectionReport;
    const { container } = render(<DirectionAnswerFirst report={report} />);
    expect(container.querySelector(".wb-answer-first-row.low")!.textContent).toContain("本期无");
    expect(container.textContent).toContain("本期无低估候选");
  });

  it("旧报告无 board_judgments → 整块不渲染（不猜测分类）", () => {
    const report = { ok: true, topic: "t", direction_summary: "", catalysts: [], risks: [] } as unknown as DirectionReport;
    const { container } = render(<DirectionAnswerFirst report={report} />);
    expect(container.querySelector(".wb-direction-answer-first")).toBeNull();
  });

  it("正文按标题拆成可折叠小节，标题行常驻可扫读", () => {
    const report = {
      ok: true,
      topic: "t",
      report: "## 研究问题与期限\n第一段内容。\n## 景气阶段\n第二段内容较长一些。\n",
      catalysts: [],
      risks: [],
    } as unknown as DirectionReport;
    const { container } = render(<DirectionFoldedReport report={report} />);
    const sections = container.querySelectorAll(".wb-fold-section");
    expect(sections.length).toBe(2);
    expect(container.textContent).toContain("研究问题与期限");
    expect(container.textContent).toContain("景气阶段");
    // 首节默认展开，其余折叠
    expect((sections[0] as HTMLDetailsElement).open).toBe(true);
    expect((sections[1] as HTMLDetailsElement).open).toBe(false);
  });

  it("无标题正文退化为单块渲染（不丢内容）", () => {
    const report = {
      ok: true,
      topic: "t",
      report: "一段没有标题的正文。",
      catalysts: [],
      risks: [],
    } as unknown as DirectionReport;
    const { container } = render(<DirectionFoldedReport report={report} />);
    expect(container.querySelectorAll(".wb-fold-section").length).toBe(1);
    expect(container.textContent).toContain("一段没有标题的正文。");
  });

  it("分类总表按分类给行上色（颜色标注，非低估象限可快速识别）", () => {
    const report = {
      ...judgmentReport(),
      evidence_snapshot: [
        { evidence_id: "E7", sector_name: "银行Ⅱ", valuation_judgment: "高位", valuation_judgment_rationale: "PB 分位 96.6% ≥ 70% → 高位" },
        { evidence_id: "E8", sector_name: "房地产开发", valuation_judgment: "深跌未反转", valuation_judgment_rationale: "亏损面 72.5% ≥ 40% → 深跌未反转" },
      ],
    } as unknown as DirectionReport;
    const { container } = render(<BoardJudgmentSummary report={report} />);
    expect(container.querySelector("tr.is-high")).toBeTruthy();
    expect(container.querySelector("tr.is-deep-fall")).toBeTruthy();
  });
});

/**
 * v39 追问接线（方向研判「追问复盘」页签挂到主页面）：
 * 页签渲染契约由 DirectionResultTabs 保证——挂了面板才出现页签；计数徽章区分生成中/已就绪。
 * 面板内部行为（提问/附录列表/草稿）由 followup.test.tsx 覆盖，此处不重复。
 */
describe("DirectionResultTabs 追问页签（v39 接线契约）", () => {
  it("传入 followup 面板 → 「追问复盘」页签出现，点击后渲染面板内容", () => {
    const { container } = render(
      <DirectionResultTabs
        report={poolReport()}
        onSendToTactics={noopSend}
        onStudySingle={vi.fn()}
        followup={<div data-testid="dir-followup-panel">追问面板占位</div>}
        followupCount={2}
      />,
    );
    const tab = screen.getByRole("tab", { name: /追问复盘/ });
    expect(tab).toBeTruthy();
    expect(tab.textContent).toContain("2"); // 附录计数徽章
    // 默认在概览页签，面板未渲染
    expect(screen.queryByTestId("dir-followup-panel")).toBeNull();
    fireEvent.click(tab);
    expect(screen.getByTestId("dir-followup-panel")).toBeTruthy();
  });

  it("不传 followup → 页签不出现（旧报告无 report_id 不显示空页签）", () => {
    render(<DirectionResultTabs report={poolReport()} onSendToTactics={noopSend} onStudySingle={vi.fn()} />);
    expect(screen.queryByRole("tab", { name: /追问复盘/ })).toBeNull();
  });

  it("生成中（followupActive）徽章不带 ok 态，就绪后带 ok", () => {
    const { container: busy } = render(
      <DirectionResultTabs report={poolReport()} onSendToTactics={noopSend} onStudySingle={vi.fn()} followup={<div />} followupCount={1} followupActive />,
    );
    expect(busy.querySelector(".wb-result-tab .wb-tab-badge:not(.ok)")).toBeTruthy();
    const { container: done } = render(
      <DirectionResultTabs report={poolReport()} onSendToTactics={noopSend} onStudySingle={vi.fn()} followup={<div />} followupCount={1} />,
    );
    expect(done.querySelector(".wb-result-tab .wb-tab-badge.ok")).toBeTruthy();
  });
});
