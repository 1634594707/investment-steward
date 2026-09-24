import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
// R0-1 字体本地打包（@fontsource，断网可用）：Inter Variable（界面）+ JetBrains Mono（数据/等宽）+ 思源黑体（中文）。
// 字重纪律（R0-2）：中文正文 400 / 强调 500 / 标题 600，不引入 700+（黑体加粗发糊）。
import "@fontsource-variable/inter";
import "@fontsource/manrope/400.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./fonts/fonts.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// F2-5 启动壳：index.html 内联骨架已让首帧有像素，React 挂载后移除。
requestAnimationFrame(() => {
  document.getElementById("boot-shell")?.remove();
});
