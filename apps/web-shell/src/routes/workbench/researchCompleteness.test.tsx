/**
 * v39 Q19/Q20（2026-09-19 研报与追问质量路线图 P2）：深研完成度与摘要修订留痕的呈现。
 *
 * 要防住的误读有两种：
 * - 「正文 5000 字 = 研究完整」——完成度必须由四项关键研究项决定，字数只作辅助提示；
 * - 「反方检查指出了矛盾 = 系统已经改好了」——摘要若被加注，导出里要能看见原话与标注；
 *   反方检查根本没跑时，更要直说「检查未运行」，不假装已修订。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReviewQueue, StockReport } from "./researchTypes";
import { ReportQualityPanel, ResearchCompletenessCard, ReviewQueuePanel, ReportFirstScreen, ReportResultTabs } from "./panels";
import { buildStockReportMarkdown, markdownToHtml } from "./export";
import { earningsAnalysisDisplay, earningsNoteDisplay, comparableReviewNote, valuationEvidenceDisplay, valuationEvidenceReadingNote, reportReadingSummary, valuationRepairDisplayLines } from "./format";

const COMPLETENESS = {
  version: "v1",
  items: [
    { key: "earnings_decomposition", label: "盈利增长拆解", state: "missing", state_label: "缺失",
      covered: [], gaps: ["业务量", "价格", "成本效率", "低基数", "非经常性损益"],
      note: "至少需拆到三项，才算把增长来源说清。" },
    { key: "cash_flow_and_capex", label: "现金流与资本开支", state: "partial", state_label: "部分完成",
      covered: ["经营现金流"], gaps: ["折旧摊销", "营运资本", "资本开支"],
      note: "只给覆盖倍数不足以判断盈利质量。" },
    { key: "valuation_explanation", label: "估值分化解释", state: "partial", state_label: "部分完成",
      covered: ["PE/PB 分位"], gaps: ["ROE", "净资产口径"], note: "低 PE 不等于安全边际。" },
    { key: "scenarios_and_counter_evidence", label: "情景与反证", state: "done", state_label: "已完成",
      covered: ["三情景", "反方检查"], gaps: [], note: "" },
  ],
  done_count: 1,
  partial_count: 2,
  missing_count: 1,
  score: 0.25,
  complete: false,
  headline: "关键研究项缺失：盈利增长拆解（缺失）；现金流与资本开支（部分完成）——研究完成度 1/4，字数只作辅助提示，不能替代论证。",
  word_count: 1793,
  word_count_mode: { mode: "deep", label: "深研版", min: 3500, max: 6000 },
  word_count_note: "正文 1793 字，低于深研版建议区间 3500-6000 字；字数只作辅助提示，不参与完成度判定。",
  word_count_advisory: true,
  warnings: ["关键研究项缺失（盈利增长拆解，完成度 1/4）：当前缺 业务量、价格；下一步：补来源与计算口径。"],
  note: "完成度由结构化拆解块与正文探针确定性推导，不采信模型自评。",
};

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "002468",
    title: "样本标的研报",
    report: "### 技术面\nMA20 之上运行。\n### 基本面\n盈利同比改善。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-19T10:00:00Z",
    report_chars: 1793,
    report_mode: "deep",
    ...overrides,
  } as StockReport;
}

describe("ResearchCompletenessCard · v39 Q19 深研完成度", () => {
  it("模型原有措辞显式标明来源，阅读分层不改写历史正文和限制", () => {
    const original = "估值结论不能给出，verdict为无数据。";
    const limitation = "正常化盈利置信度为low，估值状态必须为undetermined";
    const report = baseReport({ report: original, valuation: {
      pe_ttm: "14.37", pe_percentile: "71.6%", peer_position: "第4/38", verdict: "无数据", basis: "缺正常化盈利验证",
      valuation_limitations: [limitation],
    } });
    const { container } = render(<ReportResultTabs report={report} />);
    expect(container.textContent).toContain("模型提供的估值限制（原文）");
    expect(container.textContent).toContain(limitation);
    fireEvent.click(screen.getByRole("tab", { name: "正文" }));
    expect(container.textContent).toContain("以下保留报告原有措辞；应用核验及修订留痕另列。");
    expect(container.textContent).toContain(original);
    const md = buildStockReportMarkdown(report);
    for (const output of [md, new DOMParser().parseFromString(markdownToHtml(md), "text/html").body.textContent ?? ""]) {
      expect(output).toContain(original);
      expect(output).toContain(`模型提供的估值限制（原文）：${limitation}`);
    }
    expect(report.report).toBe(original);
  });
  it("首屏估值使用指标名称，保留未验证限制与完整原说明，不改非固定说明", () => {
    const note = "pe_ttm 14.37｜pe_percentile 近1250个交易日71.6%分位｜peer_position 盈利样本第4/38 已取得，但正常化盈利未经验证，不能给出低估或偏贵结论。";
    const report = baseReport({ valuation: { verdict: "无数据", pe_ttm: "14.37", pe_percentile: "近1250个交易日71.6%分位", peer_position: "盈利样本第4/38", basis: "正常化盈利未经验证", valuation_evidence: {
      state: "normalization_unverified", note,
      metrics: { peer_position: "盈利样本第4/38", pe_percentile: "近1250个交易日71.6%分位", pe_ttm: "14.37" },
    } } });
    const { container } = render(<ReportResultTabs report={report} />);
    const reading = "PE(TTM) 14.37｜历史分位 近1250个交易日71.6%分位｜同业位置 盈利样本第4/38 已取得，但正常化盈利未经验证，不能给出低估或偏贵结论。";
    expect(container.textContent).toContain(reading);
    const audit = screen.getByText("估值指标说明留痕").closest("details");
    expect(audit?.hasAttribute("open")).toBe(false);
    expect(audit?.textContent).toContain(note);
    const md = buildStockReportMarkdown(report);
    expect(md.slice(0, md.indexOf("## 生成记录附录"))).not.toContain(note);
    expect(md).toContain(reading);
    expect(md.split(note)).toHaveLength(2);
    expect(new DOMParser().parseFromString(markdownToHtml(md), "text/html").body.textContent).toContain(reading);
    const custom = valuationEvidenceDisplay({ state: "normalization_unverified", note: "需核对 pe_ttm 的计算来源", metrics: { pe_ttm: "14.37" } });
    expect(valuationEvidenceReadingNote(custom)).toBe(custom.note);
  });
  it("阅读区解释完成度边界，计算实现原话只在页面留痕与导出附录出现", () => {
    const report = baseReport({ research_completeness: COMPLETENESS });
    const reading = render(<ResearchCompletenessCard completeness={COMPLETENESS} />);
    expect(reading.container.textContent).not.toContain(COMPLETENESS.note);
    expect(reading.container.textContent).toContain("未核验或证据不足的项目不算完成");
    reading.unmount();
    const audit = render(<ReportQualityPanel report={report} />);
    const auditDetails = screen.getByText("完成度计算留痕").closest("details");
    expect(auditDetails?.textContent).toContain(COMPLETENESS.note);
    expect(auditDetails?.hasAttribute("open")).toBe(false);
    const md = buildStockReportMarkdown(report);
    const beforeAppendix = md.slice(0, md.indexOf("## 生成记录附录"));
    expect(beforeAppendix).not.toContain(COMPLETENESS.note);
    expect(beforeAppendix).toContain("未核验或证据不足的项目不算完成");
    expect(md.split(COMPLETENESS.note)).toHaveLength(2);
    const html = new DOMParser().parseFromString(markdownToHtml(md), "text/html");
    expect(html.body.textContent?.split(COMPLETENESS.note)).toHaveLength(2);
  });

  it("先交付分析和使用限制，再列完整校验与来源，来源后置仍可回查", () => {
    const report = baseReport({
      report: "### 基本面\n分析正文引用财务材料[S4]。",
      cash_flow_quality: { operating_cash_flow: 3.43, verdict: "不足以判断盈利质量" },
      research_completeness: COMPLETENESS,
      limitations: ["本报告缺少资本开支性质证据。"],
      quality_warnings: ["校验记录应完整保留。"],
      evidence_quality: { version: "v1", sources: [], tiers: { A: [], B: ["S4"] },
        coverage_vocab: [], core_support_qualities: ["B"] },
      source_catalog: [{ source_id: "S4", name: "原财务材料", url: "", quality: "B" }],
    });
    const markdown = buildStockReportMarkdown(report);
    const html = new DOMParser().parseFromString(markdownToHtml(markdown), "text/html");
    const order = ["研报正文", "深研拆解（盈利增长 · 现金流质量 · 估值分化）",
      "深研完成度", "局限与口径声明",
      "证据质量与结论支撑",
      "来源目录（可回查：编号 / 名称 / 链接 / 数据日期 / 获取时间 / 口径限制）",
      "质量校验记录"];
    const headings = Array.from(html.querySelectorAll("h2"), (node) => node.textContent);
    for (let index = 0; index < order.length; index += 1) {
      expect(markdown).toContain(`## ${order[index]}`);
      expect(headings).toContain(order[index]);
      if (index > 0) {
        expect(markdown.indexOf(`## ${order[index - 1]}`)).toBeLessThan(markdown.indexOf(`## ${order[index]}`));
        expect(headings.indexOf(order[index - 1]!)).toBeLessThan(headings.indexOf(order[index]!));
      }
    }
    for (const text of ["本报告缺少资本开支性质证据。", "校验记录应完整保留。", "原财务材料"]) {
      expect(markdown.split(text)).toHaveLength(2);
      expect((html.body.textContent ?? "").split(text)).toHaveLength(2);
    }
    const citation = Array.from(html.querySelectorAll("a")).find((node) => node.textContent === "[S4]");
    expect(citation).toBeTruthy();
    expect(html.getElementById(citation!.getAttribute("href")!.slice(1))?.textContent).toContain("原财务材料");
  });
  it.each([
    { stale_claims: ["C1"], staleness_checked: true, expected: "1 条判断所引数据超期（C1）" },
    { stale_claims: [], staleness_checked: true, expected: "不代表数据当前仍有效" },
    { stale_claims: [], staleness_checked: false, expected: "不能认定来源未过期" },
  ])("首屏与导出保留生成时效边界：$expected", ({ expected, ...timing }) => {
    const report = baseReport({ claim_findings: {
      claims: [], total: 0, supported: 0, downgraded: [], no_source: [],
      core_conclusion_supported: null, warnings: [], ...timing,
    } });
    const { container } = render(<ReportFirstScreen report={report} />);
    const md = buildStockReportMarkdown(report);
    const html = markdownToHtml(md);
    expect(container.textContent).toContain(expected);
    expect(md.slice(0, md.indexOf("## 研报正文"))).toContain(expected);
    expect(html).toContain(expected);
  });

  it("阅读核对先于长篇限制且跨页签保留一份，避免风险边界埋在概览下方", () => {
    const report = baseReport({ limitations: ["需要保留的完整限制"] });
    const { container } = render(<><ReportFirstScreen report={report} /><ReportResultTabs report={report} /></>);
    const summary = screen.getByText("阅读前核对");
    const guard = screen.getByTestId("report-firstscreen-guard");
    expect(summary.compareDocumentPosition(guard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    for (const tab of ["正文", "证据", "概览"]) {
      fireEvent.click(screen.getByRole("tab", { name: tab }));
      expect(screen.getAllByText("阅读前核对")).toHaveLength(1);
      for (const line of reportReadingSummary(report)) expect(container.textContent).toContain(line);
      expect(guard.textContent).toContain("需要保留的完整限制");
    }
  });

  it("校验附录保留完整原句、加注与规则，旧记录不补造编号", () => {
    const original = "测试原句".repeat(40);
    const report = baseReport({ valuation_language_repairs: [
      { line: 3, sentence: original, notes: ["测试风险加注"], rules: ["D01"] },
      { line: 5, sentence: "旧记录原句", notes: ["旧记录加注"] },
    ] });
    const { container } = render(<ReportResultTabs report={report} />);
    fireEvent.click(screen.getByRole("tab", { name: "证据" }));
    const md = buildStockReportMarkdown(report);
    for (const line of valuationRepairDisplayLines(report)) {
      expect(container.textContent).toContain(line);
      expect(md).toContain(line);
    }
    expect(md).toContain(original);
    expect(md).toContain("旧记录未记录编号");
    expect(md.indexOf("## 估值表述校验附录")).toBeGreaterThan(md.indexOf("## 研报正文"));
  });

  it("概览和导出共用分母说明，旧记录缺口不得显示为已完成", () => {
    const report = baseReport({ research_completeness: COMPLETENESS });
    const { container } = render(<ReportFirstScreen report={report} />);
    const md = buildStockReportMarkdown(report);
    for (const line of reportReadingSummary(report)) {
      expect(container.textContent).toContain(line);
      expect(md).toContain(line);
    }
    expect(md).toContain("研究完成度：1/4（已完成研究项数／要求研究项数）");
    expect(md).toContain("核心判断支撑：未记录可统计的判断校验结果");
    expect(md.indexOf("## 阅读前核对")).toBeLessThan(md.indexOf("## 研报正文"));
    expect(md).toContain("下一验证动作：未提供");
  });

  it("四项逐条给出状态、已覆盖与缺口，字数提示只作辅助且排在完成度之后", () => {
    const { container } = render(<ReportQualityPanel report={baseReport({ research_completeness: COMPLETENESS })} />);
    expect(screen.getByText(/深研完成度 1\/4 · 关键研究项缺失/)).toBeTruthy();
    expect(screen.getByText("盈利增长拆解")).toBeTruthy();
    expect(screen.getByText("缺失")).toBeTruthy();
    expect(screen.getAllByText("部分完成")).toHaveLength(2);
    expect(screen.getByText(/缺 业务量、价格、成本效率/)).toBeTruthy();
    expect(screen.getByText(/研究完成度 1\/4，字数只作辅助提示/)).toBeTruthy();
    expect(screen.getByText(/不参与完成度判定/)).toBeTruthy();
    // 顺序：完成度标签必须出现在字数提示之前（长而不全不能先看到「字够多」）。
    const text = container.textContent ?? "";
    expect(text.indexOf("深研完成度")).toBeLessThan(text.indexOf("正文 1793 字"));
  });

  it("旧报告没有完成度字段时整块不渲染（不凭空给一个分数）", () => {
    const { container } = render(<ResearchCompletenessCard completeness={null} />);
    expect(container.firstChild).toBeNull();
    const empty = render(<ResearchCompletenessCard completeness={{ items: [] }} />);
    expect(empty.container.firstChild).toBeNull();
  });

  it("导出写「深研完成度」并点名缺失项与辅助性质的字数提示", () => {
    const markdown = buildStockReportMarkdown(baseReport({ research_completeness: COMPLETENESS }));
    expect(markdown).toContain("## 深研完成度");
    expect(markdown).toContain("判定：关键研究项缺失（1/4）");
    expect(markdown).toContain("盈利增长拆解：缺失");
    expect(markdown).toContain("缺：业务量、价格、成本效率");
    expect(markdown).toContain("字数只作辅助提示，不参与完成度判定");
  });
});

describe("摘要修订留痕 · v39 Q20", () => {
  const revised = {
    version: "v1",
    reviewed: true,
    changed: true,
    overreach: ["多方以低 PE/PS 分位作为安全边际"],
    counter_denials: ["低 PE/PS 不等于安全边际（PB 66.9% 分位、正常化盈利未验证）"],
    summary_original: "PE 历史分位仅 1.7%，构成安全边际。",
    revised_summary: "PE 历史分位仅 1.7%，构成安全边际。（反方检查：此说不成立）",
    revision_note: "执行摘要 1 处断言已就地标注；模型原话整份保留在 summary_original。",
    revision_trace: [
      { sentence: "PE 历史分位仅 1.7%，构成安全边际。", denial: "低 PE 不等于安全边际", marker_added: true },
    ],
    warnings: ["执行摘要过度断言：1 处结论与反方检查冲突。"],
  };

  it("反方检查否认后：导出同时给出标注句与模型原话，读者能看到改了什么", () => {
    const markdown = buildStockReportMarkdown(baseReport({ summary_overreach: revised }));
    expect(markdown).toContain("## 摘要修订留痕");
    expect(markdown).toContain("反方：低 PE 不等于安全边际");
    expect(markdown).toContain("模型原话：PE 历史分位仅 1.7%，构成安全边际。");
    expect(markdown).toContain("执行摘要过度断言");
  });

  it("反方检查没跑时如实写「未运行」，不假装已修订", () => {
    const markdown = buildStockReportMarkdown(baseReport({
      summary_overreach: { reviewed: false, changed: false, revision_note: "本次未完成反方检查。" },
    }));
    expect(markdown).toContain("反方检查未运行");
    expect(markdown).toContain("不假装已修订");
  });

  it("无断言冲突时不新增小节（不把「没改动」写成改动留痕）", () => {
    const markdown = buildStockReportMarkdown(baseReport({
      summary_overreach: { reviewed: true, changed: false, revision_note: "反方检查未否认相关结论，摘要无需修订。" },
    }));
    expect(markdown).not.toContain("摘要修订留痕");
  });
});

/* ==========================================================================
   v39 Q16/Q17/Q18（P2）：深研三块拆解的呈现与导出。
   红线：贡献比例算不出就显示「未给出」（不显示 0%，也不显示估的数）；
   只有覆盖倍数时判定只能是「不足以判断盈利质量」；低 PE 分位永远不等于安全边际。
   ========================================================================== */

