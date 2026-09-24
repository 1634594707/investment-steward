// A1（frontend-optimization-roadmap-2026-09-12）：打包后断言 web 资源未携带历史残留。
// 单次干净构建的 JS chunk 数量 ~15（入口 + 路由懒加载 + macro-chart + 共享件）；
// 阈值放宽到 30 以容纳合理的 chunk 演进，超出即判定清理机制失效，构建失败。
const fs = require("node:fs");
const path = require("node:path");

const MAX_JS_FILES = 30;

module.exports = async function afterPack(context) {
  // Only verification material belongs in a distributable, never publisher secrets.
  const keysDir = path.join(context.appOutDir, "resources", "keys");
  const keyFiles = fs.existsSync(keysDir) ? fs.readdirSync(keysDir) : [];
  const publicKey = "steward-plugin-publishing.pub.pem";
  if (keyFiles.length !== 1 || keyFiles[0] !== publicKey) {
    throw new Error("[afterPack] keys must contain only the publisher public key");
  }
  const keyText = fs.readFileSync(path.join(keysDir, publicKey), "utf8");
  if (!keyText.includes("-----BEGIN PUBLIC KEY-----") || keyText.includes("PRIVATE KEY")) {
    throw new Error("[afterPack] invalid public verification key in package");
  }

  const assetsDir = path.join(context.appOutDir, "resources", "web", "assets");
  if (!fs.existsSync(assetsDir)) {
    throw new Error(`[afterPack] web assets 目录缺失：${assetsDir}（extraResources 复制失败）`);
  }
  const files = fs.readdirSync(assetsDir);
  const jsFiles = files.filter((name) => name.endsWith(".js"));
  const cssFiles = files.filter((name) => name.endsWith(".css"));
  if (jsFiles.length > MAX_JS_FILES) {
    throw new Error(
      `[afterPack] web assets 含 ${jsFiles.length} 个 JS 文件（阈值 ${MAX_JS_FILES}），疑似历史构建残留未清理。`,
    );
  }
  console.log(`[afterPack] web assets 校验通过：${jsFiles.length} JS / ${cssFiles.length} CSS`);
};
