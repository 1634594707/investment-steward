/**
 * T3（用户视角路线图 2026-09-26）：托盘态下新通知要能弹系统通知，且不得变成打扰。
 *
 * 改造前托盘模式**完全收不到提醒**：`useCorePoll` 在 `document.visibilityState` 隐藏时
 * 整轮跳过，而宿主早已具备原生通知能力（`preload.ts` 的 `showNotification` →
 * `host:notify` → Electron `Notification.show()`）却从无调用方。设置页承诺的
 * 「保留到托盘 · 后台守护 · 保持宏观预警」因此落空。
 */
import { describe, expect, it } from "vitest";
import {
  isQuietHoursNow,
  shouldShowSystemNotification,
  toMinutes,
} from "./useNotifications";

const item = { notification_id: "n1", title: "标题", summary: "摘要" } as never;

describe("shouldShowSystemNotification · 四条判定缺一不可", () => {
  const base = { item, firstLoad: false, alreadyNotified: false, documentHidden: true, quiet: false };

  it("托盘态下的新通知要弹", () => {
    expect(shouldShowSystemNotification(base)).toBe(true);
  });

  it("窗口可见时不弹——用户正看着应用，弹系统通知纯属重复", () => {
    expect(shouldShowSystemNotification({ ...base, documentHidden: false })).toBe(false);
  });

  it("首轮不弹——否则每次启动都会把积压的历史通知一次性弹一遍", () => {
    expect(shouldShowSystemNotification({ ...base, firstLoad: true })).toBe(false);
  });

  it("同一条不重复弹", () => {
    expect(shouldShowSystemNotification({ ...base, alreadyNotified: true })).toBe(false);
  });

  it("免打扰时段内不弹", () => {
    expect(shouldShowSystemNotification({ ...base, quiet: true })).toBe(false);
  });
});

describe("toMinutes", () => {
  it("解析 HH:MM", () => {
    expect(toMinutes("22:00")).toBe(1320);
    expect(toMinutes("00:00")).toBe(0);
    expect(toMinutes("08:30")).toBe(510);
  });

  it("非法输入返回 null（不静默当作有效时段）", () => {
    for (const bad of ["", "24:00", "7:00", "22:60", "abc", "22:0"]) {
      expect(toMinutes(bad)).toBeNull();
    }
  });
});

describe("isQuietHoursNow · 与 Core 侧免打扰口径一致", () => {
  const at = (h: number, m: number) => new Date(2026, 8, 26, h, m);

  it("未启用时永不命中", () => {
    expect(isQuietHoursNow({ quiet_hours_enabled: false, quiet_start: "22:00", quiet_end: "08:00" }, at(23, 0))).toBe(false);
  });

  it("跨零点时段（22:00→08:00）两段都命中", () => {
    const prefs = { quiet_hours_enabled: true, quiet_start: "22:00", quiet_end: "08:00" };
    expect(isQuietHoursNow(prefs, at(23, 0))).toBe(true);
    expect(isQuietHoursNow(prefs, at(2, 30))).toBe(true);
    expect(isQuietHoursNow(prefs, at(7, 59))).toBe(true);
    expect(isQuietHoursNow(prefs, at(8, 0))).toBe(false);
    expect(isQuietHoursNow(prefs, at(12, 0))).toBe(false);
    expect(isQuietHoursNow(prefs, at(21, 59))).toBe(false);
  });

  it("不跨零点时段按单段判断", () => {
    const prefs = { quiet_hours_enabled: true, quiet_start: "09:00", quiet_end: "12:00" };
    expect(isQuietHoursNow(prefs, at(10, 0))).toBe(true);
    expect(isQuietHoursNow(prefs, at(12, 0))).toBe(false);
    expect(isQuietHoursNow(prefs, at(8, 0))).toBe(false);
  });

  it("时段配置非法时按「不在免打扰」处理——宁可多提醒，不静默吞掉", () => {
    const prefs = { quiet_hours_enabled: true, quiet_start: "99:99", quiet_end: "08:00" };
    expect(isQuietHoursNow(prefs, at(2, 0))).toBe(false);
  });
});
