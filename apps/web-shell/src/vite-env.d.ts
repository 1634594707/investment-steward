/// <reference types="vite/client" />

import type { HostBridge } from "@investment-steward/host-bridge";

declare global {
  interface Window {
    steward?: HostBridge;
    stewardWin?: {
      minimize(): Promise<void>;
      maximize(): Promise<void>;
      close(): Promise<void>;
      /** 执行具体的关闭动作（托盘隐藏或直接退出） */
      performCloseAction?(action: "tray" | "quit", remember?: boolean): Promise<void>;
      /** 获取记住的关闭动作配置 ("ask" | "tray" | "quit") */
      getRememberedCloseAction?(): Promise<"ask" | "tray" | "quit">;
      /** 保存关闭动作配置 */
      setRememberedCloseAction?(action: "ask" | "tray" | "quit"): Promise<void>;
      /** F3-3：主进程在 maximize/unmaximize 时推送窗口状态。 */
      onWindowState?(callback: (maximized: boolean) => void): void;
      /** F3-2：缩放变化（菜单加速键 / Ctrl+滚轮）后推送百分比。 */
      onZoom?(callback: (percent: number) => void): void;
    };
  }
}

export {};
