// A1（frontend-optimization-roadmap-2026-09-12）：extraResources 的 web 目录是合并复制，
// 旧 win-unpacked 未清理时历史 hash 文件会残留并滚入安装包（实测曾累积 270 个 JS / 33MB）。
// 打包前显式删除目的地 web 目录，保证只含本次构建产物。
const fs = require("node:fs");
const path = require("node:path");

module.exports = async function beforePack(context) {
  const webDir = path.join(context.appOutDir, "resources", "web");
  if (fs.existsSync(webDir)) {
    fs.rmSync(webDir, { recursive: true, force: true });
    console.log(`[beforePack] removed stale web bundle: ${webDir}`);
  }
};