const DEEP = {
  earnings_growth: [
    { factor: "volume", factor_label: "业务量", effect: "业务量上升带动收入", contribution_pct: null,
      basis: "8 月业务量同比 +18%（月度经营简报）", sources: ["S2"], source_note: "S2",
      note: "未给出可复算的贡献比例（服务端不代填数字）" },
    { factor: "cost_efficiency", factor_label: "成本效率", effect: "单票成本下降", contribution_pct: 20,
      basis: "单票成本同比 -4%（中报）", formula: "(1 - 4%)", sources: ["S4"], source_note: "S4" },
  ],
  cash_flow_quality: {
    version: "v1", operating_cash_flow: 51.23, net_income: 19.5, coverage_ratio: 2.63,
    depreciation_amortization: null, working_capital: null, working_capital_change: null,
    capex: null, free_cash_flow: null, extras_present: [],
    verdict: "不足以判断盈利质量",
    note: "覆盖倍数 2.63 只说明经营现金流与净利的比值；当前只补到 无其他口径——折旧摊销、营运资本变动、资本开支至少再补两项才能谈盈利质量。",
    downgrade_note: "正文出现「盈利质量较好」式结论，但现金流口径不足（当前判定：不足以判断盈利质量）：该结论按证据不足对待，只保留为待验证说法。",
    strength: "insufficient",
  },
  valuation_divergence: {
    version: "v1", pe_ttm: "11.70", pb_mrq: "1.94", pe_percentile: "1.7% 分位", pb_percentile: "66.9% 分位",
    roe: 9.4, equity_basis: "归母净资产，不含少数股东权益", cycle_note: "利润处于近三年高位",
    explained_axes: ["roe", "cycle"], explanation_state: "partial",
    low_pe_is_not_margin_of_safety: true,
    guardrail: "低 PE 分位只说明「按当前利润算的倍数低」，利润处于周期高点时它反而危险。",
    valuation_evidence_state: "normalization_unverified",
    sensitivity_allowed: false,
    sensitivity_note: "正常化盈利未验证：不给敏感性或目标价，只报指标与分位。",
    comparable_review: {
      sample_count: 2, excluded: [{ name: "公司C", reason: "亏损，PE 无意义" }],
      exclusion_rule: "公司C：亏损，PE 无意义", comparability: "thin", comparability_label: "样本偏少",
      note: "可比样本仅 2 只：只作参照，不足以支撑「同业都这么贵/便宜」。", sensitivity_allowed: false,
    },
  },
};

