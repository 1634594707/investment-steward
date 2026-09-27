/**
 * 图表配色（共享）
 *
 * T7（用户视角路线图 2026-09-26）：此前这些逻辑只存在于 `cards/KLineCard.tsx` 里，
 * 而 `routes/workbench/panels.tsx` 的研报价格图**另写了一份**——读同一个
 * `--mkt-up/--mkt-down` 令牌，却把坐标轴文字/网格**硬编码**成深色系十六进制，
 * 且 effect 依赖数组里没有任何偏好项。结果是：
 *
 * 1. 改「红涨绿跌 / mint-amber」或换主题后，投资页 K 线会变，**已打开的研报价格图不变**；
 * 2. 浅色 `paper` 主题下，浅灰坐标轴文字配白底，基本看不见。
 *
 * 抽到本模块是让两处**共用同一实现**——分叉正是这个 bug 的成因，只改 panels 一处
 * 还会再分叉回去。
 */

/** 把 #rrggbb 转成 rgba()，非 hex 原样返回。 */
export function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  if ([r, g, b].some((n) => Number.isNaN(n))) return hex;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** F4-2：涨跌语义色从 CSS 令牌读取，随设置页「红涨绿跌 / mint-amber」即时切换。 */
export function readMarketColors(): { up: string; down: string } {
  const style = getComputedStyle(document.body);
  return {
    up: style.getPropertyValue("--mkt-up").trim() || "#e2726a",
    down: style.getPropertyValue("--mkt-down").trim() || "#5fb389",
  };
}

/** T01：坐标轴文字/网格从令牌读取，随 `body[data-theme]` 切换（浅色主题下避免暗色文字不可读）。 */
export function readChartThemeColors(): { text: string; grid: string; scaleBorder: string } {
  const style = getComputedStyle(document.body);
  const muted = style.getPropertyValue("--muted").trim() || "#9baaa3";
  const base = muted.startsWith("#") ? muted : "#9baaa3";
  return {
    text: muted,
    grid: withAlpha(base, 0.18),
    scaleBorder: withAlpha(base, 0.4),
  };
}
