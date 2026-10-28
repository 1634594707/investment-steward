import type { FinancialPeriodBasis } from "@investment-steward/domain-contracts";

export const FINANCIAL_PERIOD_LABELS: Record<FinancialPeriodBasis, string> = {
  cumulative: "年初至期末累计", single_quarter: "单季", ttm: "滚动十二个月（TTM）", instant: "期末时点", unknown: "未知口径",
};

export function formatDate(value: string | undefined | null): string {
  if (!value) return "等待数据";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
