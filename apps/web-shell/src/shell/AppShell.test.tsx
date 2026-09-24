import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppShell } from "./AppShell";

/**
 * C（frontend-optimization-roadmap-2026-09-12）：AppShell 整壳挂载冒烟。
 * jsdom 无 window.steward 桥、无 VITE_CORE_BASE：AppShell 应回落启动指引（不白屏、不伪造数据），
 * 且壳层导航（工作区切换 / 命令面板）可用。
 */
describe("AppShell 整壳冒烟（无数据通道）", () => {
  it("渲染壳层 chrome 与启动指引，不白屏", async () => {
    render(<AppShell />);
    // booting 骨架在 loadAll 判定无通道后落定，启动指引随后出现。
    expect(await screen.findByRole("note", { name: "启动指引" })).toBeInTheDocument();
    expect(screen.getByText("未检测到数据通道")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "工作区切换" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "最小化" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭" })).toBeInTheDocument();
  });

  it("Ctrl+K 打开命令面板，Escape 逐层关闭", async () => {
    const user = userEvent.setup();
    render(<AppShell />);
    await user.keyboard("{Control>}k{/Control}");
    expect(document.querySelector(".palette-overlay")).not.toBeNull();
    await user.keyboard("{Escape}");
    expect(document.querySelector(".palette-overlay")).toBeNull();
  });
});
