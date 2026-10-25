/**
 * D3（研报产出与交付链路路线图 2026-09-27）：研报重点标注三档色值的**唯一来源**。
 *
 * 背景：同一套「蓝=关键结论 / 琥珀=待核验 / 珊瑚红=无来源支撑」的语义色，上一版在两个地方
 * 各维护一份——屏显走 `styles.css` 的 `--rpt-hl-*` token（随主题明暗变化），导出件在
 * `export.tsx` 里内联三个 hex（`#1d4ed8` / `#8a5a00` / `#b3261e`）。改一处忘另一处 =
 * 屏显与导出颜色不一致，而**浅色主题的 AA 对比度（README 承诺实测 ≥4.5:1）只在一处被验证**。
 *
 * 现在导出侧的三档色值统一由本模块派生（`reportHighlightCss()` 生成 `<style>` 规则）；
 * 屏显侧继续消费 `styles.css` 的同名 token——两者语义与命名在此对齐，导出侧不再有第二份硬编码。
 *
 * 注意：两套值**刻意不同**，不是重复定义——屏显要跟随明暗主题（`--blue` 在暗色主题是
 * `#8cb6e7`、浅色主题是 `#2b5f9e`），而导出件是固定白底，必须用一套深色值保证对比度。
 */

export interface ReportHighlightToneMeta {
  /** 屏显色（styles.css 的 `--rpt-hl-*` token，随主题变化）。 */
  screen: string;
  /** 导出件色值（固定白底，对比度 ≥4.5:1）。 */
  print: string;
  /** 语义标签（每种颜色必须同时有文字表达，§3.2）。 */
  label: string;
}

export const REPORT_HIGHLIGHT_TONES = {
  blue: { screen: "var(--rpt-hl-blue)", print: "#1d4ed8", label: "关键结论" },
  amber: { screen: "var(--rpt-hl-amber)", print: "#8a5a00", label: "待核验" },
  coral: { screen: "var(--rpt-hl-coral)", print: "#b3261e", label: "无来源支撑" },
} as const satisfies Record<string, ReportHighlightToneMeta>;

export type ReportHighlightTone = keyof typeof REPORT_HIGHLIGHT_TONES;

/**
 * 导出件 `<style>` 里的三档色值规则（由常量生成——改色值只改上面一处）。
 * 行内 `mark.rpt-hl { background: transparent; font-weight: 700 }` 由调用方负责。
 */
export function reportHighlightCss(): string {
  return (Object.keys(REPORT_HIGHLIGHT_TONES) as ReportHighlightTone[])
    .map((tone) => `  mark.rpt-hl-${tone} { color: ${REPORT_HIGHLIGHT_TONES[tone].print}; }`)
    .join("\n");
}
