import { useEffect, useState } from "react";

/** 桌面端缩放档（F3-2）：Host 菜单 Ctrl+=/-/0 缩放后经 preload 派发百分比事件，状态栏显示。 */
export function useZoomPercent(): number | null {
  const [percent, setPercent] = useState<number | null>(null);
  useEffect(() => {
    function onZoom(event: Event) {
      const detail = (event as CustomEvent<number>).detail;
      if (typeof detail === "number") setPercent(Math.round(detail));
    }
    window.addEventListener("steward:zoom", onZoom);
    return () => window.removeEventListener("steward:zoom", onZoom);
  }, []);
  return percent;
}
