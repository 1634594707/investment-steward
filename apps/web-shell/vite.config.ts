import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { visualizer } from "rollup-plugin-visualizer";
import { fileURLToPath } from "node:url";

// 开发态可选：`VITE_DEV_PROXY=http://127.0.0.1:<core端口>` 时，把 `/api/*` 前缀代理到真实 Core，
// 便于在浏览器里走查直连模式（coreClient 的 VITE_CORE_BASE 相应填 `http://127.0.0.1:<vite端口>/api`，
// 规避浏览器对非 vite 本地端口的跨域/隔离限制）。默认关闭，不影响生产构建。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL(".", import.meta.url)), "");
  const devProxyTarget = env.VITE_DEV_PROXY;
  return {
    plugins: [
      react(),
      // C（frontend-optimization-roadmap-2026-09-12）：`pnpm build:analyze` 时产出体积报告（staging 模式下启用）。
      ...(mode === "staging"
        ? [visualizer({ filename: "dist/stats.html", gzipSize: true, brotliSize: true })]
        : []),
    ],
    base: "./",
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      ...(devProxyTarget
        ? { proxy: { "/api": { target: devProxyTarget, rewrite: (url) => url.replace(/^\/api/, "") } } }
        : {}),
    },
    build: {
      target: "es2022",
      // A1（frontend-optimization-roadmap-2026-09-12）：显式清空 outDir。
      // outDir 在项目根内时 Vite 默认会清理，但此处分发链（electron-builder extraResources 合并复制）
      // 依赖 dist 每次只含单次构建产物，一旦残留 hash 文件会一路带进安装包（实测曾累积 270 个 JS）。
      emptyOutDir: true,
      // §9：生产包不携带 source map（EXE 内无 devtools 调试场景，map 曾占 assets ~45%）；
      // 需要排错时本地跑 `vite build --mode staging` 可得 map。
      sourcemap: mode !== "production",
      manifest: true,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("/node_modules/echarts/") || id.includes("/node_modules/zrender/")) return "macro-chart";
          },
        },
      },
    },
  };
});
