import { formatDate } from "../state/format";

interface Props {
  asOf: string | null | undefined;
  isDemo?: boolean;
}

/** 顶栏数据新鲜度：时间来自数据本身，不写死。 */
export function FreshnessIndicator({ asOf, isDemo }: Props) {
  const text = asOf ? formatDate(asOf) : "暂无数据";
  return (
    <div className="as-of">
      数据截至 {text}
      {isDemo && <span className="dot-gray"> · 演示数据</span>}
    </div>
  );
}