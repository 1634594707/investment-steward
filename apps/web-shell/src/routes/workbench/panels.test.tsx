/**
 * v31 研报质量恢复（2026-09-13 方案）前端面板测试：
 * 三态首屏 / 不完整横幅 / 正文目录缺失占位 / 关键数字高亮（按 claim 重要度，不做关键词染色）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { StockReport } from "./researchTypes";
import { MarkdownishText, ReportBodyToc, ReportFirstScreen, keyFactHighlights, EvidenceQualityPanel, DecisionCardPanel } from "./panels";

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "600000",
    title: "测试研报",
    report: "### 技术面\nMA20 6.35 之上运行，量能温和放大。\n### 综合判断\n总体偏多。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-13T00:00:00Z",
    report_chars: 2153,
    report_mode: "standard",
    ...overrides,
  };
}

describe("ReportFirstScreen（§4.1 首屏固定行）", () => {
  it("展示 模式 · 字数 · 质量状态 三元组", () => {
    render(<ReportFirstScreen report={baseReport({ quality_status: "complete", quality_status_label: "完整" })} />);
    expect(screen.getByText("标准版")).toBeTruthy();
    expect(screen.getByText("正文 2153 字")).toBeTruthy();
    expect(screen.getByText("完整")).toBeTruthy();
  });

  it("incomplete 时显示红色横幅与缺失小节，不冒充正常报告", () => {
    render(
      <ReportFirstScreen
        report={baseReport({
          quality_status: "incomplete",
          quality_status_label: "不完整",
          is_draft: true,
          missing_sections: ["消息面", "基本面"],
          quality_blockers: [
            { code: "missing_section", section: "消息面", message: "缺少必需小节「消息面」。" },
            { code: "missing_section", section: "基本面", message: "缺少必需小节「基本面」。" },
          ],
        })}
      />,
    );
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText(/生成不完整/)).toBeTruthy();
    expect(screen.getByText("消息面 · 未生成")).toBeTruthy();
    expect(screen.getByText("基本面 · 未生成")).toBeTruthy();
    expect(screen.getByText("草稿 · 不计入正式报告")).toBeTruthy();
  });
});

describe("ReportBodyToc（§4.2 四节目录锚点）", () => {
  it("缺失小节显示红色「未生成」占位", () => {
    render(
      <ReportBodyToc
        report={baseReport({
          section_states: [
            { section: "技术面", chars: 30, present: true },
            { section: "消息面", chars: 0, present: false },
            { section: "基本面", chars: 0, present: false },
            { section: "综合判断", chars: 25, present: true },
          ],
        })}
      />,
    );
    expect(screen.getByText("技术面")).toBeTruthy();
    expect(screen.getByText("消息面 · 未生成")).toBeTruthy();
    expect(screen.getByText("基本面 · 未生成")).toBeTruthy();
    expect(screen.getByText("综合判断")).toBeTruthy();
  });

  it("旧记录无状态数据时只渲染锚点，不断言缺失（不编造）", () => {
    render(<ReportBodyToc report={baseReport()} />);
    expect(screen.queryByText(/未生成/)).toBeNull();
  });
});

describe("keyFactHighlights（§八.5 按 claim 重要度定位）", () => {
  it("high+full → 蓝，high+partial → 琥珀，high+none → 珊瑚红；低重要度不染色", () => {
    const report = baseReport({
      conclusion: { statement: "PE-TTM 19.7 处于历史低分位，但现金流含金量存疑。", direction: "中性", core_conflict: "", confidence: "medium" },
      claims: [
        { claim_id: "C1", text: "", sources: [], dropped_sources: [], requires: [], unknown_requires: [], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", importance: "high", support: "full" },
        { claim_id: "C2", text: "负债率 68.5 需要进一步核对", sources: [], dropped_sources: [], requires: [], unknown_requires: [], missing_coverage: [], evidence_quality: "C", status: "downgraded", note: "", importance: "high", support: "partial" },
        { claim_id: "C3", text: "单季亏损 3.2 无来源支撑", sources: [], dropped_sources: [], requires: [], unknown_requires: [], missing_coverage: [], evidence_quality: null, status: "no_source", note: "", importance: "low", support: "none" },
      ],
    });
    const highlights = keyFactHighlights(report);
    const tones = highlights.map((item) => item.tone);
    expect(tones).toContain("blue");
    expect(tones).toContain("amber");
    expect(tones).not.toContain("coral"); // C3 importance=low → 不进高亮
    // 每个 highlight 都带文字标签（title），满足「文字+颜色双重表达」
    expect(highlights.every((item) => item.title && item.title.length > 0)).toBe(true);
  });
});

describe("MarkdownishText（锚点 + 高亮）", () => {
  it("标题命中必需小节时写入锚点 id，数字按 highlights 着色且带 title", () => {
    const { container } = render(
      <MarkdownishText
        content={"### 技术面\nMA20 6.35 之上运行。\n"}
        highlights={[{ text: "6.35", tone: "blue", title: "核心判断中的关键数字" }]}
      />,
    );
    const anchor = container.querySelector("#wb-sec-技术面");
    expect(anchor).toBeTruthy();
    const mark = container.querySelector("mark.rpt-hl-blue");
    expect(mark).toBeTruthy();
    expect(mark?.getAttribute("title")).toBe("核心判断中的关键数字");
    expect(mark?.textContent).toBe("6.35");
  });

  it("§4.2 长段落默认折叠且可展开，折叠态下高亮颜色保留；短段落不折叠", async () => {
    const user = userEvent.setup();
    const longSentence = "股价沿均线运行，量能温和放大，趋势结构完好，短期支撑与压力位清晰，等待量能进一步确认方向。".repeat(10);
    const { container } = render(
      <MarkdownishText
        content={`### 技术面\n${longSentence}关键数字 19.73 待确认。\n\n短线缩量整理。`}
        highlights={[{ text: "19.73", tone: "amber", title: "待核验数字" }]}
      />,
    );
    const collapsed = container.querySelector(".wb-md-collapse");
    expect(collapsed).toBeTruthy();
    const toggle = collapsed?.querySelector(".wb-md-toggle") as HTMLButtonElement | null;
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector("mark.rpt-hl-amber")).toBeTruthy(); // 折叠不影响高亮
    await user.click(toggle!);
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(toggle?.textContent).toBe("收起");
    // 短段落不折叠
    const paragraphs = container.querySelectorAll(".wb-md-p");
    expect(paragraphs.length).toBe(2);
    expect(container.querySelectorAll(".wb-md-collapse").length).toBe(1);
  });
});

/* —— v39（2026-09-19 研报与追问质量路线图）：Q05 结论卡估值行 / Q09 来源目录上屏 —— */

