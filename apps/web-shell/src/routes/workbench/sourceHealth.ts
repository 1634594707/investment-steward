import type { ReportSourceHealth } from "./researchTypes";
import { formatTimestamp } from "./format";

export const SOURCE_HEALTH_LABELS = {
  failed: "取数失败", stale: "观测日期超过阈值",
  unknown_date: "日期无法核验", within_threshold: "观测日期在阈值内",
};
export const SOURCE_HEALTH_UNAVAILABLE = "缺少来源健康记录，无法核验数据时效。";

/** 页面与导出沿用服务端判定，只格式化日期，不在客户端重判时效。 */
export function sourceHealthDetails(row: ReportSourceHealth["sources"][number]): string {
  return `观测日期：${row.data_date || "未知"} · 时效阈值：${row.max_age_days} 天`
    + ` · 采集时间：${row.retrieved_at ? formatTimestamp(row.retrieved_at) : "未知"}`
    + ` · 缓存年龄：${row.cache_age_days === null ? "未知" : `${row.cache_age_days} 天`}`;
}
