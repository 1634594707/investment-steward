import { app, BrowserWindow, Notification } from "electron";
import { autoUpdater } from "electron-updater";

/**
 * E2（frontend-optimization-roadmap-2026-09-12）：桌面自动更新通道。
 * electron-updater + GitHub Releases（与插件市场 GitHub 分发设计对齐）；NSIS + blockmap 已由
 * electron-builder 产出（差量更新）。仅打包态启用；离线 / 无发布源时静默失败，不打扰用户。
 * 注意：安装包当前未签名（signExecutable: false），Windows SmartScreen 会拦截升级器——
 * 签名补齐前，更新通道处于「可用但需用户放行」状态。
 */
export type UpdateStatus =
  | { phase: "checking" }
  | { phase: "not-available" }
  | { phase: "available"; version: string }
  | { phase: "downloading"; percent: number }
  | { phase: "downloaded"; version: string }
  | { phase: "error"; message: string };

function sendStatus(status: UpdateStatus): void {
  BrowserWindow.getAllWindows().forEach((window) => {
    window.webContents.send("steward:update-status", status);
  });
}

export function initUpdater(): void {
  if (!app.isPackaged) return;
  autoUpdater.autoDownload = true;
  // 下载完成后不强制重启：等用户下次退出时安装（autoInstallOnAppQuit），不打断研究流程。
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("checking-for-update", () => sendStatus({ phase: "checking" }));
  autoUpdater.on("update-not-available", () => sendStatus({ phase: "not-available" }));
  autoUpdater.on("update-available", (info) => sendStatus({ phase: "available", version: info.version ?? "" }));
  autoUpdater.on("download-progress", (progress) => sendStatus({ phase: "downloading", percent: Math.round(progress.percent ?? 0) }));
  autoUpdater.on("update-downloaded", (info) => {
    sendStatus({ phase: "downloaded", version: info.version ?? "" });
    // 系统通知兜底：用户不在应用内也能感知（托盘常驻场景）。
    try {
      const notification = new Notification({ title: "Investment Steward", body: `新版本 v${info.version ?? ""} 已就绪，重启应用后生效。` });
      notification.show();
    } catch {
      /* 通知不可用时静默（状态已推送至渲染层） */
    }
  });
  autoUpdater.on("error", (error) => sendStatus({ phase: "error", message: error?.message ?? String(error) }));
  void autoUpdater.checkForUpdates().catch(() => undefined);
}

export function checkForUpdates(): void {
  if (!app.isPackaged) return;
  void autoUpdater.checkForUpdates().catch(() => undefined);
}
