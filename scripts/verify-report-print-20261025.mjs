import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire("C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js");
const { chromium } = require("playwright-core");
const input = resolve("docs/assets/history-audit-2026-10-25/original-report-reexport.html");
const out = resolve("docs/assets/history-audit-2026-10-25/print");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
try {
  const context = await browser.newContext({ offline: true });
  const page = await context.newPage();
  await page.goto(pathToFileURL(input).href);
  await page.emulateMedia({ media: "print" });
  await page.evaluate(() => document.fonts.ready);
  const tables = await page.locator(".doc-table-scroll").evaluateAll(elements => elements.map(el => ({
    client: el.clientWidth, scroll: el.scrollWidth, overflow: getComputedStyle(el).overflow,
    tableMinWidth: getComputedStyle(el.querySelector("table")).minWidth,
    headerDisplay: getComputedStyle(el.querySelector("thead")).display,
  })));
  const text = await page.locator("body").textContent();
  writeFileSync(resolve(out, "html-text.txt"), text);
  await page.pdf({ path: resolve(out, "original-report-a4.pdf"), format: "A4", printBackground: true,
    margin: { top: "12mm", bottom: "12mm", left: "0mm", right: "0mm" } });
  writeFileSync(resolve(out, "print-evidence.json"), JSON.stringify({ browser: browser.version(), inputSha256: createHash("sha256").update(readFileSync(input)).digest("hex"),
    paper: "A4; top/bottom 12mm; document CSS supplies horizontal margins", tables,
    scope: "PDF generated; per-page rendering and inspection required" }, null, 2) + "\n");
  console.log("Generated offline A4 print evidence; page inspection pending.");
} finally { await browser.close(); }
