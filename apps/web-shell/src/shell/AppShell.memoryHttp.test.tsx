import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { afterAll, beforeAll, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AppShell } from "./AppShell";

// Real local HTTP + temporary SQLite; model network disabled. No user data or API credentials.
const fixture = `
import json, socket, tempfile, os, sys, threading
from uuid import uuid4
from pathlib import Path
import uvicorn
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence, ModelProfile
with tempfile.TemporaryDirectory(prefix="data-", dir=os.environ["STEWARD_MEMORY_TEST_PARENT"]) as folder:
 app = create_app(CoreSettings(data_dir=Path(folder), session_token="synthetic-local", model_access_enabled=False))
 core = app.state.core
 eid = uuid4()
 core.database.insert_evidence(Evidence(evidence_id=eid, tenant_id=core.local_user_id, subject_refs=["600001"], evidence_type="financial", financial_period_end="2026-06-30", source_name="synthetic-memory", summary="Operating cash flow declined", content_hash="synthetic-memory-http-123456", relation="contradicting"))
 core.database.upsert_model_profile(ModelProfile(profile_id=uuid4(), name="synthetic local only", base_url="https://invalid.example", model="synthetic", credential_ref="unused", status="使用中"))
 http = TestClient(app)
 headers = {"X-Core-Session-Token": "synthetic-local"}
 h = http.post("/holdings", headers=headers, json={"instrument":"600001","label":"合成标的","status":"watchlist","strategy_note":"合成测试"})
 assert h.status_code == 201, h.text
 sock = socket.socket(); sock.bind(("127.0.0.1", 0))
 print(json.dumps({"port":sock.getsockname()[1], "evidence":str(eid)}), flush=True)
 server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", log_level="error"))
 def shutdown():
  sys.stdin.readline()
  server.should_exit = True
 threading.Thread(target=shutdown, daemon=True).start()
 try:
  server.run(sockets=[sock])
 finally:
  core.database.close()
`;
let server: ChildProcess;
let base: string;
let evidenceId: string;
let fixtureParent: string;
beforeAll(async () => {
  const root = resolve("../..");
  fixtureParent = mkdtempSync(resolve(tmpdir(), "steward-memory-http-"));
  server = spawn(resolve(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"), ["-u", "-c", fixture], {
    cwd: root, windowsHide: true, env: { ...process.env, STEWARD_MEMORY_TEST_PARENT: fixtureParent },
  });
  let errors = "";
  server.stderr?.on("data", (chunk) => { errors += String(chunk); });
  const ready = await new Promise<{port: number; evidence: string}>((accept, reject) => {
    let buffer = "";
    const timer = setTimeout(() => reject(new Error(`Local fixture startup timeout: ${errors}`)), 15000);
    server.once("error", (error) => { clearTimeout(timer); reject(error); });
    server.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Local fixture exited ${code}: ${errors}`)); });
    server.stdout?.on("data", (chunk) => {
      buffer += String(chunk);
      for (const line of buffer.split("\n")) {
        if (line.startsWith('{"port"')) { clearTimeout(timer); accept(JSON.parse(line)); }
      }
    });
  });
  base = `http://127.0.0.1:${ready.port}`;
  evidenceId = ready.evidence;
  await waitFor(async () => { expect((await fetch(`${base}/health`)).ok).toBe(true); }, { timeout: 10000 });
  vi.stubEnv("VITE_CORE_BASE", base);
  vi.stubEnv("VITE_CORE_TOKEN", "synthetic-local");
  localStorage.setItem("steward.last-view", "investment");
  localStorage.setItem("steward.onboarding-deferred-at", String(Date.now()));
}, 30000);
afterAll(async () => {
  cleanup(); vi.unstubAllEnvs();
  if (server && server.exitCode === null) {
    await new Promise<void>((done, reject) => {
      const timer = setTimeout(() => reject(new Error("Local fixture did not shut down")), 5000);
      server.once("close", (code) => {
        clearTimeout(timer);
        if (code === 0) done(); else reject(new Error(`Local fixture shutdown failed: ${code}`));
      });
      server.stdin?.end("stop\n");
    });
  }
  if (fixtureParent) rmSync(fixtureParent, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
});

it("AppShell saves a thesis association over HTTP and retrieves its scoped research memory", async () => {
  const view = render(<AppShell />);
  fireEvent.click(await screen.findByRole("button", { name: "新建投资逻辑" }, { timeout: 10000 }));
  fireEvent.change(screen.getByLabelText("投资逻辑陈述"), { target: { value: "回款支撑增长的用户假设" } });
  fireEvent.change(screen.getByLabelText("失效条件"), { target: { value: "回款持续恶化" } });
  fireEvent.click(screen.getByRole("checkbox", { name: new RegExp(evidenceId) }));
  fireEvent.click(screen.getByRole("button", { name: /保存投资逻辑/ }));
  const linked = await screen.findByRole("region", { name: "已关联证据" });
  expect(within(linked).getByRole("button", { name: /synthetic-memory/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "研究" }));
  const newRun = await screen.findByRole("button", { name: /新建研究/ });
  await waitFor(() => expect(newRun).not.toBeDisabled());
  fireEvent.click(newRun);
  fireEvent.change(screen.getByLabelText("你的问题"), { target: { value: "cash flow" } });
  fireEvent.click(screen.getByText("证据范围（可选）"));
  fireEvent.change(screen.getByLabelText("标的标识"), { target: { value: "600001" } });
  fireEvent.change(screen.getByLabelText("报告期末（可多项）"), { target: { value: "2026-06-30" } });
  fireEvent.click(screen.getByRole("button", { name: /开始研究|创建/ }));
  expect(await screen.findByText("研究记忆 · 600001", {}, { timeout: 10000 })).toBeTruthy();
  const memory = screen.getByText("研究记忆 · 600001").closest("section")!;
  expect(within(memory).getByText("原始判断：回款支撑增长的用户假设")).toBeTruthy();
  expect(within(memory).getByText("回款持续恶化")).toBeTruthy();
  fireEvent.click(within(memory).getAllByRole("button", { name: /synthetic-memory/ })[0]!);
  expect(await screen.findByRole("dialog", { name: "证据详情" })).toBeTruthy();
  expect(within(screen.getByRole("dialog", { name: "证据详情" })).getByText("Operating cash flow declined")).toBeTruthy();
  expect(within(screen.getByRole("dialog", { name: "证据详情" })).getByText("2026-06-30")).toBeTruthy();
  expect(within(screen.getByRole("dialog", { name: "证据详情" })).getByText("未知口径")).toBeTruthy();
  const headers = { "X-Core-Session-Token": "synthetic-local" };
  const runs = await (await fetch(`${base}/research/runs`, { headers })).json();
  expect(runs).toHaveLength(1);
  expect(runs[0].evidence_scope.financial_period_ends).toEqual(["2026-06-30"]);
  const saved = await (await fetch(`${base}/research/runs/${runs[0].run_id}/response`, { headers })).json();
  expect(saved.model_provider).toBe("local-deterministic");
  expect(saved.research_memory[0]).toMatchObject({ original_statement: "回款支撑增长的用户假设",
    linked_evidence_refs: [evidenceId], candidate_contradicting_refs: [evidenceId] });
  view.unmount();
  render(<AppShell />);
  expect(await screen.findByText("研究记忆 · 600001", {}, { timeout: 10000 })).toBeTruthy();
  expect(screen.getByText("原始判断：回款支撑增长的用户假设")).toBeTruthy();
}, 30000);
