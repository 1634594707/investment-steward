import { describe, expect, it } from "vitest";
import { buildHtmlDocument, markdownToHtml } from "./export";

describe("H04 窄屏导出表格", () => {
  it("来源目录保留完整日期，单独标记九列目录供屏幕展开，其他表格不受影响", () => {
    const catalog = "| 编号 | 名称 | 可用链接 | 数据日期 | 获取时间 | 等级 | 引用状态 | 被哪些判断使用 | 口径限制 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| S1 | 财务 | 未提供链接 | 2026-10-08 | 2026/10/8 20:57:42（UTC+08:00） | C | 已引用 | C1 | 摘要 |";
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml(catalog + "\n\n| 名称 | 数值 |\n| --- | --- |\n| 现金流 | 3.43 |");
    const table = root.querySelector("table.doc-source-catalog");
    expect(table?.querySelector("td:nth-child(4)")?.textContent).toBe("2026-10-08");
    expect(table?.querySelector("td:nth-child(5)")?.textContent).toBe("2026/10/8 20:57:42（UTC+08:00）");
    expect(root.querySelectorAll("table.doc-source-catalog")).toHaveLength(1);
    expect(root.querySelectorAll("table")).toHaveLength(2);
  });

  it("表格有可聚焦的独立滚动区，正文和所有单元格保留", () => {
    const body = markdownToHtml("## 财务\n\n报告说明\n\n| 期间 | 经营现金流 | 资本开支 |\n| --- | --- | --- |\n| 2026H1 | 3.43 亿元 | 6.06 亿元 |");
    const root = document.createElement("div");
    root.innerHTML = body;
    const region = root.querySelector('[role="region"]');
    expect(region?.getAttribute("tabindex")).toBe("0");
    expect(region?.getAttribute("aria-label")).toBe("表格：期间、经营现金流、资本开支");
    expect(region?.querySelectorAll("td")).toHaveLength(3);
    expect(region?.textContent).toContain("6.06 亿元");
    expect(root.querySelector("p")?.textContent).toBe("报告说明");
  });

  it("文档声明实际设备宽度，打印解除表格最小宽度与滚动裁切", () => {
    const root = document.implementation.createHTMLDocument();
    root.documentElement.innerHTML = buildHtmlDocument("研报", [], markdownToHtml("正文"));
    expect(root.querySelector('meta[name="viewport"]')?.getAttribute("content"))
      .toBe("width=device-width, initial-scale=1");
    const css = root.querySelector("style")!.textContent!;
    const print = css.slice(css.indexOf("@media print"));
    expect(print).toContain(".doc-table-scroll { overflow: visible; max-width: none; }");
    expect(print).toContain("min-width: 0; width: 100%; table-layout: fixed;");
    expect(print).toContain("thead { display: table-header-group; }");
    expect(print).not.toContain("blockquote, table { page-break-inside: avoid; }");
  });
});
