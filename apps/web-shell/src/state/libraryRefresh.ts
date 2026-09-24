/**
 * A01（前端设计与架构优化任务路线图 2026-09-19）：书架重载信号注册表——模块级，与 taskCenter/toastStore 同层，
 * 不引全局状态库。此前插件安装后的刷新指向壳层那份没有消费者的 useLibrary 实例（方案 E10），
 * 用户看到的书架（LibraryPage 自建实例）不在重载路径上。改为事件方调用 requestLibraryReload()，
 * 由唯一的书架所有者订阅；页面尚未挂载时信号丢弃即可——书架在挂载时自取，不存在漏刷。
 */

const listeners = new Set<() => void>();

export function requestLibraryReload(): void {
  listeners.forEach((listener) => listener());
}

export function subscribeLibraryReload(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
