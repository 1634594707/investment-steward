/**
 * 研报产出与交付链路路线图 2026-09-27（甲组渲染缺陷 R1–R7 + D2/D3）前端回归。
 *
 * 覆盖：
 * - R1：关键数字高亮候选取值收窄（年份不进候选）+ 屏显与导出 <mark> 序列逐字相同；
 * - R2：来源目录「可用链接」列渲染为可点击 <a rel="noopener noreferrer">；
 * - R3：缩进列表项渲染为嵌套 <ul>，不再退化成带 `  - ` 残留的 <p>；
 * - R4：标题按 `#` 数量映射层级 + 生成文档目录；
 * - R5：免责声明全篇恰好 1 处（blockquote 渲染，不再被转义成 `&gt;`）；
 * - R6：核心判断表有编号列；
 * - R7：来源获取时间与生成时间同口径（本地时区、秒级）。
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import type { ClaimFinding, SourceCatalogEntry, StockReport } from "./researchTypes";
import { buildHtmlDocument, buildStockReportMarkdown, markdownToHtml } from "./export";
import { keyFactHighlights, renderInline, SourceCatalogTable } from "./panels";
import { formatTimestamp, type ReportHighlight } from "./format";

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "002468",
    title: "申通快递：测试",
    report: "### 技术面\n收盘 14.90。",
    citations: ["S1"],
    available_citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-27T16:56:39Z",
    report_chars: 3561,
    report_mode: "deep",
    ...overrides,
  } as StockReport;
}

describe("R05 逐篇来源在页面及两种导出中保持身份", () => {
  it.each([
    { source_id: "S2", name: "新闻摘要", quality: "B", urls: ["https://example.test/news-1", "https://example.test/news-2"] },
    { source_id: "S2", name: "新闻摘要", quality: "B", urls: ["https://example.test/news-1", ""] },
    { source_id: "S3", name: "公告标题", quality: "B", urls: ["https://example.test/notice-1", ""] },
    { source_id: "S3A", name: "公告正文", quality: "A", urls: ["https://example.test/body-1.pdf", "https://example.test/notice-2"] },
  ])("$source_id $urls 不用一条链接代替整组材料", ({ urls, ...fields }) => {
    const row: SourceCatalogEntry = {
      ...fields, url: "https://example.test/group-first-link",
      documents: urls.map((url, index) => ({
        title: `材料第${index + 1}篇`, data_date: `2026-10-0${index + 1} 12:30`, url,
      })),
    };
    const { container } = render(<SourceCatalogTable rows={[row]} />);
    const md = buildStockReportMarkdown(baseReport({ source_catalog: [row] }));
    const exported = document.createElement("div");
    exported.innerHTML = markdownToHtml(md);
    for (const doc of row.documents!) {
      for (const root of [container, exported]) {
        expect(root.textContent).toContain(doc.title);
        expect(root.textContent).toContain(doc.data_date);
        expect(root.textContent).toContain(fields.quality);
        if (doc.url) expect([...root.querySelectorAll("a")].some((a) => a.getAttribute("href") === doc.url)).toBe(true);
        else expect(root.textContent).toContain("（未提供链接）");
      }
      expect(md).toContain(doc.title);
      expect(md).toContain(doc.data_date);
      if (doc.url) expect(md).toContain(`](${doc.url})`);
    }
    for (const root of [container, exported]) {
      expect([...root.querySelectorAll("a")].some((a) => a.getAttribute("href") === row.url)).toBe(false);
    }
    expect(md).not.toContain(row.url);
  });
});

function claim(overrides: Partial<ClaimFinding> = {}): ClaimFinding {
  return {
    claim_id: "C1",
    text: "",
    sources: [],
    dropped_sources: [],
    requires: [],
    unknown_requires: [],
    missing_coverage: [],
    evidence_quality: "B",
    status: "supported",
    note: "",
    importance: "high",
    support: "full",
    ...overrides,
  } as ClaimFinding;
}

/** 取一段 HTML 里全部 <mark> 的文本（按出现顺序）。 */
function marksOfHtml(html: string): string[] {
  const container = document.createElement("div");
  container.innerHTML = html;
  return [...container.querySelectorAll("mark")].map((el) => el.textContent ?? "");
}

