import { useEffect, useRef, useState } from "react";
import { dismissToast, useToasts } from "../state/toastStore";

const AUTO_DISMISS_MS: Record<string, number> = { ok: 3500, info: 4000, err: 6000 };

/**
 * 全局 toast 宿主（F5-1）：右下角操作回执；悬停暂停自动消退，鼠标移出后重新计时。
 * 与通知铃职责区分 —— toast=写操作回执，通知=已确认观察条件触发。
 */
export function ToastHost() {
  const toasts = useToasts();
  const [paused, setPaused] = useState(false);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    for (const item of toasts) {
      if (timers.current.has(item.id) || paused) continue;
      const timer = setTimeout(() => {
        timers.current.delete(item.id);
        dismissToast(item.id);
      }, AUTO_DISMISS_MS[item.kind] ?? 4000);
      timers.current.set(item.id, timer);
    }
    for (const [id, timer] of timers.current) {
      if (paused && !toasts.some((item) => item.id === id)) continue;
      if (paused) {
        clearTimeout(timer);
        timers.current.delete(id);
      }
    }
  }, [toasts, paused]);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="status" aria-live="polite" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      {toasts.map((item) => (
        <div className={`toast ${item.kind}`} key={item.id}>
          <span className="toast-dot" aria-hidden="true" />
          <p>{item.text}</p>
          <button className="toast-close" aria-label="关闭提示" onClick={() => dismissToast(item.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
