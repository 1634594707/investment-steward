/**
 * B03（桌面端升级路线图 2026-09-18）回归：Ctrl+数字 的落点必须由导航派生，
 * 且速查面板文案与映射同源。
 * 修复前的事实：PRIMARY_NAV 已插入「研究工作台」（nav.ts:18），而手写映射
 * `["today","investment","research","review"]` 未同步 → Ctrl+3 跳到侧栏第 4 项、
 * 工作台按不到；面板文案还停留在「今天 / 我的投资 / 研究 / 复盘」。
 */
import { describe, expect, it } from "vitest";
import { APPS_NAV, PRIMARY_NAV } from "../shell/nav";
import { DIGIT_NAV_VIEWS, SHORTCUTS, navShortcutRows, viewForDigit, viewPluginId } from "./shortcuts";

const ALL_REACHABLE = new Set([
  "official.reading-library",
  "official.macro-radar",
  "official.stock-tactics",
  "official.youzi-radar",
]);

describe("B03（桌面端升级路线图 2026-09-18）数字键落点与导航同源", () => {
  it("前 5 个数字就是一级导航，顺序与侧栏完全一致", () => {
    expect(DIGIT_NAV_VIEWS.slice(0, PRIMARY_NAV.length)).toEqual(PRIMARY_NAV.map((entry) => entry.id));
    expect(viewForDigit(3, { enabledPluginIds: ALL_REACHABLE, proMode: true })).toBe("workbench");
  });

  it("一级导航之后接独立应用区（APPS_NAV 顺序）", () => {
    expect(DIGIT_NAV_VIEWS.slice(PRIMARY_NAV.length)).toEqual(APPS_NAV.map((entry) => entry.id));
  });

  it("插件未启用时对应数字不跳转（不跳错页也不跳空页）", () => {
    const macroDigit = DIGIT_NAV_VIEWS.indexOf("macro") + 1;
    expect(viewPluginId("macro")).toBe("official.macro-radar");
    expect(viewForDigit(macroDigit, { enabledPluginIds: new Set<string>(), proMode: true })).toBeNull();
    expect(viewForDigit(macroDigit, { enabledPluginIds: ALL_REACHABLE, proMode: true })).toBe("macro");
  });

  it("越界数字返回 null", () => {
    for (const digit of [0, DIGIT_NAV_VIEWS.length + 1, 99]) {
      expect(viewForDigit(digit, { enabledPluginIds: ALL_REACHABLE, proMode: true })).toBeNull();
    }
  });

  it("速查面板的导航行由映射生成，且 keys 唯一（面板以 keys 作 React key）", () => {
    const rows = navShortcutRows();
    expect(rows).toHaveLength(DIGIT_NAV_VIEWS.length);
    expect(rows[0]?.keys).toBe("Ctrl 1");
    expect(rows[0]?.action).toContain("今天");
    const keys = SHORTCUTS.map((item) => item.keys);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).not.toContain("Ctrl 1 / 2 / 3 / 4");
  });
});
