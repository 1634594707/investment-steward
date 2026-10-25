/** Re-export the user-provided historical report using the application's actual export functions. */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";
import { buildStockReportHtml, buildStockReportMarkdown } from "./export";
import { formatTimestamp } from "./format";
import type { StockReport } from "./researchTypes";

it("原始研报重导出保留历史正文及来源缺失，引用回到真实目录", () => {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../docs/assets/history-audit-2026-10-25");
  const baselineBytes = readFileSync(resolve(root, "original-report.json"));
  const viewBytes = readFileSync(resolve(root, "original-report-view.json"));
  const baseline = JSON.parse(baselineBytes.toString("utf8")) as StockReport;
  const report = JSON.parse(viewBytes.toString("utf8")) as StockReport;
  expect(report.report_id).toBe("bea494cc-5e3f-44e4-9f8d-5b8156c85a30");
  expect(report.report).toBe(baseline.report);
  expect(report.generated_at).toBe(baseline.generated_at);
  expect(report.source_catalog?.map((row) => row.source_id)).toEqual(["S1", "S2", "S3", "S4", "S5"]);
  for (const row of report.source_catalog!) {
    expect(row.url).toBe("");
    expect(row.documents).toEqual([]);
    expect(row.url_note).toContain("未保留");
  }
  const markdown = buildStockReportMarkdown(report);
  const html = buildStockReportHtml(report);
  const parsed = new DOMParser().parseFromString(html, "text/html");
  expect(parsed.querySelector('meta[name="viewport"]')?.getAttribute("content")).toContain("width=device-width");
  expect(parsed.title).toBe(report.title);
  expect(parsed.querySelector(".meta")).toBeNull();
  for (const line of [`标的：${report.symbol}`, `模型：${report.model}`, `置信度：${report.confidence}`, `生成时间：${formatTimestamp(report.generated_at)}`]) {
    expect([...parsed.querySelectorAll("li")].filter(node => node.textContent?.trim() === line)).toHaveLength(1);
  }
  expect(parsed.body.textContent).toContain("未提供链接");
  expect(report.cash_flow_quality?.verdict).toBe("不足以判断盈利质量");
  expect(report.cash_flow_quality?.free_cash_flow).toBeNull();
  expect(report.cash_flow_quality?.free_cash_flow_reported).toBe(-2.63);
  expect(report.research_completeness?.done_count).toBe(1);
  expect(report.research_completeness?.items?.find(row => row.key === "cash_flow_and_capex")?.covered).not.toContain("自由现金流");
  for (const text of [markdown, parsed.body.textContent!]) {
    expect(text).toContain("研究完成度：1/4");
    expect(text).toContain("生成时告警（历史留痕，不代表当前核验）");
    expect(text).toContain("判定：不足以判断盈利质量");
    expect(text).toContain("模型提供的自由现金流 -2.63（原值留痕，不代表核验通过）");
    expect(text).not.toContain("判定：盈利质量偏弱");
  }
  expect(parsed.querySelectorAll('a[href^="https://"], a[href^="http://"]')).toHaveLength(0);
  const citations = [...parsed.querySelectorAll<HTMLAnchorElement>("a.cite")];
  expect(citations.length).toBeGreaterThan(0);
  for (const citation of citations) expect(parsed.getElementById(citation.hash.slice(1))).not.toBeNull();
  expect(readFileSync(resolve(root, "original-report.json"))).toEqual(baselineBytes);
  // Acceptance artifacts are written only after every assertion above succeeds.
  const files = { "original-report-reexport.md": markdown, "original-report-reexport.html": html };
  for (const [name, content] of Object.entries(files)) writeFileSync(resolve(root, name), content, "utf8");
  const sha256 = (content: string | Uint8Array) => createHash("sha256").update(content).digest("hex");
  writeFileSync(resolve(root, "reexport-evidence.json"), JSON.stringify({
    report_id: report.report_id, generated_at: report.generated_at,
    baseline_sha256: sha256(baselineBytes), view_sha256: sha256(viewBytes),
    outputs: Object.fromEntries(Object.entries(files).map(([name, content]) => [name, sha256(content)])),
    source_rows: report.source_catalog!.length, source_urls: 0, citation_links: citations.length,
    scope: "DOM assertions and actual export artifacts; viewport rendering and printing not verified",
  }, null, 2) + "\n", "utf8");
});
