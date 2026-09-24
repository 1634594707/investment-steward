import { app, BrowserWindow, dialog, ipcMain, Menu, Notification, screen, session, shell, Tray } from "electron";
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { createServer } from "node:net";
import { join, resolve } from "node:path";
import { SidecarProcess } from "./processManager.js";
import { checkForUpdates, initUpdater } from "./updater.js";

// CJS 编译（Electron 主进程 ESM 加载在 pnpm 布局下报 cjsPreparseModuleExports 错）：
// 直接用 CommonJS 内置 __dirname（指向 dist/，与原 fileURLToPath(import.meta.url) 等价）。
const PROJECT_ROOT = resolve(__dirname, "../../..");
const coreDir = join(PROJECT_ROOT, "apps", "core-api");
const workerDir = join(PROJECT_ROOT, "apps", "agent-worker");
const runnerDir = join(PROJECT_ROOT, "apps", "plugin-runner");
const webShellDist = join(PROJECT_ROOT, "apps", "web-shell", "dist", "index.html");
// 打包后资源在 resources/ 下（asar 内 __dirname 指向 app.asar/dist，resources 不在其中）：
// 网页静态产物 → resources/web；图标 → resources/icon.ico；冻结后端 → resources/backend。
const packagedResources = process.resourcesPath ?? "";
const packagedWebIndex = join(packagedResources, "web", "index.html");
const packagedIcon = join(packagedResources, "icon.ico");
const packagedBackend = join(packagedResources, "backend", "steward-backend.exe");
// 图标：打包后在 resources/icon.ico（asar 外）；dev 在 desktop-host/resources（asar 外的源码树）。
// Tray 构造对不存在路径直接抛错（曾致打包版启动弹 Error 框），必须保证路径有效。
const appIcon = app.isPackaged ? packagedIcon : join(__dirname, "..", "resources", "icon.ico");
const sessionToken = randomBytes(32).toString("base64url");
let corePort = 0;
let core: SidecarProcess | undefined;
let worker: SidecarProcess | undefined;
let runner: SidecarProcess | undefined;
let mainWindow: BrowserWindow | undefined;
let tray: Tray | undefined;
let isQuitting = false;
// 关闭行为(D-F3 定稿):"ask"=每次询问,"quit"=直接退出,"tray"=保留托盘;记住的选择存 userData
let rememberedCloseAction: "quit" | "tray" | "ask" = "ask";

function loadRememberedCloseAction(): void {
  try {
    const raw = JSON.parse(readFileSync(join(app.getPath("userData"), "close-action.json"), "utf8"));
    if (raw && (raw.action === "quit" || raw.action === "tray" || raw.action === "ask")) {
      rememberedCloseAction = raw.action;
    }
  } catch {
    /* 无存档时保持询问 */
  }
}

function saveRememberedCloseAction(action: "quit" | "tray" | "ask"): void {
  try {
    writeFileSync(join(app.getPath("userData"), "close-action.json"), JSON.stringify({ action }));
  } catch {
    /* 写失败则下次继续询问 */
  }
}

function createTray(): void {
  tray = new Tray(appIcon);
  tray.setToolTip("Investment Steward · 投资管家（本地优先）");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "打开 Investment Steward",
        click: () => {
          mainWindow?.show();
          mainWindow?.focus();
        },
      },
      { type: "separator" },
      {
        label: "退出（结束全部后台进程）",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
}

app.setAppUserModelId("com.investmentsteward.app");

// GPU 禁用走命令行级开关（等效启动参数 --disable-gpu）：仅 disableHardwareAcceleration()
// 在本机（远程会话/无独显）仍会出现 GPU 进程连续崩溃 → FATAL 退出（打包版踩坑，dev 靠参数没暴露）。
// dev 启动命令还带 --no-sandbox：GPU/渲染子进程沙箱在本机起不来，打包版同样需要禁沙箱。
app.commandLine.appendSwitch("no-sandbox");
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-gpu-compositing");
app.commandLine.appendSwitch("disable-gpu-sandbox");
app.commandLine.appendSwitch("disable-dev-shm-usage");
// 无条件禁用硬件加速：与上述开关双保险。
app.disableHardwareAcceleration();
if (!app.isPackaged) {
  app.setPath("userData", join(PROJECT_ROOT, ".runtime-local", "desktop-user-data"));
}

function findFreePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("could not allocate loopback port"));
        return;
      }
      server.close(() => resolvePort(address.port));
    });
  });
}

async function waitForCore(baseUrl: string): Promise<void> {
  // 打包模式的后端是 PyInstaller onefile，首次启动要解压 ~32MB 到临时目录（实测 20-35s），
  // 20s 会误判启动失败；dev 模式（venv python）保持 20s。
  const budgetMs = app.isPackaged ? 120_000 : 20_000;
  const deadline = Date.now() + budgetMs;
  let lastError = "Core did not become ready";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`);
      if (response.ok) return;
      lastError = `Core health returned ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
  }
  throw new Error(lastError);
}

async function startSidecars(): Promise<void> {
  corePort = await findFreePort();
  const dataDir = join(app.getPath("userData"), "data");
  // 打包模式：用 PyInstaller 冻结的 steward-backend.exe（--mode core|worker|runner），
  // 用户机器无需安装 Python；dev 模式仍走项目 .venv（依赖齐全，系统 python 缺 cryptography 会崩）。
  const packaged = app.isPackaged;
  const venvPython = join(PROJECT_ROOT, ".venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "python.exe" : "python");
  const python = process.env.STEWARD_CORE_PYTHON ?? (existsSync(venvPython) ? venvPython : "python");
  const backendCwd = packaged ? join(packagedResources, "backend") : coreDir;
  const coreSpec = packaged
    ? {
        command: packagedBackend,
        // ★ 传 --default-data-dir（宿主兜底）而非 --data-dir（显式指定）：
        //   显式指定的优先级高于位置指针，会把用户在设置页迁移后的存储位置
        //   在每次重启时覆盖回 userData/data（2026-09-10 真机缺陷）。
        //   兜底模式下「env > 位置指针 > userData/data」，用户的选择重启后仍生效。
        args: ["--mode", "core", "--host", "127.0.0.1", "--port", String(corePort), "--default-data-dir", dataDir, "--session-token", sessionToken],
        env: undefined as NodeJS.ProcessEnv | undefined,
      }
    : {
        command: python,
        args: ["-m", "investment_steward_core", "--host", "127.0.0.1", "--port", String(corePort), "--default-data-dir", dataDir, "--session-token", sessionToken],
        env: { PYTHONPATH: join(coreDir, "src") } as NodeJS.ProcessEnv | undefined,
      };
  // 冻结后端不再有源码目录结构：插件注册表与发布者公钥必须显式指到 resources 下的副本
  // （Core 端两个 env 覆盖点：STEWARD_PLUGIN_REGISTRY_DIR / STEWARD_PLUGIN_PUBLIC_KEY_FILE）。
  if (packaged) {
    coreSpec.env = {
      STEWARD_PLUGIN_REGISTRY_DIR: join(packagedResources, "plugins"),
      STEWARD_PLUGIN_PUBLIC_KEY_FILE: join(packagedResources, "keys", "steward-plugin-publishing.pub.pem"),
    };
  }
  core = new SidecarProcess({
    name: "core-api",
    command: coreSpec.command,
    cwd: backendCwd,
    args: coreSpec.args,
    env: coreSpec.env,
  });
  core.start();
  await waitForCore(`http://127.0.0.1:${corePort}`);

  // E1：出网白名单由 Core 聚合能力声明并注入 runner env（Network allowlist 由 Core 代理注入）。
  let networkAllowlist: string[] = [];
  let grantedCapabilities: string[] = [];
  try {
    const config = await fetch(`http://127.0.0.1:${corePort}/runner/config`, {
      headers: { "X-Core-Session-Token": sessionToken },
    });
    if (config.ok) {
      const payload = await config.json();
      networkAllowlist = payload.network_allowlist ?? [];
      grantedCapabilities = payload.granted_capabilities ?? [];
    }
  } catch {
    networkAllowlist = [];
  }

  const workerSpec = packaged
    ? {
        command: packagedBackend,
        args: ["--mode", "worker", "--status-file", join(dataDir, "agent-worker.status.json"), "--core-url", `http://127.0.0.1:${corePort}`, "--session-token", sessionToken],
        env: undefined as NodeJS.ProcessEnv | undefined,
      }
    : {
        command: python,
        args: ["-m", "investment_steward_agent_worker", "--status-file", join(dataDir, "agent-worker.status.json"), "--core-url", `http://127.0.0.1:${corePort}`, "--session-token", sessionToken],
        env: { PYTHONPATH: join(workerDir, "src") } as NodeJS.ProcessEnv | undefined,
      };
  worker = new SidecarProcess({
    name: "agent-worker",
    command: workerSpec.command,
    cwd: backendCwd,
    args: workerSpec.args,
    env: workerSpec.env,
  });
  // 出网白名单由 Core 注入 env；runner 侧按此二次门控（G3-5 零密钥：不改环境、不注入凭据）。
  const runnerEnv: NodeJS.ProcessEnv = {
    STEWARD_NETWORK_ALLOWLIST: JSON.stringify(networkAllowlist),
    STEWARD_GRANTED_CAPABILITIES: JSON.stringify(grantedCapabilities),
  };
  runner = new SidecarProcess({
    name: "plugin-runner",
    command: packaged ? packagedBackend : python,
    cwd: backendCwd,
    args: packaged ? ["--mode", "runner"] : ["-m", "investment_steward_plugin_runner"],
    env: packaged ? runnerEnv : { ...runnerEnv, PYTHONPATH: join(runnerDir, "src") },
    stdio: "pipe",
  });
  worker.start();
  runner.start();
}

// F3-1 窗口状态记忆：大小/位置/最大化存 userData（.runtime-local/desktop-user-data），
// 启动时恢复并做屏幕可见性校验（拔显示器后不出现屏幕外窗口）。
interface WindowState {
  x?: number;
  y?: number;
  width: number;
  height: number;
  maximized: boolean;
}

function windowStatePath(): string {
  return join(app.getPath("userData"), "window-state.json");
}

function loadWindowState(): WindowState | null {
  try {
    const raw: unknown = JSON.parse(readFileSync(windowStatePath(), "utf8"));
    if (raw && typeof raw === "object") {
      const obj = raw as Record<string, unknown>;
      if (typeof obj.width === "number" && typeof obj.height === "number") {
        return {
          x: typeof obj.x === "number" ? obj.x : undefined,
          y: typeof obj.y === "number" ? obj.y : undefined,
          width: obj.width,
          height: obj.height,
          maximized: obj.maximized === true,
        };
      }
    }
  } catch {
    /* 首次启动无存档 */
  }
  return null;
}

function isRectVisible(bounds: { x: number; y: number; width: number; height: number }): boolean {
  return screen.getAllDisplays().some((display) => {
    const area = display.workArea;
    return (
      bounds.x < area.x + area.width - 80 &&
      bounds.x + bounds.width > area.x + 80 &&
      bounds.y < area.y + area.height - 80 &&
      bounds.y + bounds.height > area.y + 80
    );
  });
}

function saveWindowState(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try {
    const normal = mainWindow.getNormalBounds();
    const state: WindowState = { ...normal, maximized: mainWindow.isMaximized() };
    writeFileSync(windowStatePath(), JSON.stringify(state));
  } catch {
    /* 写失败不影响退出 */
  }
}

function sendZoomPercent(): void {
  const contents = mainWindow?.webContents;
  if (!contents || mainWindow?.isDestroyed()) return;
  mainWindow!.webContents.send("steward:zoom", Math.round(contents.getZoomFactor() * 100));
}

function applyZoomDelta(delta: number): void {
  const contents = mainWindow?.webContents;
  if (!contents) return;
  const next = Math.min(3, Math.max(-3, Math.round((contents.getZoomLevel() + delta) * 2) / 2));
  contents.setZoomLevel(next);
  sendZoomPercent();
}

// 当前真实数据目录：与后端 StorageLayout.from_environment 同口径（打包模式无 env 覆盖，
// 即「位置指针 > userData/data 兜底」）。用户迁移存储位置后菜单才能打开新目录。
function currentDataDir(): string {
  try {
    const pointer = join(process.env.LOCALAPPDATA ?? "", "InvestmentSteward", "storage-layout.json");
    const parsed = JSON.parse(readFileSync(pointer, "utf8")) as { user_data?: unknown };
    if (typeof parsed.user_data === "string" && parsed.user_data) return parsed.user_data;
  } catch {
    // 无指针 / 损坏 / 未设 LOCALAPPDATA → 兜底 userData/data
  }
  return join(app.getPath("userData"), "data");
}

function buildApplicationMenu(): void {
  const menu = Menu.buildFromTemplate([
    {
      label: "文件",
      submenu: [
        {
          label: "打开数据目录",
          click: () => {
            void shell.openPath(currentDataDir());
          },
        },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { label: "放大", accelerator: "CmdOrCtrl+=", click: () => applyZoomDelta(0.5) },
        { label: "缩小", accelerator: "CmdOrCtrl+-", click: () => applyZoomDelta(-0.5) },
        {
          label: "重置缩放",
          accelerator: "CmdOrCtrl+0",
          click: () => {
            mainWindow?.webContents.setZoomLevel(0);
            sendZoomPercent();
          },
        },
        { type: "separator" },
        { role: "reload", label: "重新加载" },
        { role: "forceReload", label: "强制重新加载" },
        { role: "toggleDevTools", label: "开发者工具" },
      ],
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "快捷键速查",
          click: () => mainWindow?.webContents.send("steward:show-shortcuts"),
        },
        { type: "separator" },
        {
          label: "关于 Investment Steward",
          click: () => {
            if (!mainWindow) return;
            void dialog.showMessageBox(mainWindow, {
              type: "info",
              title: "关于 Investment Steward",
              message: `Investment Steward ${app.getVersion()}`,
              detail: "AI 投资管家 · 本地优先\nCore loopback + 随机会话令牌 · 插件 Ed25519 签名 · 数据不出本机",
            });
          },
        },
      ],
    },
  ]);
  Menu.setApplicationMenu(menu);
}

