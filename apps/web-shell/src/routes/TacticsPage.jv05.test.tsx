/**
 * JV05（Jev 决策模型接入路线图 2026-09-21）：扫描批量去误报的**呈现契约**。
 *
 * 锁住三件用户能直接看见、且最容易做错的事：
 *
 * 1. **关闭时界面零变化**——`jev_review.enabled === false` 时统计行与折叠都不出现，
 *    与接入前逐字节一致（JV00 铁律 6）。这一条比「打开时好看」更重要。
 * 2. **三种「没标注」视觉可分**——`reviewed`（绿）/ `pending_verification`（警告）/
 *    字段缺失（灰「语义未核」）。把「没送出去评」渲染成空白，用户会默认它跟「已复核」一样可信。
 * 3. **「待核验」是折叠不是删除**——被折叠的票**仍然在结果里**，展开就能看到。
 *    删掉一行就是改写规则引擎的输出。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import {
  JevReviewSummary,
  JevTag,
  ScanResultTable,
  type JevReview,
  type RowActions,
  type ScanResult,
  type ScanRow,
  type HitDetail,
} from "./TacticsPage";

afterEach(cleanup);

function row(overrides: Partial<ScanRow> = {}): ScanRow {
  return {
    symbol: "600176",
    name: "中国巨石",
    ok: true,
    hit_tactics: [],
    latest_signals: [],
    tactic_score: 58,
    rank_score: 58,
    eligible: true,
    data_quality: {
      bars_count: 250,
      required_bars: 35,
      sample_sufficient: true,
      insufficient_tactics: [],
      latest_bar_date: "2026-09-18",
      missing_fields: [],
      level: "ok",
    },
    ...overrides,
  };
}

function review(overrides: Partial<JevReview> = {}): JevReview {
  return {
    enabled: true,
    model: "typesafe/jev-1.13-20260917",
    total: 3,
    reviewed: 1,
    pending: 1,
    unannotated: 1,
    verdicts: { valid: 1, doubtful: 0, invalid: 1 },
    skipped: [],
    shards: 1,
    elapsed_ms: 2400,
    note: "语义复核只做标注与分组，不改判定、不删除候选；「未标注」表示本票未取得判定，不等于形态成立",
    ...overrides,
  };
}

const actions: RowActions = {
  catalog: [],
  busy: false,
  onLoadSignals: () => undefined,
  onSaveWatch: async () => true,
};

function scanResult(rows: ScanRow[], jevReview?: JevReview): ScanResult & { jev_review?: JevReview } {
  return { results: rows, scanned: rows.length, generated_at: "2026-09-21T10:00:00+00:00", jev_review: jevReview };
}

describe("T01 · 主信号展示必须与后端选取一致", () => {
  const hits: HitDetail[] = [
    { tactic_id: "ma_bullish_alignment", tactic_name: "均线多头排列", direction: "bullish", category: "ma", weight: 6, date: "2026-09-17", age_bars: 1, freshness: 0.93, contribution: 0, min_bars: 20, counted: false, family_merged: true, invalidation_rule: "排列破坏" },
    { tactic_id: "ma_golden_cross", tactic_name: "均线金叉", direction: "bullish", category: "ma", weight: 7, date: "2026-09-18", age_bars: 0, freshness: 1, contribution: 7, min_bars: 20, counted: true, invalidation_rule: "测试失效条件：跌回触发平台" },
  ];

  it("不取第一条命中，名称、日期和失效条件均关联后端指定信号", () => {
    render(<ScanResultTable result={scanResult([row({
      primary_tactic_id: "ma_golden_cross", hit_tactics: hits.map((hit) => hit.tactic_id), hit_detail: hits,
      direction_bias: "conflict", gating_reasons: ["测试门槛：主信号已失效"],
      primary_signal_state: { state: "invalidated", label: "已失效", reason: "测试失效原因", signal_date: "2026-09-18", observed_bars: 3, window_bars: 5, min_bars: 1, volume_ratio_threshold: 1, invalidation_volume_ratio: 1, version: "test" },
    })])} actions={actions} />);
    for (const text of ["主信号：均线金叉", "触发日：2026-09-18", "状态：已失效", "失效条件：测试失效条件：跌回触发平台", "多空冲突：存在", "测试门槛：主信号已失效"]) {
      expect(screen.getByText(text).closest("details")).toBeNull();
    }
    const fold = screen.getByText("全部命中（2）").closest("details")!;
    expect(fold.open).toBe(false);
    fireEvent.click(screen.getByText("全部命中（2）"));
    expect(fold.open).toBe(true);
    expect(fold.textContent).toContain("均线多头排列同族合并·不计分");
    expect(fold.textContent).toContain("均线金叉计分");
  });

  it("旧数据缺主信号标识时不按日期、顺序或贡献重选，缺失效规则时不补造", () => {
    const { rerender } = render(<ScanResultTable result={scanResult([row({ hit_tactics: hits.map((hit) => hit.tactic_id), hit_detail: hits })])} actions={actions} />);
    expect(screen.getByText("主信号：未提供")).toBeTruthy();
    expect(screen.getByText("失效条件：未提供")).toBeTruthy();
    rerender(<ScanResultTable result={scanResult([row({ primary_tactic_id: "ma_golden_cross", hit_tactics: ["ma_golden_cross"], hit_detail: [{ ...hits[1]!, invalidation_rule: undefined }] })])} actions={actions} />);
    expect(screen.getByText("主信号：均线金叉")).toBeTruthy();
    expect(screen.getByText("失效条件：未提供")).toBeTruthy();
  });
});

describe("T02 · 收起低频操作后仍可处理同一标的", () => {
  it.each([undefined, "", "行情服务返回错误"])("失败行始终说明取数状态，已有原因保持原文：%s", (error) => {
    render(<ScanResultTable result={scanResult([row({ ok: false, error })])} actions={actions} />);
    const failedRow = screen.getByText("600176").closest("tr")!;
    expect(within(failedRow).getByText(error || "取数失败（未提供原因）")).toBeVisible();
    expect(screen.getByText("取数失败 1 票")).toBeVisible();
    expect(within(failedRow).queryByText("窗口内无信号")).toBeNull();
  });

  it("异常行保留且按后端顺序显示，展开说明和操作不会改变规则分", () => {
    const rows = [
      row({ symbol: "600176", tactic_score: 20, rank_score: 20, eligible: false, gating_reasons: ["主信号已经失效"] }),
      row({ symbol: "600519", name: "贵州茅台", tactic_score: 70, rank_score: 70, eligible: false,
        data_quality: { bars_count: 10, required_bars: 35, sample_sufficient: false, insufficient_tactics: [], latest_bar_date: "2026-09-18", missing_fields: [], level: "insufficient" },
        gating_reasons: ["行情样本不足"] }),
      row({ symbol: "000001", name: "平安银行", ok: false, error: "行情读取失败" }),
    ];
    const before = JSON.stringify(rows);
    const { container } = render(<ScanResultTable result={scanResult(rows)} actions={{ ...actions, onOpenStockReport: vi.fn() }} />);
    const order = () => Array.from(container.querySelectorAll("table.kv-table tbody tr")).map((tr) => tr.querySelector("button")?.textContent);
    expect(order()).toEqual(["600176", "600519", "000001"]);
    for (const text of ["主信号已经失效", "行情样本不足", "行情读取失败"]) {
      const element = screen.getByText(text);
      expect(element.closest("details")).toBeNull();
      expect(element).toBeVisible();
    }
    fireEvent.click(screen.getByText("排名口径与公式（图形分 × 数据质量因子）"));
    for (const summary of screen.getAllByText("更多操作")) fireEvent.click(summary);
    expect(order()).toEqual(["600176", "600519", "000001"]);
    expect(JSON.stringify(rows)).toBe(before);
  });

  it("折叠入口保留忙碌、已自选与模型未接通的禁用约束", () => {
    const callbacks = { onSaveWatch: vi.fn(), onAddToWatchlist: vi.fn(), onRunAiReview: vi.fn() };
    render(<ScanResultTable result={scanResult([row()])} actions={{ ...actions, ...callbacks,
      busy: true, trackedInstruments: ["600176"], aiReady: false,
    }} />);
    fireEvent.click(screen.getByText("更多操作"));
    for (const name of ["+观察", "已自选", "AI 复核"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    for (const callback of Object.values(callbacks)) expect(callback).not.toHaveBeenCalled();
  });

  it("默认保留详情和观察，展开后报告、自选、复核沿用原始代码与回调", () => {
    const onLoadSignals = vi.fn();
    const onSaveWatch = vi.fn(async () => true);
    const onOpenStockReport = vi.fn();
    const onAddToWatchlist = vi.fn(async () => true);
    const onRunAiReview = vi.fn();
    render(<ScanResultTable result={scanResult([row()])} actions={{
      ...actions, onLoadSignals, onSaveWatch, onOpenStockReport,
      onAddToWatchlist, onRunAiReview, aiReady: true,
    }} />);
    const more = screen.getByText("更多操作").closest("details")!;
    const formula = screen.getByText("排名口径与公式（图形分 × 数据质量因子）").closest("details")!;
    expect(more.open).toBe(false);
    expect(formula.open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "详情" }));
    fireEvent.click(screen.getByRole("button", { name: "+观察" }));
    expect(onLoadSignals).toHaveBeenCalledWith("600176");
    expect(onSaveWatch).toHaveBeenCalledWith("600176", "中国巨石", "");
    fireEvent.click(screen.getByText("更多操作"));
    expect(more.open).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "AI 报告" }));
    fireEvent.click(screen.getByRole("button", { name: "+自选" }));
    fireEvent.click(screen.getByRole("button", { name: "AI 复核" }));
    expect(onOpenStockReport).toHaveBeenCalledWith("600176", expect.objectContaining({ sourceLabel: "战法雷达" }));
    expect(onAddToWatchlist).toHaveBeenCalledWith("600176", "中国巨石");
    expect(onRunAiReview).toHaveBeenCalledWith("600176", null);
  });
});

describe("JevReviewSummary（统计行）", () => {
  it("enabled=false 时什么都不渲染——关闭即零变化", () => {
    const { container } = render(<JevReviewSummary review={review({ enabled: false })} />);
    expect(container.textContent).toBe("");
  });

  it("三个数字分开列，并把「未核」与「通过」在措辞上分开", () => {
    render(<JevReviewSummary review={review()} />);
    const text = screen.getByText(/Jev 语义复核/).closest("p")?.textContent ?? "";
    expect(text).toContain("已复核");
    expect(text).toContain("待核验");
    // 「未核」不能写成「其它」这类中性词——它**不是**「通过」
    expect(text).toContain("未核");
    expect(text).toContain("判定 成立 1 / 存疑 0 / 疑似误报 1");
    expect(text).toContain("typesafe/jev-1.13-20260917");
    expect(text).toContain("复核耗时 2.4s");
    // 免责说明必须在场，否则「未核 1」会被读成「1 只通过」
    expect(text).toContain("不等于形态成立");
    expect(text).toContain("不改判定、不参与排序、不删除候选");
  });

  it("有跳过票时把原因带出来（不静默吞）", () => {
    render(
      <JevReviewSummary
        review={review({
          skipped: [{ symbol: "600519", reason: "over_candidate_limit", note: "超出单轮复核上限 200 票，本轮未送评" }],
        })}
      />,
    );
    const text = screen.getByText(/Jev 语义复核/).closest("p")?.textContent ?? "";
    expect(text).toContain("未送评 1 票");
    expect(text).toContain("超出单轮复核上限 200 票");
  });
});

describe("JevTag（逐票标签）", () => {
  it("字段缺失（Jev 关闭）时不渲染任何标签", () => {
    const { container } = render(<JevTag row={row()} />);
    expect(container.textContent).toBe("");
  });

  it("reviewed 显示已复核与置信度", () => {
    render(<JevTag row={row({ jev_review_state: "reviewed", jev_verdict: "valid", jev_confidence: 0.82, jev_note: "语义复核通过（置信度 0.82）" })} />);
    expect(screen.getByText(/语义已复核/)).toBeTruthy();
    expect(screen.getByText(/0\.82/)).toBeTruthy();
  });

  it("pending_verification 显示待核验 + 标签 + 置信度", () => {
    render(<JevTag row={row({ jev_review_state: "pending_verification", jev_verdict: "invalid", jev_label: "疑似误报", jev_confidence: 0.88, jev_note: "语义判定「疑似误报」，进待核验" })} />);
    expect(screen.getByText(/待核验/)).toBeTruthy();
    expect(screen.getByText(/疑似误报/)).toBeTruthy();
  });

  it("review_state=null（根本没送出去评）显示「语义未核」，不能留空白", () => {
    render(<JevTag row={row({ jev_review_state: null, jev_verdict: null, jev_label: "未取得判定", jev_note: "本票所在分片未取得应答（服务端返回 429（触发限流）），未标注——不等于形态成立" })} />);
    expect(screen.getByText("语义未核")).toBeTruthy();
  });
});

describe("ScanResultTable（折叠）", () => {
  it("Jev 关闭时：无统计行、无折叠、全部行照常显示", () => {
    const rows = [row({ symbol: "600176" }), row({ symbol: "600519", name: "贵州茅台" })];
    const { container } = render(
      <ScanResultTable result={scanResult(rows, review({ enabled: false }))} actions={actions} />,
    );
    expect(container.querySelector(".jev-pending-fold")).toBeNull();
    expect(screen.queryByText(/Jev 语义复核/)).toBeNull();
    expect(screen.getByText("600176")).toBeTruthy();
    expect(screen.getByText("600519")).toBeTruthy();
  });

  it("Jev 打开时：待核验折叠起来，但票**仍在结果里**（展开可见）", () => {
    const rows = [
      row({ symbol: "600176", jev_review_state: "reviewed", jev_verdict: "valid", jev_label: "形态成立", jev_confidence: 0.91 }),
      row({ symbol: "600519", name: "贵州茅台", jev_review_state: "pending_verification", jev_verdict: "invalid", jev_label: "疑似误报", jev_confidence: 0.88 }),
    ];
    const { container } = render(<ScanResultTable result={scanResult(rows, review())} actions={actions} />);

    const fold = container.querySelector(".jev-pending-fold");
    expect(fold).toBeTruthy();
    expect(fold?.hasAttribute("open")).toBe(false); // 默认折叠
    expect(fold?.textContent).toContain("待核验（1 只，默认折叠）");
    expect(fold?.textContent).toContain("仍在本轮扫描结果里");
    // 折叠里确实有那一行（没被删除）
    expect(fold?.textContent).toContain("600519");
    // 主列表里只有已复核的那只
    const mainTable = container.querySelector("table.kv-table");
    expect(mainTable?.textContent).toContain("600176");
    expect(mainTable?.textContent).not.toContain("600519");
  });

  it("未标注（unannotated）的票留在主列表并挂灰标签，不藏进折叠", () => {
    const rows = [
      row({ symbol: "600176", jev_review_state: null, jev_verdict: null, jev_label: "未送评", jev_note: "超出单轮复核上限 200 票，本轮未送评" }),
    ];
    const { container } = render(
      <ScanResultTable result={scanResult(rows, review({ reviewed: 0, pending: 0, unannotated: 1 }))} actions={actions} />,
    );
    expect(container.querySelector(".jev-pending-fold")).toBeNull();
    expect(container.querySelector("table.kv-table")?.textContent).toContain("600176");
    expect(screen.getByText("语义未核")).toBeTruthy();
  });
});
