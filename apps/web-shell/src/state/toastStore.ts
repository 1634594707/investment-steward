import { useEffect, useState } from "react";

/** 操作回执 toast（F5-1）：与通知铃职责区分 —— toast=写操作回执，通知=已确认观察条件触发。 */
export type ToastKind = "ok" | "err" | "info";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

type Listener = (items: ToastItem[]) => void;

let items: ToastItem[] = [];
const listeners = new Set<Listener>();
let seq = 0;

function emit(): void {
  const snapshot = [...items];
  listeners.forEach((listener) => listener(snapshot));
}

export function pushToast(kind: ToastKind, text: string): number {
  const id = ++seq;
  items = [...items, { id, kind, text }];
  emit();
  return id;
}

export function dismissToast(id: number): void {
  items = items.filter((item) => item.id !== id);
  emit();
}

/** 订阅 toast 队列（ToastHost 使用）。 */
export function useToasts(): ToastItem[] {
  const [snapshot, setSnapshot] = useState<ToastItem[]>(items);
  useEffect(() => {
    listeners.add(setSnapshot);
    return () => {
      listeners.delete(setSnapshot);
    };
  }, []);
  return snapshot;
}

export const toast = {
  ok: (text: string) => pushToast("ok", text),
  err: (text: string) => pushToast("err", text),
  info: (text: string) => pushToast("info", text),
};