/** 取屏显 renderInline 产出的全部 <mark> 文本（按出现顺序）。 */
function marksOfScreen(text: string, highlights: ReportHighlight[]): string[] {
  const { container } = render(<div>{renderInline(text, "k", highlights)}</div>);
  return [...container.querySelectorAll("mark")].map((el) => el.textContent ?? "");
}

describe("R1 关键数字高亮（候选取值收窄 + 屏显/导出同源）", () => {
  it("相同数字按各自判断显示状态，不跨段沿用第一条判断的颜色", () => {
    const full = "营业收入 19.7 亿元已核对";
    const partial = "经营现金流 19.7 亿元待核对";
    const report = baseReport({ claims: [
      claim({ text: full, importance: "high", support: "full" }),
      claim({ text: partial, importance: "high", support: "partial" }),
    ] });
    const highlights = keyFactHighlights(report);
    for (const [text, tone] of [[full, "blue"], [partial, "amber"], ["其他指标 19.7 无对应判断", null]] as const) {
      const screen = render(<>{renderInline(text, "scope", highlights)}</>);
      const exported = document.createElement("div");
      exported.innerHTML = markdownToHtml(text, highlights);
      for (const root of [screen.container, exported]) {
        const marks = [...root.querySelectorAll("mark")];
        expect(marks).toHaveLength(tone ? 1 : 0);
        if (tone) expect(marks[0]!.classList.contains(`rpt-hl-${tone}`)).toBe(true);
      }
    }
  });

  it("完整数字边界阻止子串、日期及 MA 周期误标", () => {
    const highlights: ReportHighlight[] = [{ text: "19.7", tone: "blue" }];
    const text = "119.7、19.73、MA19.7、2026.19.7 均不强调；PE 19.7 可强调。";
    expect(marksOfScreen(text, highlights)).toEqual(["19.7"]);
    expect(marksOfHtml(markdownToHtml(text, highlights))).toEqual(["19.7"]);
  });

  it("年份不进候选：2026 被排除，19.7 保留", () => {
    const highlights = keyFactHighlights(baseReport({
      conclusion: { statement: "2026 年 PE-TTM 19.7 处于历史低分位。", direction: "中性", core_conflict: "", confidence: "medium" },
    }));
    const texts = highlights.map((item) => item.text);
    expect(texts).not.toContain("2026");
    expect(texts).toContain("19.7");
  });

  it("生成时间行不会被染色（年份与整数时间都不是精确数值）", () => {
    const highlights = keyFactHighlights(baseReport({
      conclusion: { statement: "2026/9/27 16:56:39 生成，PE-TTM 19.7。", direction: "中性", core_conflict: "", confidence: "medium" },
    }));
    expect(highlights.map((item) => item.text)).toEqual(["19.7"]);
  });

  it("两条判断各拿配额：单条最多 3 个候选，后面的判断不再被吃光", () => {
    const highlights = keyFactHighlights(baseReport({
      conclusion: undefined,
      claims: [
        claim({ claim_id: "C1", text: "归母净利润10.35亿元，同比增长约128.5%，营业总收入324.91亿元同比增长约29.8%", support: "full" }),
        claim({ claim_id: "C2", text: "PE(TTM) 11.67，分位 66.2%，PB 1.94，覆盖倍数 1.9%", support: "partial" }),
      ],
    }));
    expect(highlights.filter((item) => item.tone === "blue").map((item) => item.text))
      .toEqual(["10.35", "128.5%", "324.91"]);
    // 旧实现只有「全篇 8 个候选」的总量上限，第二条判断只抢到 1 格即被截断。
    expect(highlights.filter((item) => item.tone === "amber").map((item) => item.text))
      .toEqual(["11.67", "66.2%", "1.94"]);
  });

  it("屏显与导出的 <mark> 序列逐字相同（此前屏显走子串、导出走全等）", () => {
    const highlights: ReportHighlight[] = [
      { text: "10.35", tone: "blue", title: "结论数字" },
      { text: "128.5%", tone: "blue", title: "结论数字" },
      { text: "66.2%", tone: "amber", title: "待核验数字" },
    ];
    const first = "2026H1归母净利润10.35亿元，同比增长约128.5%。";
    const second = "估值分位 66.2%，PE(TTM) 11.67。";
    expect(marksOfHtml(markdownToHtml(`- ${first}`, highlights))).toEqual(marksOfScreen(first, highlights));
    expect(marksOfHtml(markdownToHtml(`- ${second}`, highlights))).toEqual(marksOfScreen(second, highlights));
    // 段内预算 2 处，两侧一致地只取最靠前的两个。
    expect(marksOfScreen(first, highlights)).toEqual(["10.35", "128.5%"]);
    expect(marksOfScreen(second, highlights)).toEqual(["66.2%"]);
  });
});

