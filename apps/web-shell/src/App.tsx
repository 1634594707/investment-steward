import { ErrorBoundary } from "./components/ErrorBoundary";
import { AppShell } from "./shell/AppShell";

/**
 * 入口仅承担路由分发，具体页面与插件插槽机制见 /shell 与 /routes /cards。
 * Core 桥接与 mock 降级见 /state/coreClient.ts。
 * ErrorBoundary 兜底渲染期异常，避免整页白屏。
 */
export function App() {
  return (
    <ErrorBoundary>
      <AppShell />
    </ErrorBoundary>
  );
}