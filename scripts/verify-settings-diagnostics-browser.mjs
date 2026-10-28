// Production diagnostic component and CSS, synthetic transport, isolated Vite/Edge.
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";

const web = resolve("apps/web-shell");
const localRequire = createRequire(resolve(web, "package.json"));
const { createServer } = await import(pathToFileURL(localRequire.resolve("vite")).href);
const browserRequire = createRequire("C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js");
const { chromium } = browserRequire("playwright-core");
const out = resolve("docs/assets/settings-diagnostics-browser-20261027");
mkdirSync(out, { recursive: true });
const model = "synthetic-requested-model-with-a-long-name";
const fixture = { summaries: [{ profile_id: "synthetic", model, purpose: "stock_report", calls: 2,
  retried_calls: 1, ok_calls: 0, errors: 1, timeouts: 1, prompt_tokens: 20, completion_tokens: 10,
  total_tokens: 30, usage_missing: 1, avg_latency_ms: 1000, max_latency_ms: 1500 }], recent_calls: [{
  call_id: "synthetic", model, returned_model: "synthetic-reported-model", purpose: "stock_report",
  created_at: "2026-10-27T01:00:00Z", attempt_count: 2, outcome: "error", error_kind: "protocol",
  http_status: 200, latency_ms: 1000, usage_complete: 0,
  attempts: [{ index: 1, outcome: "error", error_kind: "reasoning_budget", http_status: null,
    latency_ms: 100, total_tokens: 30, max_tokens: 512, thinking_disabled: false }],
}] };
const entry = `import React from '/node_modules/.vite/deps/react.js';
import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import {ModelUsageSection} from '/src/routes/SettingsPage.tsx';
import '/src/styles.css'; import '/src/fonts/fonts.css'; import '/src/routes/settings.css';
window.acceptanceRequests=[]; window.acceptanceMode='ok';
const coreRequest=async req=>{window.acceptanceRequests.push(req);
 if(window.acceptanceMode==='fail') throw new Error('合成断网：无法刷新');
 return {status:200,data:${JSON.stringify(fixture)}};};
document.getElementById('boot-shell')?.remove();
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement('main',
 {className:'settings-page',style:{padding:16}},React.createElement(ModelUsageSection,{coreRequest})));`;
const server = await createServer({ root: web, configFile: resolve(web, "vite.config.ts"),
  server: { host: "127.0.0.1", port: 0, strictPort: false } });
let browser, page;
const results = [], errors = [];
try {
  await server.listen();
  browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
  page = await browser.newPage();
  page.on("pageerror", error => errors.push(String(error)));
  await page.route("**/src/main.tsx", route => route.fulfill({ contentType: "text/javascript", body: entry }));
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(server.resolvedUrls.local[0]);
    await page.getByText(model, { exact: true }).first().waitFor();
    const diagnostics = page.getByText("最近调用诊断（最多 20 条）", { exact: true });
    await diagnostics.focus();
    await page.keyboard.press("Enter");
    await page.getByRole("region", { name: "最近模型调用" }).waitFor();
    await page.getByText("请求明细", { exact: true }).focus();
    await page.keyboard.press("Enter");
    assert.match(await page.locator("body").innerText(), /已知总 token 30/);
    await page.evaluate(() => document.fonts.ready);
    for (const name of ["模型用量汇总", "最近模型调用"]) {
      const region = page.getByRole("region", { name, exact: true });
      if (width === 390) {
        await region.focus();
        await page.keyboard.press("End");
        await region.evaluate(el => { el.scrollLeft = el.scrollWidth; });
        assert.ok(await region.evaluate(el => el.scrollLeft > 0), `${name} must scroll`);
        await region.evaluate(el => { el.scrollLeft = 0; });
      }
    }
    const layout = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth }));
    await page.screenshot({ path: resolve(out, `${width}.png`), fullPage: true });
    results.push({ width, layout, noDocumentOverflow: layout.width <= layout.viewport + 1 });
    await page.evaluate(() => { window.acceptanceMode = "fail"; });
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").innerText(), /合成断网/);
    assert.equal(await page.getByText(model, { exact: true }).count(), 2);
    await page.evaluate(() => { window.acceptanceMode = "ok"; });
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await page.getByRole("alert").waitFor({ state: "detached" });
  }
  assert.deepEqual(errors, []);
  writeFileSync(resolve(out, "results.json"), JSON.stringify({ mode: "production_component_synthetic_transport",
    browser: await browser.version(), results, errors, fullAppShell: false }, null, 2));
  assert.ok(results.every(row => row.noDocumentOverflow), "diagnostics cause document overflow");
  console.log(JSON.stringify({ status: "passed", viewports: results.length }));
} catch (error) {
  console.error(JSON.stringify({ errors, text: await page?.locator('body').innerText() }));
  throw error;
} finally {
  await browser?.close();
  await server.close();
}