describe("R2 来源目录链接列", () => {
  it("S3A 正文来源目录缺项时明确提示，其他大小写原样保留", () => {
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml("正文 [S3A]，其他文本 [s3a]、[S3a]。");
    expect(root.querySelector("span.cite")?.textContent).toBe("[S3A]（来源目录缺项）");
    expect(root.querySelector("a.cite")).toBeNull();
    expect(root.textContent).toContain("[s3a]、[S3a]");
    expect(root.querySelectorAll("span.cite")).toHaveLength(1);
  });

  it("S3A 无外链时仍有原生引用与返回入口，目标可聚焦且编号唯一", () => {
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml(buildStockReportMarkdown(baseReport({
      report: "### 技术面\n正文一次 [S3A]，再次 [S3A]。",
      citations: ["S3A"],
      source_catalog: [{ source_id: "S3A", name: "公告正文", url: "", quality: "A" }],
    })));
    const refs = [...root.querySelectorAll<HTMLAnchorElement>("a.cite")];
    expect(refs.length).toBeGreaterThanOrEqual(2);
    for (const ref of refs) {
      expect(ref.getAttribute("aria-label")).toBe("查看来源 S3A");
      const target = root.querySelector(ref.hash)!;
      expect(target.getAttribute("tabindex")).toBe("-1");
      expect(target.querySelector(`a[href="#${ref.id}"]`)).not.toBeNull();
    }
    const ids = [...root.querySelectorAll("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(root.querySelector('a[href^="http"]')).toBeNull();
  });

  const rows: SourceCatalogEntry[] = [
    { source_id: "S3", name: "近期公告", url: "https://finance.eastmoney.com/a/2026092712345678.html", data_date: "2026-09-27", retrieved_at: "2026-09-27T08:55:56.785097+00:00", quality: "B", cited: true, used_by: ["C3"] },
    { source_id: "S5", name: "估值与历史分位", url: "", url_note: "来源未提供可访问链接", data_date: "2026-09-26", quality: "C", cited: true, used_by: ["C6"] },
  ];

  it("有真实链接的条目渲染为可点击 <a>，且带 rel=noopener noreferrer", () => {
    const html = markdownToHtml(buildStockReportMarkdown(baseReport({ source_catalog: rows })));
    const container = document.createElement("div");
    container.innerHTML = html;
    // 只取来源目录表里的外链——目录（<nav>）里的 # 锚点不算。
    const anchors = [...container.querySelectorAll('table a[href^="https://"]')];
    expect(anchors).toHaveLength(1);
    expect(anchors[0]!.getAttribute("href")).toBe("https://finance.eastmoney.com/a/2026092712345678.html");
    expect(anchors[0]!.getAttribute("rel")).toBe("noopener noreferrer");
    expect(anchors[0]!.getAttribute("target")).toBe("_blank");
  });

  it("无链接条目仍如实标「（未提供链接…）」，不编造 URL", () => {
    const md = buildStockReportMarkdown(baseReport({ source_catalog: rows }));
    expect(md).toContain("（未提供链接：来源未提供可访问链接）");
    // 全篇只有 S3 被写成 Markdown 链接；S5 那行是纯文本「（未提供链接…）」，没被拼出任何地址。
    const links = md.match(/\]\((https?:\/\/[^\s)]+)\)/g) ?? [];
    expect(links).toHaveLength(1);
    expect(links[0]).toBe("](https://finance.eastmoney.com/a/2026092712345678.html)");
  });
});

