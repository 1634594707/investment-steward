/**
 * K06（2026-09-18 排版与研报呈现一致性路线图）：宽度档单一来源。
 *
 * 旧问题：JS 窄态（AppShell 的 `narrow`，<1024/≥1064 迟滞）与 CSS `@media`（散落在 720/920/1180/1380…）
 * 两套并行、阈值不一致 → 「壳已窄、内容仍多列」。这里把宽度档收敛成一套边界，并用
 * `body[data-width-tier]` 暴露给 CSS，页面按档响应（同 AppShell 的 narrow 共用同一个 innerWidth 读数）。
 *
 * 四档边界（路线图 §K06）：720 / 1024 / 1280 / 1600 → 五档 xs/sm/md/lg/xl。
 * sm 上界 = 1024，正好对齐 AppShell 进入窄态的阈值，消除两套并行的错位。
 */
export const WIDTH_BREAKPOINTS = { sm: 720, md: 1024, lg: 1280, xl: 1600 } as const;

export type WidthTier = "xs" | "sm" | "md" | "lg" | "xl";

/** 纯函数：视口宽度 → 宽度档（左闭右开：w<720=xs，720≤w<1024=sm，…，w≥1600=xl）。 */
export function tierForWidth(width: number): WidthTier {
  if (width < WIDTH_BREAKPOINTS.sm) return "xs";
  if (width < WIDTH_BREAKPOINTS.md) return "sm";
  if (width < WIDTH_BREAKPOINTS.lg) return "md";
  if (width < WIDTH_BREAKPOINTS.xl) return "lg";
  return "xl";
}

/** 把当前宽度的档位写到 `body[data-width-tier]`，供 CSS 按档响应。返回本次档位（便于测试/联动）。 */
export function applyWidthTier(width: number): WidthTier {
  const tier = tierForWidth(width);
  document.body.dataset.widthTier = tier;
  return tier;
}
