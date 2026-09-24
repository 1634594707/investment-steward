import { describe, expect, it } from "vitest";
import { DEFAULT_VIEW, NAV_ALL, SETTINGS_WORKSPACE, WORKSPACES, workspaceDefaultView, workspaceOfView, workspaceViews } from "./nav";

describe("壳层导航模型", () => {
  it("视图 → 工作区映射覆盖全部视图", () => {
    for (const entry of NAV_ALL) {
      expect(workspaceOfView(entry.id)).toBeTruthy();
      // 专业模式导航包含 PRO 页（quant），以全集校验。
      expect(workspaceViews(workspaceOfView(entry.id), true).some((nav) => nav.id === entry.id)).toBe(true);
    }
  });

  it("专业模式才出现量化研究", () => {
    const withoutPro = workspaceViews("steward", false).map((nav) => nav.id);
    const withPro = workspaceViews("steward", true).map((nav) => nav.id);
    expect(withoutPro).not.toContain("quant");
    expect(withPro).toContain("quant");
  });

  it("插件工作区默认视图与工作区一致", () => {
    for (const ws of WORKSPACES) {
      expect(workspaceDefaultView(ws.id)).toBeTruthy();
      expect(workspaceOfView(workspaceDefaultView(ws.id))).toBe(ws.id);
    }
    expect(workspaceOfView(workspaceDefaultView(SETTINGS_WORKSPACE.id))).toBe("settings");
  });

  it("默认视图合法且导航 id 无重复", () => {
    const ids = NAV_ALL.map((nav) => nav.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(NAV_ALL.some((nav) => nav.id === DEFAULT_VIEW)).toBe(true);
  });

  // P3-D01：游资雷达工作区。上面两条断言已覆盖「视图→工作区往返」与「id 无重复」，
  // 这里再钉住插件绑定，避免入口在插件未启用时也显示。
  it("游资雷达工作区绑定 official.youzi-radar 插件", () => {
    const youzi = WORKSPACES.find((ws) => ws.id === "youzi");
    expect(youzi).toBeTruthy();
    expect(youzi?.pluginId).toBe("official.youzi-radar");
    expect(workspaceDefaultView("youzi")).toBe("youzi");
    expect(workspaceOfView("youzi")).toBe("youzi");
    expect(workspaceViews("youzi", true).map((nav) => nav.id)).toEqual(["youzi"]);
  });

  it("游资雷达不在投资管家主工作区的默认导航里", () => {
    // 独立应用页不应混进 steward 侧栏（否则插件没启用也会出现入口）。
    expect(workspaceViews("steward", true).map((nav) => nav.id)).not.toContain("youzi");
  });
});
