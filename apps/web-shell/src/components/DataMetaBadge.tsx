/** M2-01 数据可信度徽标（v21）：来源 + 数据时点 + 新鲜度三要素，一行呈现。
 *
 *  新鲜度语义：
 *  - realtime：本机实拉（FRED / Comtrade 等真实端点本次取得）；
 *  - partial：部分序列实拉成功，其余降级（诚实标注，不抹平）；
 *  - static：静态实测回退值（带实测日期，非实时）；
 *  - demo：演示数据（非真实行情）；
 *  - unknown：元数据缺失——如实标注「时点未知」，不编造。
 */

export type DataFreshness = "realtime" | "partial" | "static" | "demo" | "unknown";

const FRESHNESS_LABEL: Record<DataFreshness, string> = {
  realtime: "实时",
  partial: "部分实拉",
  static: "静态回退",
  demo: "演示",
  unknown: "时点未知",
};

export function DataMetaBadge({ source, asOf, freshness }: { source: string; asOf?: string | null; freshness: DataFreshness }) {
  const label = FRESHNESS_LABEL[freshness];
  return (
    <span
      className={`data-meta-badge ${freshness}`}
      title={`来源 ${source}${asOf ? ` · 数据时点 ${asOf}` : ""} · ${label}`}
    >
      <i aria-hidden="true" />
      {source} · {asOf ?? "时点未知"} · {label}
    </span>
  );
}
