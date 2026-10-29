/** R05: committed catalog snapshots from controlled Core HTTP responses, with synthetic report metadata. */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render } from "@testing-library/react";
import { expect, it } from "vitest";
import { SourceCatalogTable } from "./panels";
import { buildStockReportHtml, buildStockReportMarkdown } from "./export";
import type { SourceCatalogEntry, StockReport } from "./researchTypes";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "fixtures");
it.each(["stock-news-url", "stock-missing-news-url", "collab-news-url", "collab-missing-news-url"])(
  "%s: titles stay title-only and successful bodies retain their own identity across delivery", (name) => {
    const fixture = JSON.parse(readFileSync(resolve(root, `${name}.json`), "utf8")) as {
      report: StockReport; immediate_catalog: SourceCatalogEntry[];
    };
    const report = fixture.report;
    expect(report.source_catalog).toEqual(fixture.immediate_catalog);
    const rows = report.source_catalog!;
    const byId = new Map(rows.map(row => [row.source_id, row]));
    expect(byId.get("S2")!.quality).toBe("C");
    expect(byId.get("S3")!.quality).toBe("B");
    expect(byId.get("S3A")!.quality).toBe("A");
    expect(byId.get("S3")!.documents).toHaveLength(3);
    expect(byId.get("S3A")!.documents).toHaveLength(2);
    expect(byId.get("S3A")!.documents!.map(doc => doc.url)).toEqual([
      "https://example.test/notice-2", "https://example.test/body-three.pdf",
    ]);
    const page = render(<SourceCatalogTable rows={rows} />);
    const markdown = buildStockReportMarkdown(report);
    const html = buildStockReportHtml(report);
    const exported = new DOMParser().parseFromString(html, "text/html");
    for (const row of rows) {
      for (const text of [row.name, row.scope_note]) {
        if (!text) continue;
        expect(page.container.textContent).toContain(text);
        expect(exported.body.textContent).toContain(text);
        expect(markdown).toContain(text);
      }
      for (const doc of row.documents ?? []) {
        for (const scope of [page.container, exported.body]) {
          expect(scope.textContent).toContain(doc.title);
          expect(scope.textContent).toContain(doc.data_date || "日期未标注");
          if (doc.url) expect([...scope.querySelectorAll("a")].some(a => a.getAttribute("href") === doc.url)).toBe(true);
          else expect(scope.textContent).toContain("（未提供链接）");
        }
      }
    }
    expect(page.container.textContent).toContain("未保留该编号的来源文本");
    expect(exported.body.textContent).toContain("未保留该编号的来源文本");
  },
);