function registerBridge(): void {  ipcMain.handle("host:get-info", () => ({
    host_version: app.getVersion(),
    bridge_version: "1.0",
    platform: process.platform,
    is_packaged: app.isPackaged,
  }));
  ipcMain.handle("host:get-core-status", () => ({
    status: core?.pid ? "ready" : "stopped",
    base_url: `http://127.0.0.1:${corePort}`,
    version: "0.1.0",
    pid: core?.pid,
    last_error: core?.error ?? null,
  }));
  ipcMain.handle("host:restart-core", async () => {
    await core?.stop();
    core?.start();
    await waitForCore(`http://127.0.0.1:${corePort}`);
    return { status: "ready", base_url: `http://127.0.0.1:${corePort}`, version: "0.1.0", pid: core?.pid, last_error: null };
  });
  ipcMain.handle("host:core-request", async (_event, request: { method: "GET" | "POST" | "PUT" | "DELETE"; path: string; body?: unknown; timeoutMs?: number; reqId?: string }) => {
    const requestPath = request.path.split("?", 1)[0]!;
    const corePath = /^\/(?:health|session|slots|cards\/slots\/[^/]+|instruments\/(?:resolve|lookup)|investment-policies(?:\/[^/]+\/confirm)?|investor\/profile|evidence(?:\/(?:announcements|news|financials)\/[^/]+|\/macro\/(?:events|releases|trade|impacts|calendar|weights(?:\/ai-proposal)?)|\/macro\/[^/]+(?:\/(?:anomalies|analysis|my-view|pricing|signals(?:\/[^/]+\/interpret)?))?|\/[^/]+)?|capabilities|audit|plugins\/(?:catalog|[^/]+\/(?:install|disable|update|revoke|app))|market\/candles\/[^/]+|holdings(?:\/[^/]+)?|thesis(?:\/[^/]+)?|plans(?:\/[^/]+\/transition)?|decisions|research\/runs(?:\/[^/]+(?:\/(?:transition|run|response))?)?|research\/questions\/latest|research\/snapshots(?:\/[^/]+)?|research\/templates(?:\/[^/]+)?|brief\/today(?:\/generate)?|review\/weekly|portfolio\/risk|notifications\/(?:pending|triage|evaluate|channels|graded|[^/]+\/read)|data-source\/(?:quality|failures|retry)|decisions(?:\/inaction-stats)?|evidence\/freshness\/patrol|learning\/(?:unit\/today|goals\/current|activities(?:\/[^/]+(?:\/propose-policy-change)?)?|reflections\/export)|channels|credentials(?:\/[^/]+(?:\/test)?)?|model-profiles(?:\/[^/]+(?:\/(?:activate|test))?)?|model-usage|jev\/(?:config|probe|models)|library\/(?:books(?:\/[^/]+(?:\/[^/]+)?)?|plan\/generate|annotations\/[^/]+\/to-research|insights)|quant\/(?:artifacts|runs|experiments(?:\/[^/]+\/(?:reproduce|leakage-check|walk-forward))?|lineage\/.+|factors\/(?:compute|mine)\/[^/]+|parameter-sets(?:\/[^/]+\/(?:lineage|replay|fork|track-records(?:\/verify)?))?|stages|models\/[^/]+|strategy-packs(?:\/[^/]+\/run)?|portfolio\/backtest)|runner\/config|export\/all|import\/(?:preview|apply)|personal\/settings|storage\/(?:layout(?:\/(?:migrate(?:\/preview)?|verify|reset-default))?|lifecycle|backups(?:\/[^/]+\/restore)?)|timeline\/decision\/[^/]+|sync\/(?:config(?:\/reveal)?|pairing-code|status|run|inbox|apply)|transfer\/requests(?:\/[^/]+(?:\/(?:decide|send|receive))?)?|market\/(?:catalog|install)|judgments(?:\/[^/]+(?:\/verify)?)?|watch-items(?:\/[^/]+(?:\/(?:checks|transition))?)?|tactics\/(?:catalog|sectors|signals\/[^/]+|scan|scan-market(?:\/[^/]+(?:\/cancel)?)?|ai-review(?:s(?:\/[^/]+)?)?|watchlist(?:\/[^/]+(?:\/notes)?)?|notes\/[^/]+)|youzi\/(?:today|watchlist|cross-check|cffex|curriculum|tactics(?:\/[^/]+)?|replay\/[^/]+|seats\/[^/]+|profile\/[^/]+)|ai-research\/(?:review-queue|reports(?:\/[^/]+(?:\/follow-ups)?)?))$/.test(requestPath);
    if (!corePath) {
      throw new Error(`Core path is not exposed through Host Bridge: ${requestPath}`);
    }
    const hasBody = request.method === "POST" || request.method === "PUT";
    // A02（2026-09-15）：此前用 Node 全局 fetch（undici），其默认 headersTimeout=300s 会切断
    // 深研档等 >5 分钟的同步长请求（实测 525.5s 在 306s 被 HeadersTimeoutError 中断）。
    // node:http 无该默认限制；**默认仍不设超时**（A02 修的 525.5s 场景不回退），但 C01
    // （桌面端升级路线图 2026-09-18）起渲染层可为请求显式携带 timeoutMs（仅 GET 有默认值，
    // 由渲染层决定后传入）——到点 request.destroy() 并以 504 形状返回，使 classifyCoreError
    // 能归为 timeout；请求也可经 host:core-cancel 携 reqId 取消（以 499 形状返回）。
    const inflightReqId = request.reqId ?? null;
    return await new Promise<{ status: number; data: unknown }>((resolveRequest, rejectRequest) => {
      const payload = hasBody ? JSON.stringify(request.body ?? {}) : null;
      const coreRequest = httpRequest(
        {
          host: "127.0.0.1",
          port: corePort,
          method: request.method,
          path: request.path,
          headers: {
            "X-Core-Session-Token": sessionToken,
            "Content-Type": "application/json",
            ...(payload !== null ? { "Content-Length": Buffer.byteLength(payload) } : {}),
          },
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on("data", (chunk: Buffer) => chunks.push(chunk));
          response.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf-8");
            let data: unknown = null;
            try { data = JSON.parse(text); } catch { data = { detail: text }; }
            settle({ status: response.statusCode ?? 0, data });
          });
        },
      );
      // C01：终止态仲裁——destroy() 会触发 "error"，用 abortOutcome 区分「主动终止」与「网络错误」。
      let abortOutcome: { status: number; data: unknown } | null = null;
      let settled = false;
      let timeoutTimer: NodeJS.Timeout | undefined;
      const settle = (result: { status: number; data: unknown }) => {
        if (settled) return;
        settled = true;
        if (timeoutTimer) clearTimeout(timeoutTimer);
        if (inflightReqId) inflightRequests.delete(inflightReqId);
        resolveRequest(result);
      };
      if (request.timeoutMs && request.timeoutMs > 0) {
        timeoutTimer = setTimeout(() => {
          abortOutcome = { status: 504, data: { detail: `Core 请求超时（${Math.round(request.timeoutMs! / 1000)}s 未响应）：命令面板可重启本地 Core。` } };
          coreRequest.destroy();
        }, request.timeoutMs);
        timeoutTimer.unref?.();
      }
      if (inflightReqId) inflightRequests.set(inflightReqId, coreRequest);
      coreRequest.on("error", (error) => {
        if (settled) return;
        settled = true;
        if (timeoutTimer) clearTimeout(timeoutTimer);
        if (inflightReqId) inflightRequests.delete(inflightReqId);
        if (abortOutcome) resolveRequest(abortOutcome);
        else rejectRequest(error);
      });
      coreRequest.on("close", () => {
        // destroy() 在部分平台上只触发 close 不触发 error：保证终止态一定落账。
        if (abortOutcome && !settled) settle(abortOutcome);
      });
      if (payload !== null) coreRequest.write(payload);
      coreRequest.end();
    });
  });
  // C01：在途请求登记表（reqId → node:http request），供 host:core-cancel 取消。
  const inflightRequests = new Map<string, import("node:http").ClientRequest>();
  ipcMain.handle("host:core-cancel", (_event, reqId: string) => {
    const inflight = inflightRequests.get(reqId);
    if (!inflight) return false;
    inflightRequests.delete(reqId);
    inflight.destroy();
    return true;
  });
  ipcMain.handle("host:runner-request", async (_event, request: Record<string, unknown>) => {
    if (!runner) throw new Error("Plugin Runner is not available");
    return runner.requestJsonRpc(request);
  });
  ipcMain.handle("host:select-file", async (_event, options: { multiple?: boolean; directory?: boolean } = {}) => {
    const properties: Array<"openFile" | "multiSelections" | "openDirectory" | "createDirectory"> = options.directory
      ? ["openDirectory", "createDirectory"]
      : options.multiple
        ? ["openFile", "multiSelections"]
        : ["openFile"];
    const result = await dialog.showOpenDialog(mainWindow!, { properties });
    return result.canceled ? [] : result.filePaths;
  });
  ipcMain.handle("host:open-path", async (_event, path: string) => {
    const errorMessage = await shell.openPath(path);
    return errorMessage.length === 0;
  });
  ipcMain.handle("host:notify", (_event, input: { title: string; body: string }) => {
    if (Notification.isSupported()) new Notification(input).show();
  });
  ipcMain.handle("host:restart", () => { app.relaunch(); app.quit(); });
  ipcMain.handle("window:minimize", () => mainWindow?.minimize());
  ipcMain.handle("window:maximize", () => {
    if (!mainWindow) return;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  });
  ipcMain.handle("window:close", () => mainWindow?.close());
  ipcMain.handle("window:perform-close-action", (_event, action: "tray" | "quit", remember?: boolean) => {
    if (remember) {
      rememberedCloseAction = action;
      saveRememberedCloseAction(rememberedCloseAction);
    }
    if (action === "tray") {
      mainWindow?.hide();
    } else {
      isQuitting = true;
      app.quit();
    }
  });
  ipcMain.handle("host:check-for-updates", () => { checkForUpdates(); });
  ipcMain.handle("window:get-close-action", () => rememberedCloseAction);
  ipcMain.handle("window:set-close-action", (_event, action: "ask" | "tray" | "quit") => {
    rememberedCloseAction = action;
    saveRememberedCloseAction(action);
  });
}

