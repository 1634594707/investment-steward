// Source Electron -> real restarted Core; model replies reuse the synthetic crash fixture.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import assert from "node:assert/strict";
const root = process.cwd();
const interruptStage = process.argv.find(arg => arg.startsWith("--interrupt-stage="))?.split("=")[1] ?? "draft";
assert.ok(["draft", "risk"].includes(interruptStage));
const stages = ["check", "draft", "risk", "revise"];
const interruptIndex = stages.indexOf(interruptStage);
const out = resolve("docs/assets/collab-electron-crash-20261027");
mkdirSync(out, { recursive: true });
const data = mkdtempSync(resolve(out, "run-"));
const requireWeb = createRequire(resolve("apps/web-shell/package.json"));
const { createServer } = await import(pathToFileURL(requireWeb.resolve("vite")).href);
const requireHost = createRequire(resolve("apps/desktop-host/package.json"));
const requireBrowser = createRequire("C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js");
const { _electron: electron } = requireBrowser("playwright-core");
const headers = { "X-Core-Session-Token": "crash-test", "Content-Type": "application/json" };
const delay = ms => new Promise(done => setTimeout(done, ms));
const processes = [];
let server, desktop, page;
async function startCore(mode) {
  const child = spawn(resolve(".venv/Scripts/python.exe"), ["-u", "-c",
    "import runpy, sys; fixture=sys.argv.pop(1); exec(runpy.run_path(fixture)['SERVER'])",
    resolve("apps/core-api/tests/test_collab_process_restart_http.py"), data, mode, interruptStage], { cwd: root, windowsHide: true });
  const row = { child, closed: new Promise(done => child.once("close", done)), stderr: "" };
  processes.push(row);
  child.stderr.on("data", chunk => { row.stderr += String(chunk); });
  const ready = await new Promise((accept, reject) => {
    const timer = setTimeout(() => reject(new Error(`Core startup timed out: ${row.stderr}`)), 15000);
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("exit", code => { clearTimeout(timer); reject(new Error(`Core exited ${code}: ${row.stderr}`)); });
    let buffer = "";
    child.stdout.on("data", chunk => {
      buffer += String(chunk);
      const line = buffer.split("\n").find(line => line.startsWith('{"port"'));
      if (line) { clearTimeout(timer); accept(JSON.parse(line)); }
    });
  });
  row.base = `http://127.0.0.1:${ready.port}`;
  row.runId = ready.run_id;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(`${row.base}/health`)).ok) return row; } catch {}
    await delay(20);
  }
  throw new Error("Core did not become ready");
}
const next = core => fetch(`${core.base}/evidence/collab-next`, {
  method: "POST", headers, body: JSON.stringify({ run_id: core.runId }) });
const errors = [], resumed = [];
let report;
try {
  const first = await startCore("crash");
  let completed;
  for (const stage of stages.slice(0, interruptIndex)) {
    const response = await next(first);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.executed_stage, stage);
    assert.equal(body.executed_status, "done");
    completed = body.run.stages.slice(0, interruptIndex);
  }
  const waitingRequest = next(first).then(response => ({ status: response.status }), error => ({ interrupted: String(error) }));
  for (let i = 0; i < 200 && !existsSync(resolve(data, "waiting")); i++) await delay(20);
  assert.equal(readFileSync(resolve(data, "waiting"), "utf8"), interruptStage);
  first.child.kill();
  await first.closed;
  await waitingRequest;
  const restarted = await startCore("resume");
  assert.notEqual(restarted.child.pid, first.child.pid);
  assert.equal(restarted.runId, first.runId);
  server = await createServer({ root: resolve("apps/web-shell"), configFile: resolve("apps/web-shell/vite.config.ts"),
    define: { "import.meta.env.VITE_CORE_BASE": JSON.stringify("/api"), "import.meta.env.VITE_CORE_TOKEN": JSON.stringify("crash-test") },
    server: { host: "127.0.0.1", port: 0, strictPort: false,
      proxy: { "/api": { target: restarted.base, rewrite: path => path.replace(/^\/api/, "") } } } });
  await server.listen();
  const profile = resolve(data, "electron-profile");
  desktop = await electron.launch({ executablePath: requireHost("electron"),
    args: [resolve("apps/desktop-host"), `--user-data-dir=${profile}`], env: { ...process.env, STEWARD_WEB_URL: server.resolvedUrls.local[0] } });
  assert.equal(resolve(await desktop.evaluate(({ app }) => app.getPath("userData"))), profile);
  page = await desktop.firstWindow();
  page.on("pageerror", error => errors.push(String(error)));
  const responseTasks = [];
  page.on("response", response => {
    if (response.url().includes("/api/evidence/collab-next")) responseTasks.push(response.json().then(body => resumed.push(body)));
  });
  await page.getByRole("button", { name: "切换到工作区：系统设置", exact: true }).waitFor();
  await page.evaluate(() => { localStorage.setItem("steward.onboarding-deferred-at", String(Date.now())); });
  await page.reload();
  await page.getByRole("button", { name: "研究工作台", exact: true }).click();
  await page.getByRole("button", { name: "查看可恢复任务", exact: true }).click();
  await page.getByRole("button", { name: "恢复查看", exact: true }).click();
  await page.getByRole("button", { name: "继续执行（含失败阶段重试）", exact: true }).click();
  await page.locator(".wb-result").filter({ has: page.getByRole("heading", { name: /协同流水线/ }) })
    .getByText("已完成", { exact: true }).waitFor();
  await Promise.all(responseTasks);
  assert.deepEqual(resumed.map(row => row.executed_stage), stages.slice(interruptIndex));
  assert.ok(resumed.at(-1).persisted);
  assert.deepEqual(resumed.at(-1).run.stages.slice(0, interruptIndex), completed);
  const calls = readFileSync(resolve(data, "calls.jsonl"), "utf8").trim().split("\n").map(line => JSON.parse(line));
  assert.deepEqual(calls, [...stages.slice(0, interruptIndex + 1).map(stage => ({ mode: "crash", stage })),
    ...stages.slice(interruptIndex).map(stage => ({ mode: "resume", stage }))]);
  assert.deepEqual(errors, []);
  await page.getByText("没有待恢复任务。", { exact: true }).waitFor();
  await page.screenshot({ path: resolve(data, "completed.png"), fullPage: true });
  report = { status: "passed", mode: "electron_dev_real_core_crash_synthetic_model", data,
    interruptStage, firstPid: first.child.pid, restartedPid: restarted.child.pid, calls, errors,
    reportId: resumed.at(-1).collab_report_id, resumedStages: resumed.map(row => row.executed_stage) };
} catch (error) {
  writeFileSync(resolve(data, "results.json"), JSON.stringify({ status: "failed", error: String(error), errors }, null, 2));
  if (page) await page.screenshot({ path: resolve(data, "failed.png") }).catch(() => {});
  throw error;
} finally {
  await desktop?.close();
  await server?.close();
  for (const row of processes) {
    if (row.child.exitCode === null && !row.child.killed) row.child.stdin.end("stop\n");
    const timeout = new Promise((_, reject) => {
      const timer = setTimeout(() => reject(new Error("Core shutdown timed out")), 8000); timer.unref();
    });
    const code = await Promise.race([row.closed, timeout]);
    if (!row.child.killed) assert.equal(code, 0, row.stderr);
  }
}
writeFileSync(resolve(data, "results.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
