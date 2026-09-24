import { beforeEach, describe, expect, it } from "vitest";
import { applyUiPrefs, DEFAULT_UI_PREFS, loadUiPrefs, saveUiPrefs, UI_PREFS_KEY } from "./uiprefs";

describe("uiprefs 主题（T01）", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
    delete document.body.dataset.density;
    delete document.body.dataset.mkt;
  });

  it("无存档时默认主题为 dark", () => {
    expect(loadUiPrefs().theme).toBe("dark");
  });

  it("旧存档（无 theme 字段）读取后回落到 dark，不抛错", () => {
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify({ density: "compact", marketColors: "legacy" }));
    const prefs = loadUiPrefs();
    expect(prefs.theme).toBe("dark");
    expect(prefs.density).toBe("compact");
    expect(prefs.marketColors).toBe("legacy");
  });

  it("graphite / paper 主题合法保存并读取", () => {
    for (const theme of ["graphite", "paper"] as const) {
      expect(saveUiPrefs({ ...DEFAULT_UI_PREFS, theme })).toBe(true);
      expect(loadUiPrefs().theme).toBe(theme);
    }
  });

  it("未知 theme 值回落到 dark", () => {
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify({ theme: "neon" }));
    expect(loadUiPrefs().theme).toBe("dark");
  });

  it("applyUiPrefs 把主题写到 :root[data-theme]（别名令牌在 :root 解析，挂 body 会停在暗色值）", () => {
    applyUiPrefs({ ...DEFAULT_UI_PREFS, theme: "paper" });
    expect(document.documentElement.dataset.theme).toBe("paper");
    expect(document.body.dataset.density).toBe("comfortable");
    expect(document.body.dataset.mkt).toBe("cn");
  });
});
