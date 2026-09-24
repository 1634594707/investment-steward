import { useEffect, useState } from "react";

/** 界面偏好（F4-3 密度 / F4-2 涨跌语义色，D-F1；T01 主题，2026-09-20）：仅存本机 localStorage，同 profile.ts 模式。 */
export type Density = "comfortable" | "compact";
export type MarketColors = "cn" | "legacy";
/** T01：dark = 暗色·墨绿（默认，原视觉）；graphite = 暗色·石墨；paper = 浅色·日间。 */
export type Theme = "dark" | "graphite" | "paper";

export interface UiPrefs {
  density: Density;
  marketColors: MarketColors;
  theme: Theme;
}

export const UI_PREFS_KEY = "steward.ui-prefs";
const UI_PREFS_EVENT = "steward-ui-prefs-changed";

export const DEFAULT_UI_PREFS: Readonly<UiPrefs> = { density: "comfortable", marketColors: "cn", theme: "dark" };

export function loadUiPrefs(): UiPrefs {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(UI_PREFS_KEY) ?? "");
    if (raw && typeof raw === "object") {
      const obj = raw as Record<string, unknown>;
      return {
        density: obj.density === "compact" ? "compact" : "comfortable",
        // D-F1 默认「红涨绿跌」（A 股惯例）；legacy = mint 涨 / amber 跌。
        marketColors: obj.marketColors === "legacy" ? "legacy" : "cn",
        theme: obj.theme === "graphite" || obj.theme === "paper" ? obj.theme : "dark",
      };
    }
  } catch {
    /* 首次使用无存档 */
  }
  return { ...DEFAULT_UI_PREFS };
}

/** 把偏好落到 dataset（CSS 侧 body[data-density] / body[data-mkt] 消费）。
 *  主题必须写在 documentElement 上：`--text: var(--text-strong)` 这类别名令牌声明在 :root，
 *  自定义属性在声明它的元素上完成替换，写在 body 上的主题会让别名停在暗色值（T02 修复）。 */
export function applyUiPrefs(prefs: UiPrefs): void {
  document.body.dataset.density = prefs.density;
  document.body.dataset.mkt = prefs.marketColors;
  document.documentElement.dataset.theme = prefs.theme;
}

export function saveUiPrefs(next: UiPrefs): boolean {
  try {
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify(next));
  } catch {
    return false;
  }
  applyUiPrefs(next);
  window.dispatchEvent(new CustomEvent(UI_PREFS_EVENT));
  return true;
}

export function useUiPrefs(): UiPrefs {
  const [prefs, setPrefs] = useState<UiPrefs>(loadUiPrefs);
  useEffect(() => {
    applyUiPrefs(loadUiPrefs());
    const sync = () => setPrefs(loadUiPrefs());
    window.addEventListener(UI_PREFS_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(UI_PREFS_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return prefs;
}
