/* 把窗口控制暴露给渲染进程（base-shell.html 标题栏三键调用）。
 * 沙箱 preload 下仅允许 require('electron') 的 contextBridge/ipcRenderer 子集。 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("stewardWin", {
  minimize: () => ipcRenderer.send("steward-win", "minimize"),
  maximize: () => ipcRenderer.send("steward-win", "maximize"),
  close:    () => ipcRenderer.send("steward-win", "close")
});
