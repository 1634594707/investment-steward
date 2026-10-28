import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { DeepAnalysisBlocks, ResearchCompletenessCard } from "./panels";
import { buildStockReportHtml, buildStockReportMarkdown } from "./export";
import type { StockReport } from "./researchTypes";

afterEach(cleanup);
const fixtures = JSON.parse(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "__fixtures__/cash-flow-delivery.json"), "utf8")) as { report: StockReport }[];

it.each(fixtures)("backend financial decision survives page and both exports: $report.cash_flow_quality.verdict", ({ report }) => {
  const { container } = render(<><DeepAnalysisBlocks report={report} /><ResearchCompletenessCard completeness={report.research_completeness} /></>);
  const markdown = buildStockReportMarkdown(report);
  const html = new DOMParser().parseFromString(buildStockReportHtml(report), "text/html");
  const item = report.research_completeness!.items!.find((row) => row.key === "cash_flow_and_capex")!;
  for (const text of [container.textContent!, markdown, html.body.textContent!]) {
    expect(text).toContain(item.state_label);
    expect(text).toContain("模型提供的自由现金流 40");
    for (const evidence of item.evidence ?? []) expect(text).toContain(evidence);
    if (item.state === "partial") {
      expect(text).toContain("自由现金流 未取到");
      expect(text).toContain("币种：USD");
      expect(text).toContain("原值仅留痕、不计完成度");
    }
  }
  const links = [...html.querySelectorAll<HTMLAnchorElement>("a.cite")];
  expect(links.length).toBeGreaterThan(0);
  for (const link of links) expect(html.getElementById(link.hash.slice(1))).not.toBeNull();
  expect(markdown).toContain(report.report);
  expect(html.body.textContent).toContain(report.report.split(" [S4]")[0]!);
});