describe("R3 缩进列表项", () => {
  /** 列表 HTML 由 out.join("\\n") 拼出，断言前先去掉换行（只看标签结构）。 */
  const flat = (html: string): string => html.replace(/\n/g, "");

  it("渲染为嵌套 <ul>（嵌套落在父 <li> 内），不再留下裸的 `  - `", () => {
    const html = markdownToHtml(["- 顶层项", "  - 子项", "- 第二个顶层项"].join("\n"));
    expect(flat(html)).toBe("<ul><li>顶层项<ul><li>子项</li></ul><li>第二个顶层项</li></ul>");
    expect(html).not.toContain("<p>  - ");
    expect(html).not.toContain("  - ");
  });

  it("报告里的「计入统计的核心判断」与「结论成立的必要条件」不再退化成段落", () => {
    const md = buildStockReportMarkdown(baseReport({
      claim_findings: {
        claims: [claim({ claim_id: "C1", text: "判断一", support: "full" }), claim({ claim_id: "C2", text: "判断二", support: "partial" })],
        support_counts: { full: 1, partial: 1, none: 0 },
      } as never,
      counter_check: {
        ok: true, verdict_robustness: "mixed", strongest_support: "s", strongest_counter: "c",
        necessary_conditions: ["条件一", "条件二"],
      } as never,
    }));
    const html = markdownToHtml(md.replace(/^# .*$/m, "").trim());
    expect(html).not.toContain("<p>  - ");
    expect(flat(html)).toContain("<li>计入统计的核心判断：C1、C2");
    expect(flat(html)).toContain("<ul><li>条件一</li><li>条件二</li></ul>");
  });
});

describe("R4 标题层级与文档目录", () => {
  it("`##` → h2、`###` → h3、`####` → h4，不再一律 <h3>", () => {
    const html = markdownToHtml("## 研报正文\n\n### 技术面\n\n#### 催化证据链");
    expect(html).toContain("<h2 ");
    expect(html).toContain(">研报正文</h2>");
    expect(html).toContain(">技术面</h3>");
    expect(html).toContain(">催化证据链</h4>");
  });

  it("按 `##` 生成目录，且「研报正文」是「技术面」的父级", () => {
    const html = markdownToHtml("## 研报正文\n\n### 技术面\n\n## 估值判断");
    expect(html).toContain('<nav class="doc-toc">');
    expect(html).toContain('<a href="#wb-doc-sec-1">研报正文</a>');
    expect(html).toContain('<a href="#wb-doc-sec-3">估值判断</a>');
    // ### 不进目录（只收二级标题）。
    expect(html).not.toContain(">技术面</a>");
    expect(html.indexOf(">研报正文</h2>")).toBeLessThan(html.indexOf(">技术面</h3>"));
  });

  it("buildHtmlDocument 定义了 h2–h5 样式与打印分页规则（D2）", () => {
    const doc = buildHtmlDocument("t", ["m"], "<p>x</p>");
    expect(doc).toContain("h2 { font-size: 19px;");
    expect(doc).toContain("h5 { font-size: 13px;");
    expect(doc).toContain("@media print");
    expect(doc).toContain("thead { display: table-header-group; }");
    expect(doc).toContain("tr { page-break-inside: avoid; }");
    expect(doc).toContain("h1, h2, h3, h4 { page-break-after: avoid; }");
  });
});

describe("R5 免责声明只出现一次", () => {
  it("Markdown 源里的引用行渲染成 <blockquote>，不再被转义成 &gt;", () => {
    const html = markdownToHtml("## 标题\n\n> 研究参考，不构成投资建议；关键判断请回链原始来源复核。");
    expect(html).toContain("<blockquote>研究参考，不构成投资建议；关键判断请回链原始来源复核。</blockquote>");
    expect(html).not.toContain("&gt;");
  });

  it("完整导出件里「不构成投资建议」恰好 1 处", () => {
    const md = buildStockReportMarkdown(baseReport());
    const html = buildHtmlDocument("t", ["m"], markdownToHtml(md.replace(/^# .*$/m, "").trim()));
    expect(md.match(/不构成投资建议/g)).toHaveLength(1);
    expect(html.match(/不构成投资建议/g)).toHaveLength(1);
  });

  it("连续引用行合并为一个 blockquote", () => {
    const html = markdownToHtml("> 第一行\n> 第二行");
    expect(html).toBe("<blockquote>第一行<br>第二行</blockquote>");
  });
});

describe("R6 核心判断表编号列", () => {
  it("导出表有编号列，C 编号可在表内定位", () => {
    const md = buildStockReportMarkdown(baseReport({
      claim_findings: {
        claims: [claim({ claim_id: "C3", text: "判断三", support: "full" })],
        support_counts: { full: 1, partial: 0, none: 0 },
      } as never,
    }));
    expect(md).toContain("| 编号 | 核心判断 | 引用 |");
    expect(md).toMatch(/\| C3 \| 判断三 \|/);
  });
});

describe("R7 时间格式统一", () => {
  it("UTC 微秒串按本地时区、秒级展示", () => {
    const formatted = formatTimestamp("2026-09-27T08:55:56.785097+00:00");
    expect(formatted).toMatch(/（UTC[+-]\d{2}:\d{2}）$/);
    expect(formatted).not.toContain(".785097");
    expect(formatted).toContain(new Date("2026-09-27T08:55:56.785097+00:00").toLocaleString());
  });

  it("同一时刻的不同来源偏移不会产生不同本地时间，导出保留时区标识", () => {
    const utc = "2026-09-27T08:55:56Z";
    const offset = "2026-09-27T16:55:56+08:00";
    expect(formatTimestamp(utc)).toBe(formatTimestamp(offset));
    const md = buildStockReportMarkdown(baseReport({ generated_at: utc }));
    expect(md).toContain(`- 生成时间：${formatTimestamp(utc)}`);
    expect(md).toMatch(/生成时间：[^\n]+（UTC[+-]\d{2}:\d{2}）/);
  });

  it("空值与无法解析的值如实降级，不编造", () => {
    expect(formatTimestamp("")).toBe("未标注");
    expect(formatTimestamp(undefined)).toBe("未标注");
    expect(formatTimestamp("2026年三季报披露后")).toBe("2026年三季报披露后");
  });

  it("来源目录的获取时间不再原样打印 ISO 串", () => {
    const md = buildStockReportMarkdown(baseReport({
      source_catalog: [{
        source_id: "S3", name: "公告", url: "", url_note: "无",
        data_date: "2026-09-27", retrieved_at: "2026-09-27T08:55:56.785097+00:00",
      }],
    }));
    expect(md).not.toContain("2026-09-27T08:55:56.785097+00:00");
    expect(md).toContain(new Date("2026-09-27T08:55:56.785097+00:00").toLocaleString());
  });
});