describe("DeepAnalysisBlocks · v39 Q16/Q17/Q18", () => {
  it("可比样本数量冲突保留三指标分母及模式限制，内部原话只在审计留痕出现", () => {
    const count_conflict = "模型自述可比样本 5 只，服务端同业快照为 38 只（同业池共 60 只，各口径可比数：pb_mrq:60、pe_ttm:38、ps_ttm:60）——按服务端口径解释中位数，自述只作留痕。";
    const review = { sample_count: 38, model_claimed_sample_count: 5, server_peer_count: 60,
      comparable_basis: { pb_mrq: 60, pe_ttm: 38, ps_ttm: 60 }, count_conflict,
      note: `商业模式口径未核对，无法判断样本是否同模式。 ${count_conflict}`, exclusion_rule: "未声明剔除规则" };
    expect(comparableReviewNote({ note: review.note, count_conflict })).toBe(review.note);
    const report = baseReport({ valuation_divergence: { comparable_review: review } });
    const { container } = render(<ReportQualityPanel report={report} />);
    const reading = comparableReviewNote(review);
    expect(reading).toContain("商业模式口径未核对");
    expect(reading).toContain("模型列出 5 只，同业快照采用 38 只");
    expect(reading).toContain("同业池共 60 只");
    expect(reading).toContain("PB（MRQ） 60 只、PE（TTM） 38 只、PS（TTM） 60 只");
    expect(container.textContent).toContain(reading);
    const audit = screen.getByText("可比样本计算留痕").closest("details");
    expect(audit?.textContent).toContain(review.note);
    expect(audit?.hasAttribute("open")).toBe(false);
    const md = buildStockReportMarkdown(report);
    const body = md.slice(0, md.indexOf("## 生成记录附录"));
    expect(body).toContain(reading);
    expect(body).toContain(review.exclusion_rule);
    expect(body).not.toContain(count_conflict);
    expect(md.split(count_conflict)).toHaveLength(2);
    const html = new DOMParser().parseFromString(markdownToHtml(md), "text/html");
    expect(html.body.textContent).toContain(reading);
    expect(html.body.textContent?.split(count_conflict)).toHaveLength(2);
  });
  it("贡献比例限制在三种阅读交付中一致，固定实现原话只留审计且不改其他注释", () => {
    const note = "贡献比例缺少计算依据（basis/formula），按未量化处理；引用的来源不属于本次真实取到的来源：S9";
    const report = baseReport({ earnings_growth: [{ factor: "volume", factor_label: "业务量", note }] });
    const { container } = render(<ReportQualityPanel report={report} />);
    const reading = earningsNoteDisplay(note);
    expect(reading).toBe("贡献比例缺少计算依据或公式，按未量化处理；引用的来源不属于本次真实取到的来源：S9");
    expect(earningsNoteDisplay("未取得分产品销量；无法判断销量影响")).toBe("未取得分产品销量；无法判断销量影响");
    expect(container.textContent).toContain(reading);
    const audit = screen.getByText("贡献比例校验留痕").closest("details");
    expect(audit?.textContent).toContain(note);
    expect(audit?.hasAttribute("open")).toBe(false);
    const md = buildStockReportMarkdown(report);
    const body = md.slice(0, md.indexOf("## 生成记录附录"));
    expect(body).toContain(reading);
    expect(body).not.toContain("basis/formula");
    expect(md.split(note)).toHaveLength(2);
    const html = new DOMParser().parseFromString(markdownToHtml(md), "text/html");
    expect(html.body.textContent).toContain(reading);
    expect(html.body.textContent?.split(note)).toHaveLength(2);
  });
  it("证据不足的因素在页面与导出保持同一状态，旧行明确未核验", () => {
    const rows = [
      { factor: "volume", factor_label: "业务量", effect: "缺产销量，无法验证" },
      { factor: "price", factor_label: "价格", effect: "缺产品价格及价差，无法验证" },
      { factor: "cost_efficiency", factor_label: "成本效率", effect: "缺成本费用拆分，无法验证" },
    ].map((row) => ({ ...row, sources: ["S4"], source_note: "S4", contribution_pct: null,
      analysis_state: "insufficient" as const, analysis_note: "已检查，证据不足以支持该因素分析。",
      analysis_model: "test-factor-review" }));
    const report = baseReport({ earnings_growth: rows });
    const { container } = render(<ReportQualityPanel report={report} />);
    const markdown = buildStockReportMarkdown(report);
    const html = new DOMParser().parseFromString(markdownToHtml(markdown), "text/html");
    const status = "已检查，证据不足；已检查，证据不足以支持该因素分析。（核验模型：test-factor-review）";
    for (const output of [container.textContent ?? "", markdown, html.body.textContent ?? ""]) {
      expect(output.split(status)).toHaveLength(4); // 三条缺口均留痕，不能被当作已完成分析。
      for (const row of rows) expect(output).toContain(row.effect);
      expect(output).not.toContain("证据核验支持完成分析");
      expect(output).not.toContain("0%");
    }
    expect(earningsAnalysisDisplay({ factor: "volume" })).toBe("未核验，不计为完成分析");
  });
  it("贡献比例算不出显示「未给出」而不是 0%；现金流只有倍数时结论自动降档；低 PE 不当安全边际", () => {
    render(<ReportQualityPanel report={baseReport(DEEP as Partial<StockReport>)} />);
    expect(screen.getByText("盈利增长拆解（贡献比例只在能从原始数据复算时才给）")).toBeTruthy();
    expect(screen.getByText("未给出")).toBeTruthy();
    expect(screen.getByText("20%")).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
    expect(screen.getByText("未给出").getAttribute("title")).toBe("缺少可复算的贡献比例，不补填数值");
    expect(screen.getByText("不足以判断盈利质量")).toBeTruthy();
    expect(screen.getByText(/正文出现「盈利质量较好」式结论/)).toBeTruthy();
    expect(screen.getByText(/可比样本 2（样本偏少）/).textContent).toContain("2");
    expect(screen.getByText(/剔除规则：公司C：亏损，PE 无意义/)).toBeTruthy();
    expect(screen.getByText(/正常化盈利未验证：不给敏感性或目标价/).textContent).toContain("低 PE 分位 ≠ 安全边际");
    expect(screen.getByText(/利润处于近三年高位/)).toBeTruthy();
  });

  it("三块都缺的旧报告不渲染该区块（不凭空补拆解）", () => {
    const { container } = render(<ReportQualityPanel report={baseReport()} />);
    expect(container.querySelector(".wb-deep-analysis")).toBeNull();
  });

  it("导出含「深研拆解」：未给出的比例、现金流降档与可比剔除规则都在", () => {
    const markdown = buildStockReportMarkdown(baseReport(DEEP as Partial<StockReport>));
    expect(markdown).toContain("## 深研拆解（盈利增长 · 现金流质量 · 估值分化）");
    expect(markdown).toContain("| 因素 | 作用 | 贡献 | 依据与来源 |");
    expect(markdown).toContain("未给出");
    expect(markdown).toContain("判定：不足以判断盈利质量");
    expect(markdown).toContain("降档说明：正文出现「盈利质量较好」式结论");
    expect(markdown).toContain("剔除规则：公司C：亏损，PE 无意义");
    expect(markdown).toContain("低 PE 分位只说明「按当前利润算的倍数低」");
  });
});