describe("EvidenceQualityPanel · Q09 来源目录", () => {
  it("有链接给链接，没链接如实标注「（未提供链接）」，并回指被哪些判断使用", () => {
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({
          source_catalog: [
            { source_id: "S1", name: "腾讯财经行情", url: "https://qt.gtimg.cn/q=sz002468", data_date: "2026-09-19", retrieved_at: "2026-09-19T18:10:21", quality: "C", scope_note: "前复权日线", cited: true, used_by: ["C1", "C3"] },
            { source_id: "S5", name: "东方财富估值", url: "", url_note: "来源未提供可访问链接（如实标注，不猜测）", data_date: "2026-09-18", retrieved_at: "2026-09-19T18:10:22", quality: "C", scope_note: "", cited: false, used_by: [] },
          ],
        })}
      />,
    );
    expect(container.querySelector("a[href='https://qt.gtimg.cn/q=sz002468']")).toBeTruthy();
    const text = container.querySelector(".wb-source-catalog")!.textContent!;
    expect(text).toContain("（未提供链接：来源未提供可访问链接（如实标注，不猜测））");
    expect(text).toContain("C1、C3");
    expect(text).toContain("可得未引用");
  });

  it("九列目录是宽表：包在横向滚动容器里，窄窗（≈480）不裁列也不撑破正文（Q23）", () => {
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({
          source_catalog: [
            { source_id: "S1", name: "腾讯财经行情", url: "https://qt.gtimg.cn/q=sz002468", data_date: "2026-09-19", retrieved_at: "2026-09-19T18:10:21", quality: "C", scope_note: "前复权日线", cited: true, used_by: ["C1"] },
          ],
        })}
      />,
    );
    const scroller = container.querySelector(".wb-source-catalog > .wb-table-scroll");
    const table = scroller?.querySelector("table.wb-source-catalog-table");
    expect(table).toBeTruthy();
    // 「列数 > 滚动容器」是这套布局的全部前提：宽表一旦脱离 .wb-table-scroll 就会在窄窗越界。
    expect(table!.querySelectorAll("thead th").length).toBeGreaterThanOrEqual(6);
    const css = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "styles.css"), "utf8");
    expect(css).toMatch(/\.wb-table-scroll\s*\{[^}]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.wb-source-catalog-table\s+th\s*\{[^}]*white-space:\s*normal/);
  });

  it("旧记录没有来源目录时整块不渲染（不写空表冒充梳理过）", () => {
    const { container } = render(
      <EvidenceQualityPanel
        report={baseReport({
          evidence_quality: { version: "1", sources: [], tiers: { A: [], B: [], C: [], D: [] }, coverage_vocab: [], core_support_qualities: ["B"] },
        })}
      />,
    );
    expect(container.querySelector(".wb-source-catalog")).toBeNull();
  });
});

describe("DecisionCardPanel · Q05 估值行", () => {
  const card = {
    version: "1",
    symbol: "002468",
    conclusion: { statement: "旺季效应待证据", direction: "conflict", core_conflict: "", confidence: "low" },
    evidence_strength: { counts: {}, total_sources: 3, core_min_quality: "B" },
    valuation_label: "无数据",
    max_support: null,
    max_counter: null,
    counter_check_reason: "",
    next_action: null,
  };

  it("结论卡的「无数据」被三态措辞替换（与估值卡、导出同源）", () => {
    const { container } = render(
      <DecisionCardPanel
        report={baseReport({
          decision_card: card as never,
          valuation: {
            pe_ttm: "11.70", pe_percentile: "近5年 1.7% 分位", peer_position: "低于同业中位",
            verdict: "无数据", basis: "",
            valuation_status: "undetermined",
            normalized_earnings: { value: null, basis: "未单列非经常性损益", confidence: "low" },
          },
        })}
      />,
    );
    const text = container.textContent!;
    expect(text).toContain("合理价值未评估（指标已有，缺正常化盈利验证）");
    expect(text).toContain("指标已取得，合理价值尚未评估");
    expect(text).not.toContain("无数据");
  });
});
