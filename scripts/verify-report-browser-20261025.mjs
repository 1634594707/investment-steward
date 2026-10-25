import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Reuse the browser driver already used by desktop-acceptance.mjs.
const require = createRequire("C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js");
const { chromium } = require("playwright-core");
const input = resolve("docs/assets/history-audit-2026-10-25/original-report-reexport.html");
const out = resolve("docs/assets/history-audit-2026-10-25/browser");
mkdirSync(out, { recursive: true });
const results = [];
const browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
try {
  const context = await browser.newContext({ offline: true });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(pathToFileURL(input).href);
    await page.evaluate(() => document.fonts.ready);
    const layout = await page.evaluate(() => ({
      viewport: innerWidth,
      documentWidth: document.documentElement.scrollWidth,
      text: document.body.innerText,
      sourceDates: [...document.querySelectorAll(".doc-source-catalog tbody td:nth-child(4), .doc-source-catalog tbody td:nth-child(5)")].map(el => {
        const range = document.createRange();
        range.selectNodeContents(el);
        return { text: el.textContent, lines: new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size };
      }),
      tables: [...document.querySelectorAll(".doc-table-scroll")].map(el => ({
        client: el.clientWidth, scroll: el.scrollWidth,
        tabIndex: el.tabIndex, overflow: getComputedStyle(el).overflowX,
      })),
    }));
    await page.screenshot({ path: resolve(out, `report-${width}.png`), fullPage: true });
    await page.locator(".doc-source-catalog").screenshot({ path: resolve(out, `source-catalog-${width}.png`) });
    const regions = page.locator(".doc-table-scroll");
    let scrollable = 0;
    for (let i = 0; i < layout.tables.length; i++) {
      const table = layout.tables[i];
      if (table.scroll > table.client) {
        await regions.nth(i).focus();
        for (let step = 0; step < 5; step++) await page.keyboard.press("ArrowRight");
        await page.waitForTimeout(250);
        if (await regions.nth(i).evaluate(el => el.scrollLeft > 0)) scrollable++;
      }
    }
    const wideTables = layout.tables.filter(t => t.scroll > t.client).length;
    results.push({ width, documentWidth: layout.documentWidth, bodyFits: layout.documentWidth <= width,
      tableCount: layout.tables.length, wideTables, keyboardScrollable: scrollable,
      sourceDates: layout.sourceDates, sourceDatesReadable: layout.sourceDates.length === 10 && layout.sourceDates.every(cell => cell.lines === 1),
      tableKeyboardPass: scrollable === wideTables, textSha256: createHash("sha256").update(layout.text).digest("hex") });
  }
  const refs = page.locator("a.cite");
  const citations = [];
  for (let i = 0; i < await refs.count(); i++) {
    const ref = refs.nth(i);
    const href = await ref.getAttribute("href");
    const id = await ref.getAttribute("id");
    await ref.click();
    const arrived = new URL(page.url()).hash === href;
    const source = page.locator(href);
    const back = source.locator(`a[href="#${id}"]`);
    await back.click();
    citations.push({ id, href, arrived, returned: new URL(page.url()).hash === `#${id}` });
  }
  const firstRef = refs.first();
  await firstRef.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  const focusVisible = await firstRef.evaluate(el => getComputedStyle(el).outlineStyle !== "none");
  const href = await firstRef.getAttribute("href");
  await page.keyboard.press("Enter");
  const keyboardJump = new URL(page.url()).hash === href;
  const toc = page.locator(".doc-toc a");
  let tocPassed = 0;
  for (let i = 0; i < await toc.count(); i++) {
    const item = toc.nth(i);
    const target = await item.getAttribute("href");
    await item.click();
    if (new URL(page.url()).hash === target && await page.locator(target).count() === 1) tocPassed++;
  }
  const report = { browser: browser.version(), mode: "offline file URL; real archived report", input,
    inputSha256: createHash("sha256").update(readFileSync(input)).digest("hex"), results, citations,
    focusVisible, keyboardJump, tocCount: await toc.count(), tocPassed, errors,
    printing: "not verified", visualInspection: "screenshots pending inspection" };
  writeFileSync(resolve(out, "result.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ ...report, citations: `${citations.length} checked` }, null, 2));
  if (errors.length || results.some(r => !r.bodyFits || !r.tableKeyboardPass || !r.sourceDatesReadable) || citations.some(r => !r.arrived || !r.returned) || !focusVisible || !keyboardJump || tocPassed !== await toc.count()) process.exitCode = 1;
} finally {
  await browser.close();
}