/* ==========================================================================
   v39 Q21：验证点复盘五态——「已加入跟踪」只能给真落成了可回填判断的条目。
   ========================================================================== */

describe("ReviewQueuePanel · v39 Q21 复盘五态", () => {
  const queue = {
    ok: true,
    today: "2026-09-19",
    counts: { overdue: 0, scheduled: 2, event_anchored: 0 },
    item_count: 2,
    tracking: {
      version: "v1",
      counts: { suggest_observe: 1, tracked: 1, pending_verify: 0, verified: 0, unverifiable: 0 },
      summary: "建议观察 1、已加入跟踪 1",
      note: "本表只登记已落成的跟踪与人工回填结果：事件到期不会自动监控。",
      auto_monitoring: false,
    },
    items: [
      { report_id: "r1", symbol: "002468", signal: "量能放大配合站上触发价", due_on: "2026-09-21",
        verify_by: "2026-09-21 收盘后", bucket: "scheduled", judgment_id: "j1", status: "pending",
        tracking_state: "tracked", tracking_label: "已加入跟踪", tracking_note: "已加入待复盘队列（到期 2026-09-21）。" },
      { report_id: "r1", symbol: "002468", signal: "月度经营数据能否延续", due_on: "",
        verify_by: "下一次月度经营简报披露后", bucket: "event_anchored", judgment_id: null, status: "pending",
        tracking_state: "suggest_observe", tracking_label: "建议观察",
        tracking_note: "验证时点锚定事件、无确切日期，未落成跟踪记录：等事件发生后再加入待复盘。" },
    ],
  } as unknown as ReviewQueue;

  it("五态标签逐条可见，事件锚定点显示「建议观察」而非「已加入」", () => {
    render(<ReviewQueuePanel queue={queue} onOpenReport={vi.fn()} onRefill={vi.fn()} />);
    expect(screen.getByText("跟踪：已加入跟踪")).toBeTruthy();
    expect(screen.getByText("跟踪：建议观察")).toBeTruthy();
    expect(screen.getByText(/建议观察 1、已加入跟踪 1/)).toBeTruthy();
    expect(screen.getByText(/不会自动监控/)).toBeTruthy();
  });
});

