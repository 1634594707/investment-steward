/**
 * T7（用户视角路线图 2026-09-26）：图表配色必须随主题/涨跌色切换，且浅色主题下可读。
 *
 * 改造前 `routes/workbench/panels.tsx` 的研报价格图**另写了一份**配色：涨跌色读令牌
 * （这部分对），但坐标轴文字/网格**硬编码**成深色系（`#7b828b` / `rgba(231,236,233,…)`），
 * 且 effect 依赖数组只有 `[chart]`。结果是改设置后这张图不重建、浅色主题下坐标轴
 * 文字等于看不见。现已抽到 `cards/chartTheme.ts` 与 K 线卡共用。
 */
import { describe, expect, it } from "vitest";
import { readChartThemeColors, readMarketColors, withAlpha } from "./chartTheme";

/** 注入 CSS 变量，模拟某个主题下的 :root/body 计算值。 */
function stubTokens(tokens: Record<string, string>) {
  const original = document.body.style;
  for (const [key, value] of Object.entries(tokens)) {
    document.body.style.setProperty(key, value);
  }
  return () => {
    for (const key of Object.keys(tokens)) document.body.style.removeProperty(key);
    void original;
  };
}

describe("withAlpha", () => {
  it("#rrggbb → rgba()", () => {
    expect(withAlpha("#7b828b", 0.18)).toBe("rgba(123, 130, 139, 0.18)");
    expect(withAlpha("#ffffff", 0.4)).toBe("rgba(255, 255, 255, 0.4)");
  });

  it("非 hex 原样返回（不吞、不瞎猜）", () => {
    expect(withAlpha("rgba(1,2,3,.5)", 0.2)).toBe("rgba(1,2,3,.5)");
    expect(withAlpha("nope", 0.2)).toBe("nope");
  });
});

describe("readMarketColors · 涨跌色随设置切换", () => {
  it("读 --mkt-up / --mkt-down 令牌", () => {
    const restore = stubTokens({ "--mkt-up": "#d92b2b", "--mkt-down": "#1a9e5f" });
    expect(readMarketColors()).toEqual({ up: "#d92b2b", down: "#1a9e5f" });
    restore();
  });

  it("令牌缺失时回退到默认红涨绿跌", () => {
    const restore = stubTokens({});
    const colors = readMarketColors();
    expect(colors.up).toBe("#e2726a");
    expect(colors.down).toBe("#5fb389");
    restore();
  });
});

describe("readChartThemeColors · T7 的核心：浅色主题下坐标轴可读", () => {
  it("暗色主题：--muted 为浅色，坐标轴文字取该浅色", () => {
    const restore = stubTokens({ "--muted": "#9baaa3" });
    const theme = readChartThemeColors();
    expect(theme.text).toBe("#9baaa3");
    expect(theme.grid).toBe("rgba(155, 170, 163, 0.18)");
    restore();
  });

  it("浅色主题：--muted 为深色，坐标轴文字随之变深——不再出现「浅灰配白底」", () => {
    const restore = stubTokens({ "--muted": "#5b6660" });
    const theme = readChartThemeColors();
    restore();

    // 旧实现写死 #7b828b（浅灰），在白底上几乎不可见。
    const luminance = (hex: string) => {
      const v = hex.replace("#", "");
      const r = parseInt(v.slice(0, 2), 16);
      const g = parseInt(v.slice(2, 4), 16);
      const b = parseInt(v.slice(4, 6), 16);
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    expect(theme.text).toBe("#5b6660");
    expect(luminance(theme.text)).toBeLessThan(160); // 白底上属深色，对比度够
  });

  it("两套主题下网格/边框都由 --muted 派生，不含硬编码深色常量", () => {
    const restore = stubTokens({ "--muted": "#333333" });
    const theme = readChartThemeColors();
    restore();
    expect(theme.grid).toBe("rgba(51, 51, 51, 0.18)");
    expect(theme.scaleBorder).toBe("rgba(51, 51, 51, 0.4)");
  });

  it("令牌缺失或非 hex 时回退到默认灰，不产生非法颜色串", () => {
    const restore = stubTokens({ "--muted": "" });
    const theme = readChartThemeColors();
    restore();
    expect(theme.grid).toMatch(/^rgba\(\d+, \d+, \d+, [\d.]+\)$/);
  });
});
