/* STEWARD 设计稿桌面预览壳
 * 用法：
 *   electron.exe apps/desktop-preview [docs/design-draft/base-shell.html]
 * 设置 STEWARD_SHOT=<png路径> 可在启动 1.5s 后自动截图（用于会话内预览）。
 * 无边框窗口 + 标题栏拖拽区由 base-shell.html 内的 .titlebar 提供，
 * 窗口控制三键通过 preload 暴露的 window.stewardWin → IPC 回到本文件。
 */
const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");

/* 从命令行找目标 HTML（跳过 -- 开关与应用目录，两种 argv 布局都兼容） */
const raw = process.argv.slice(2).filter(a => !a.startsWith("-"));
const htmlArg = raw.find(a => a.endsWith(".html"));
const TARGET = htmlArg || path.join(__dirname, "..", "..", "docs", "design-draft", "base-shell.html");
const SHOT = process.env.STEWARD_SHOT || "";

let win = null;

ipcMain.on("steward-win", (_e, cmd) => {
  if (!win) return;
  if (cmd === "minimize") win.minimize();
  else if (cmd === "maximize") (win.isMaximized() ? win.unmaximize() : win.maximize());
  else if (cmd === "close") win.close();
});

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 980,
    minHeight: 620,
    frame: false,
    backgroundColor: "#101415",
    title: "STEWARD · AI 投资管家（设计稿）",
    webPreferences: {
      preload: path.join(__dirname, "preload.js")
    }
  });
  win.loadFile(TARGET);

  /* 渲染探针：确认页面与脚本真的在渲染进程里跑起来了 */
  win.webContents.on("did-finish-load", () => {
    win.webContents.executeJavaScript(
      "document.body.className + ' | cards=' + document.querySelectorAll('.plugin-card,.plugin-row').length + ' | lanes=' + document.querySelectorAll('.lane').length"
    ).then(r => console.log("PAGE_STATE " + r)).catch(() => {});

    if (SHOT) {
      setTimeout(() => {
        win.webContents.capturePage()
          .then(img => { fs.writeFileSync(SHOT, img.toPNG()); console.log("SHOT_SAVED " + SHOT); })
          .catch(err => console.log("SHOT_FAIL " + err.message));
      }, 1500);
    }
  });
}

if (process.env.STEWARD_NO_GPU) {
  app.disableHardwareAcceleration();   /* 仅在显式指定时回退软件渲染 */
  app.commandLine.appendSwitch("disable-gpu");
  app.commandLine.appendSwitch("disable-gpu-compositing");
}
app.whenReady().then(createWindow);
app.on("window-all-closed", () => app.quit());