/* ==========================================================================
   v39 真机回归（2026-09-20）：002468 那份 2.9 报告里，7 条核心判断全部没有 claim_id，
   于是同一份导出同时出现「部分支撑（2/3）」与「七条全都未列入核心」——
   分母按重要度算出来了，列却按编号匹配，匹配不上就全判否。
   ========================================================================== */

describe("核心支撑统计 · 无编号旧档案（v39 真机）", () => {
  const findings = {
    claims: [
      { claim_id: "", text: "H1 归母净利同比大增", sources: ["S4"], dropped_sources: [], requires: ["net_income"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", unknown_requires: [], importance: "high", support: "full" },
      { claim_id: "", text: "PE 与 PB 分化", sources: ["S5"], dropped_sources: [], requires: ["pe"], missing_coverage: [], evidence_quality: "C", status: "downgraded", note: "", unknown_requires: [], importance: "high", support: "partial" },
      { claim_id: "", text: "技术信号待确认", sources: ["S1"], dropped_sources: [], requires: ["trend"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", unknown_requires: [], importance: "medium", support: "full" },
    ],
    counts: { supported: 2, downgraded: 1, no_source: 0 },
    warnings: [],
  } as never;

  it("入核心支撑统计按位置判定，不再因缺编号把每条都判成「未列入核心」", () => {
    const md = buildStockReportMarkdown(baseReport({ claim_findings: findings }));
    expect(md).toContain("部分支撑（1/2）");
    expect(md).toContain("是 · 计入分子（完整支撑）");
    expect(md).toContain("是 · 仅计入分母");
    expect(md).toContain("否（未列入核心）");
  });

  it("被统计的判断点得出名字：无编号时按「第 n 条」回查，不显示成一片「—」", () => {
    const md = buildStockReportMarkdown(baseReport({ claim_findings: findings }));
    expect(md).toContain("计入统计的核心判断：第1条、第2条");
    expect(md).not.toContain("计入统计的核心判断：—");
  });
});

describe("DeepAnalysisBlocks · 可比口径未核对（v39 第二轮真机）", () => {
  it("模型没给标的商业模式时写「模式未核对」，不再冒称「已核对」", () => {
    render(
      <ReportQualityPanel
        report={baseReport({
          valuation_divergence: {
            pe_ttm: 11.69555428,
            pe_percentile: "1.7%，近1250个交易日自2021-07-27",
            comparable_review: {
              sample_count: 7, model_claimed_sample_count: 5, excluded_count: 13,
              comparability: "unverified", comparability_label: "模式未核对",
              note: "7 只样本，剔除 13 只；商业模式口径**未核对**（模型未给出标的商业模式，无法判断样本是否同模式）。",
              exclusion_rule: "公司C：亏损，PE 无意义",
            },
          },
        } as never)}
      />,
    );
    const chip = screen.getByText(/模式未核对/);
    expect(chip.className).toContain("warn");
    expect(chip.textContent).not.toContain("已核对");
    // 分位带着窗口出现，避免「1.7%」被读成全历史分位。
    expect(document.body.textContent).toContain("1.7%，近1250个交易日自2021-07-27");
    expect(document.body.textContent).not.toContain("（1.7%（");  // 不再套两层括号
    // 全精度浮点按两位小数收口。
    expect(document.body.textContent).toContain("PE 11.7");
  });
});
