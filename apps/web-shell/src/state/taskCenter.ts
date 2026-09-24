/**
 * D02（桌面端升级路线图 2026-09-18）：进行中任务中心——模块级注册表（与 useCoreQuery 同层，
 * 不新建数据层）。批量研报生成、方向研判等长任务在此登记进度与停止回调，StatusBar 展示
 * 可展开的任务条目与停止按钮。条目由任务自身增删；崩溃/重挂载后由任务的自愈路径重建
 * （compareJob 为模块级单例，重挂载时从快照恢复并重新登记）。
 */

export interface TaskCenterEntry {
  id: string;
  label: string;
  /** 展示用说明（如「3/6 份 · 2 份失败」或主题名）。 */
  detail?: string;
  /** 结构化进度（有则展示 N/M）。 */
  progress?: { done: number; total: number };
  /** 停止回调（无则不显示停止按钮）。 */
  stop?: () => void;
  startedAt: string;
}

const entries = new Map<string, TaskCenterEntry>();
const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((listener) => listener());
}

export function taskCenterUpsert(entry: TaskCenterEntry): void {
  entries.set(entry.id, entry);
  notify();
}

export function taskCenterRemove(id: string): void {
  if (entries.delete(id)) notify();
}

export function taskCenterSnapshot(): TaskCenterEntry[] {
  return [...entries.values()];
}

export function subscribeTaskCenter(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
