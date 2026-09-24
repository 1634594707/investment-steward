import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

// C（frontend-optimization-roadmap-2026-09-12）：前端冒烟测试基建。
// 运行：pnpm --filter @investment-steward/web-shell test
export default defineConfig({
  plugins: [react()],
  root: fileURLToPath(new URL(".", import.meta.url)),
  test: {
    environment: "jsdom",
    // 冒烟测试固定「无数据通道」场景：屏蔽本机 .env.local 的 VITE_CORE_BASE，避免走 HTTP 直连分支。
    env: { VITE_CORE_BASE: "", VITE_CORE_TOKEN: "" },
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    globals: false,
  },
});
