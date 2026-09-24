import { beforeEach, describe, expect, it } from "vitest";
import { subscribeTaskCenter, taskCenterRemove, taskCenterSnapshot, taskCenterUpsert } from "./taskCenter";

/** D02（桌面端升级路线图 2026-09-18）：任务中心注册表语义（upsert/remove/snapshot/订阅）。 */
describe("taskCenter（D02）", () => {
  beforeEach(() => {
    taskCenterSnapshot().forEach((entry) => taskCenterRemove(entry.id));
  });

  it("upsert 新增与同 id 更新（进度推进不产生重复条目）", () => {
    const seen: number[] = [];
    const off = subscribeTaskCenter(() => seen.push(taskCenterSnapshot().length));
    taskCenterUpsert({ id: "t1", label: "批量研报生成", progress: { done: 0, total: 6 }, startedAt: "2026-09-19T00:00:00Z" });
    taskCenterUpsert({ id: "t1", label: "批量研报生成", progress: { done: 3, total: 6 }, startedAt: "2026-09-19T00:00:00Z" });
    expect(taskCenterSnapshot()).toHaveLength(1);
    expect(taskCenterSnapshot()[0]?.progress).toEqual({ done: 3, total: 6 });
    expect(seen).toEqual([1, 1]);
    off();
  });

  it("remove 移除条目并通知；不存在时不通知", () => {
    taskCenterUpsert({ id: "t2", label: "方向研判生成", startedAt: "x" });
    let notified = 0;
    const off = subscribeTaskCenter(() => {
      notified += 1;
    });
    taskCenterRemove("t2");
    taskCenterRemove("t2");
    off();
    expect(taskCenterSnapshot()).toHaveLength(0);
    expect(notified).toBe(1);
  });
});
