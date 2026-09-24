import type { CSSProperties } from "react";

/**
 * 骨架屏（F2-1）：线 / 块 / 卡三形态占位，样式带 shimmer；
 * `prefers-reduced-motion` 下动画被全局禁用，退化为静态色块（布局形状不变，CLS≈0）。
 */
export function Skeleton({ kind = "line", width, style }: { kind?: "line" | "block" | "card"; width?: string; style?: CSSProperties }) {
  return <span className={`skeleton sk-${kind}`} style={width ? { width, ...style } : style} aria-hidden="true" />;
}

/** 多行线形占位：用于文字内容加载中（最后一行收窄，避免「齐刷刷」的机械感）。 */
export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  return (
    <div className="slot-skeleton">
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton key={index} kind="line" width={index === lines - 1 ? "60%" : index % 2 === 0 ? "80%" : undefined} />
      ))}
    </div>
  );
}
