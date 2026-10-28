// Full application entry + actual Core HTTP, with isolated synthetic SQLite data.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { mkdirSync, writeFileSync } from "node:fs";
import assert from "node:assert/strict";

const web = resolve("apps/web-shell");
const requireWeb = createRequire(resolve(web, "package.json"));
const { createServer } = await import(pathToFileURL(requireWeb.resolve("vite")).href);
const requireBrowser = createRequire("C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js");
const { chromium, _electron: electron } = requireBrowser("playwright-core");
const desktopMode = process.argv.includes("--electron");
const out = resolve(`docs/assets/settings-${desktopMode ? "electron" : "app-http"}-20261027`);
mkdirSync(out, { recursive: true });
writeFileSync(resolve(out, "results.json"), JSON.stringify({ status: "running" }));
const fixture = `
import json, socket, sys, threading, tempfile, sqlite3
from datetime import datetime, UTC, timedelta
from pathlib import Path
import uvicorn
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core import collab
from investment_steward_core.domain import ModelProfile
from uuid import uuid4
with tempfile.TemporaryDirectory(prefix="steward-settings-browser-") as folder:
 app = create_app(CoreSettings(data_dir=Path(folder), session_token="synthetic-browser", model_access_enabled=False))
 db = app.state.core.database
 for model in ["synthetic-recent", "synthetic-20-days-old"]:
  db.record_model_call(profile_id=None, model=model, purpose="研报", prompt_tokens=None,
   completion_tokens=None, total_tokens=None, latency_ms=100, retried=False,
   outcome="error", error_kind="protocol", http_status=200, attempt_count=1,
   returned_model="synthetic-reported", usage_complete=False)
 with sqlite3.connect(db.path) as conn:
  conn.execute("UPDATE model_calls SET created_at=? WHERE model=?", ((datetime.now(UTC)-timedelta(days=20)).isoformat(),"synthetic-20-days-old"))
 conn.close()
 profile=ModelProfile(profile_id=uuid4(),name="synthetic-recovery",base_url="https://example.invalid",model="synthetic",credential_ref="unused",status="使用中")
 db.upsert_model_profile(profile)
 run=collab.create_run("600001","synthetic recovery question","synthetic tech",{}, {},str(profile.profile_id),profile.model,
  profile_fingerprints={stage:collab.profile_fingerprint(profile) for stage in collab.STAGES})
 run["next_index"]=1
 run["stages"][0].update(status="done",result={"observations":["synthetic completed check"]})
 run["stages"][1]["status"]="running"
 db.save_collab_run(run)
 collab._runs.clear()
 sock=socket.socket(); sock.bind(("127.0.0.1",0))
 server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",log_level="error"))
 def stop():
  sys.stdin.readline(); server.should_exit=True
 threading.Thread(target=stop,daemon=True).start()
 print(json.dumps({"port":sock.getsockname()[1]}),flush=True)
 try: server.run(sockets=[sock])
 finally: db.close()
`;
const core = spawn(resolve(".venv/Scripts/python.exe"), ["-u", "-c", fixture], { windowsHide: true });
let stderr = "";
core.stderr.on("data", data => { stderr += String(data); });
const closed = new Promise(resolve => core.once("close", code => resolve(code)));
let server, browser, page, desktop;
const errors = [], requests = [], results = [], desktopLogs = [];
try {
  const port = await new Promise((accept, reject) => {
    const timer = setTimeout(() => reject(new Error(`Core startup timed out: ${stderr}`)), 15000);
    core.once("error", error => { clearTimeout(timer); reject(error); });
    core.once("exit", code => { clearTimeout(timer); reject(new Error(`Core exited ${code}: ${stderr}`)); });
    let buffer = "";
    core.stdout.on("data", data => {
      buffer += String(data);
      for (const line of buffer.split("\n")) if (line.startsWith('{"port"')) { clearTimeout(timer); accept(JSON.parse(line).port); }
    });
  });
  server = await createServer({ root: web, configFile: resolve(web, "vite.config.ts"),
    define: { "import.meta.env.VITE_CORE_BASE": JSON.stringify("/api"),
      "import.meta.env.VITE_CORE_TOKEN": JSON.stringify("synthetic-browser") },
    server: { host: "127.0.0.1", port: 0, strictPort: false,
      proxy: { "/api": { target: `http://127.0.0.1:${port}`, rewrite: path => path.replace(/^\/api/, "") } } } });
  await server.listen();
  async function launchDesktop() {
    const host = resolve("apps/desktop-host");
    const hostRequire = createRequire(resolve(host, "package.json"));
    desktop = await electron.launch({ executablePath: hostRequire("electron"),
      args: [host, `--user-data-dir=${resolve(out, "electron-profile")}`],
      env: { ...process.env, STEWARD_WEB_URL: server.resolvedUrls.local[0] } });
    desktop.process().stderr?.on("data", data => desktopLogs.push(String(data)));
    const userData = await desktop.evaluate(({ app, BrowserWindow }) => {
      for (const window of BrowserWindow.getAllWindows()) {
        window.webContents.setBackgroundThrottling(false);
        window.showInactive();
      }
      return app.getPath("userData");
    });
    assert.equal(resolve(userData), resolve(out, "electron-profile"), "Electron data must stay isolated");
    const window = await desktop.firstWindow();
    await window.getByRole("button", { name: "切换到工作区：系统设置", exact: true }).waitFor();
    return window;
  }
  if (desktopMode) page = await launchDesktop();
  else {
    browser = await chromium.launch({ executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
    page = await browser.newPage();
  }
  page.on("pageerror", error => errors.push(String(error)));
  page.on("response", response => { if (response.url().includes("/api/model-usage")) requests.push({ url: response.url(), status: response.status() }); });
  await page.route("**/*", route => {
    const url = new URL(route.request().url());
    return ["127.0.0.1", "localhost"].includes(url.hostname) ? route.continue() : route.abort();
  });
  await page.addInitScript(() => { localStorage.setItem("steward.onboarding-deferred-at", String(Date.now())); });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(server.resolvedUrls.local[0]);
  await page.getByRole("button", { name: "切换到工作区：系统设置", exact: true }).click();
  await page.getByRole("button", { name: "模型配置", exact: true }).click();
  await page.getByText("synthetic-recent", { exact: true }).first().waitFor();
  assert.equal(await page.getByText("synthetic-20-days-old", { exact: true }).count(), 0);
  await page.getByRole("button", { name: "最近 30 天", exact: true }).click();
  await page.getByText("synthetic-20-days-old", { exact: true }).first().waitFor();
  await page.getByText("最近调用诊断（最多 20 条）", { exact: true }).click();
  await page.getByText("synthetic-reported", { exact: true }).first().waitFor();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const region = page.getByRole("region", { name: "最近模型调用", exact: true });
    await region.scrollIntoViewIfNeeded();
    await region.focus();
    if (width === 390) {
      await region.evaluate(el => { el.scrollLeft = el.scrollWidth; });
      assert.ok(await region.evaluate(el => el.scrollLeft > 0));
    }
    await page.evaluate(() => document.fonts.ready);
    const layout = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth }));
    await page.screenshot({ path: resolve(out, `${width}.png`), fullPage: true });
    results.push(layout);
  }
  await page.getByRole("button", { name: "最近 7 天", exact: true }).click();
  await page.getByText("synthetic-recent", { exact: true }).first().waitFor();
  assert.equal(await page.getByText("synthetic-20-days-old", { exact: true }).count(), 0);
  assert.ok(requests.some(row => row.url.includes("window=30d") && row.status === 200));
  assert.deepEqual(errors, []);
  assert.ok(results.every(row => row.width <= row.viewport + 1), "AppShell document overflow");
  if (desktopMode) {
    await desktop.close(); desktop = null;
    page = await launchDesktop();
    await page.getByRole("button", { name: "模型配置", exact: true }).click();
    await page.getByText("synthetic-recent", { exact: true }).first().waitFor();
    await page.getByRole("button", { name: "切换到工作区：投资管家", exact: true }).click();
    await page.getByRole("button", { name: "研究工作台", exact: true }).click();
    await page.getByRole("button", { name: "查看可恢复任务", exact: true }).click();
    await page.getByRole("button", { name: "恢复查看", exact: true }).click();
    const observation = page.getByText("synthetic completed check", { exact: true });
    await observation.locator("xpath=ancestor::details[1]").locator("summary").first().click();
    await observation.waitFor();
    await page.getByRole("button", { name: "继续执行（含失败阶段重试）", exact: true }).waitFor();
    const headers = { "X-Core-Session-Token": "synthetic-browser" };
    const saved = await (await fetch(`http://127.0.0.1:${port}/evidence/collab-state`, { headers })).json();
    assert.equal(saved.items[0].next_index, 1);
    assert.equal(saved.items[0].recovery_state, "failed");
    const usage = await (await fetch(`http://127.0.0.1:${port}/model-usage?window=30d`, { headers })).json();
    assert.equal(usage.recent_calls.length, 2, "viewing recovery must not call a model");
    await page.screenshot({ path: resolve(out, "recovered-task.png"), fullPage: true });
  }
} catch (error) {
  console.error(JSON.stringify({ error: String(error), errors, desktopLogs }));
  writeFileSync(resolve(out, "results.json"), JSON.stringify({ status: "failed", error: String(error), errors }, null, 2));
  throw error;
} finally {
  await browser?.close();
  await desktop?.close();
  await server?.close();
  core.stdin.end("stop\n");
  const code = await Promise.race([closed, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error("Core shutdown timed out")), 8000);
    timer.unref();
  })]);
  assert.equal(code, 0, stderr);
}
writeFileSync(resolve(out, "results.json"), JSON.stringify({ mode: desktopMode ? "electron_dev_real_core_synthetic_db" : "full_app_real_core_synthetic_db",
  status: "passed", recoveryReadAfterElectronRelaunch: desktopMode,
  electronRelaunch: desktopMode,
  coreShutdown: "clean", requests, results, errors }, null, 2));
console.log(JSON.stringify({ status: "passed", requests: requests.length, coreShutdown: "clean" }));