async function createMainWindow(): Promise<void> {
  // F3-1 恢复上次窗口状态；不可见（屏幕外）或无存档时用默认尺寸。
  const saved = loadWindowState();
  const restore = saved && isRectVisible({ x: saved.x ?? 0, y: saved.y ?? 0, width: saved.width, height: saved.height })
    ? { x: saved.x, y: saved.y, width: saved.width, height: saved.height }
    : { width: 1440, height: 920 };
  mainWindow = new BrowserWindow({
    ...restore,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: "#0f1112",
    frame: false,
    icon: appIcon,
    webPreferences: {
      preload: join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  if (saved?.maximized) mainWindow.maximize();
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  // F3-1/F3-2：窗口状态落盘（防抖）与缩放变化推送（Ctrl+滚轮同样生效）。
  let saveTimer: ReturnType<typeof setTimeout> | undefined;
  const scheduleSave = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveWindowState, 400);
  };
  mainWindow.on("resize", scheduleSave);
  mainWindow.on("move", scheduleSave);
  mainWindow.on("maximize", () => mainWindow?.webContents.send("steward:win-state", true));
  mainWindow.on("unmaximize", () => mainWindow?.webContents.send("steward:win-state", false));
  mainWindow.webContents.on("zoom-changed", () => sendZoomPercent());
  mainWindow.on("close", (event) => {
    clearTimeout(saveTimer);
    saveWindowState();
    // D-F3 定稿:关闭先询问「退出 / 保留到托盘」;记住的选择可跳过询问
    if (isQuitting || rememberedCloseAction === "quit") return;
    event.preventDefault();
    if (rememberedCloseAction === "tray") {
      mainWindow?.hide();
      return;
    }
    void dialog
      .showMessageBox(mainWindow!, {
        type: "question",
        title: "关闭 Investment Steward",
        message: "退出程序，还是保留到系统托盘继续运行？",
        detail: "保留到托盘：Core 与插件后台继续运行，可从托盘图标回到界面或退出。",
        buttons: ["退出程序", "保留到托盘", "取消"],
        defaultId: 0,
        cancelId: 2,
        checkboxLabel: "记住我的选择，不再询问",
        checkboxChecked: false,
      })
      .then(({ response, checkboxChecked }) => {
        if (response === 2) return;
        if (checkboxChecked) {
          rememberedCloseAction = response === 1 ? "tray" : "quit";
          saveRememberedCloseAction(rememberedCloseAction);
        }
        if (response === 1) mainWindow?.hide();
        else {
          isQuitting = true;
          app.quit();
        }
      });
  });
  const devUrl = process.env.STEWARD_WEB_URL ?? "http://127.0.0.1:5173";
  if (!app.isPackaged && process.env.STEWARD_WEB_URL) await mainWindow.loadURL(devUrl);
  else await mainWindow.loadFile(app.isPackaged ? packagedWebIndex : webShellDist);
  sendZoomPercent();
}

// 单实例锁:二次启动直接聚焦已有窗口,避免多实例进程残留(用户反馈:退出后进程应全部退出)
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  app.whenReady().then(async () => {
  loadRememberedCloseAction();
  buildApplicationMenu();
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  registerBridge();
  initUpdater();
  try {
    // Dev 模式（显式 STEWARD_WEB_URL + 非打包）：不 spawn 内置 sidecars，
    // 页面（Vite dev server）经 VITE_CORE_BASE/VITE_CORE_TOKEN 直连外部 Core ——
    // 与纯浏览器行为一致，数据同库（此前 Electron 内 spawn 的空 userData 库曾造成「窗口里看不到我的方案」）。
    const devMode = !app.isPackaged && !!process.env.STEWARD_WEB_URL;
    if (devMode) {
      corePort = 18765;
      console.log("[steward] dev mode: using external core at", `http://127.0.0.1:${corePort}`);
    } else {
      await startSidecars();
    }
    await createMainWindow();
    createTray();
  } catch (error) {
    dialog.showErrorBox("Investment Steward 启动失败", error instanceof Error ? error.message : String(error));
    app.quit();
  }
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("before-quit", async (event) => {
  event.preventDefault();
  await Promise.all([runner?.stop(), worker?.stop(), core?.stop()]);
  app.exit(0);
});
}
