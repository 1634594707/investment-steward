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
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import {
  JevReviewSummary,
  JevTag,
  ScanResultTable,
  type JevReview,
  type RowActions,
  type ScanResult,
  type ScanRow,
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
