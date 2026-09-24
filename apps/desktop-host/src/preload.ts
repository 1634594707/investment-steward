import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("steward", {
  getHostInfo: () => ipcRenderer.invoke("host:get-info"),
  getCoreStatus: () => ipcRenderer.invoke("host:get-core-status"),
  restartCore: () => ipcRenderer.invoke("host:restart-core"),
  requestFileSelection: (options?: { multiple?: boolean; directory?: boolean }) => ipcRenderer.invoke("host:select-file", options),
  openPath: (path: string) => ipcRenderer.invoke("host:open-path", path),
  showNotification: (input: { title: string; body: string }) => ipcRenderer.invoke("host:notify", input),
  requestAppRestart: () => ipcRenderer.invoke("host:restart"),
  coreRequest: (request: { method: "GET" | "POST" | "PUT" | "DELETE"; path: string; body?: unknown; timeoutMs?: number; reqId?: string }) => ipcRenderer.invoke("host:core-request", request),
  // C01（桌面端升级路线图 2026-09-18）：取消在途 Core 请求（宿主对该 TCP 连接 request.destroy()）。
  coreCancel: (reqId: string) => ipcRenderer.invoke("host:core-cancel", reqId),
  runnerRequest: (request: Record<string, unknown>) => ipcRenderer.invoke("host:runner-request", request),
  // E2：自动更新（仅打包态生效；状态经 steward:update-status 推送）。
  checkForUpdates: () => ipcRenderer.invoke("host:check-for-updates"),
  onUpdateStatus: (callback: (status: unknown) => void) => {
    ipcRenderer.on("steward:update-status", (_event, status: unknown) => callback(status));
  },
});

contextBridge.exposeInMainWorld("stewardWin", {
  minimize: () => ipcRenderer.invoke("window:minimize"),
  maximize: () => ipcRenderer.invoke("window:maximize"),
  close: () => ipcRenderer.invoke("window:close"),
  performCloseAction: (action: "tray" | "quit", remember?: boolean) => ipcRenderer.invoke("window:perform-close-action", action, remember),
  getRememberedCloseAction: () => ipcRenderer.invoke("window:get-close-action"),
  setRememberedCloseAction: (action: "ask" | "tray" | "quit") => ipcRenderer.invoke("window:set-close-action", action),
  // F3-3：窗口最大化/还原状态推送（标题栏按钮图标态）。
  onWindowState: (callback: (maximized: boolean) => void) => {
    ipcRenderer.on("steward:win-state", (_event, maximized: boolean) => callback(maximized));
  },
  // F3-2：缩放档变化推送（菜单加速键 / Ctrl+滚轮）。
  onZoom: (callback: (percent: number) => void) => {
    ipcRenderer.on("steward:zoom", (_event, percent: number) => callback(percent));
  },
});
