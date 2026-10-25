import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { LearningGoal } from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";
import { useSteward } from "./useSteward";

describe("学习活动与复盘列表同步", () => {
  it("保存后立即使用服务端活动，并读取服务端进度而非本地加一", async () => {
    const activity = { activity_id: "a1", unit_id: "unit1" };
    const goal = { goal_id: "g1", completed_count: 7 } as LearningGoal;
    const request = vi.fn().mockResolvedValueOnce({ status: 201, data: activity })
      .mockResolvedValueOnce({ status: 200, data: goal });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, false));
    let ok = false;
    await act(async () => { ok = await result.current.submitLearningActivity({
      unit_id: "unit1", objective: "练习", user_answer: "回答", reflection: "反思",
    }); });
    expect(ok).toBe(true);
    expect(result.current.activities).toEqual([activity]);
    expect(result.current.learningGoal).toEqual(goal);
    expect(request.mock.calls[1]?.[0]).toEqual({ method: "GET", path: "/learning/goals/current" });
  });

  it("保存失败不新增活动或增加进度，也不请求目标刷新", async () => {
    const request = vi.fn().mockResolvedValue({ status: 500, data: { detail: "失败" } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, false));
    const goal = { goal_id: "g1", completed_count: 2 } as LearningGoal;
    act(() => result.current.setLearningGoal(goal));
    let ok = true;
    await act(async () => { ok = await result.current.submitLearningActivity({
      unit_id: "unit1", objective: "练习", user_answer: "回答", reflection: "反思",
    }); });
    expect(ok).toBe(false);
    expect(result.current.activities).toEqual([]);
    expect(result.current.learningGoal).toEqual(goal);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("活动已保存但目标刷新失败时，活动可立即复盘且保留旧进度", async () => {
    const activity = { activity_id: "saved", unit_id: "unit1" };
    const request = vi.fn().mockResolvedValueOnce({ status: 201, data: activity })
      .mockResolvedValueOnce({ status: 503, data: { detail: "进度暂不可用" } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, false));
    const goal = { goal_id: "g1", completed_count: 2 } as LearningGoal;
    act(() => result.current.setLearningGoal(goal));
    await act(async () => {
      expect(await result.current.submitLearningActivity({ unit_id: "unit1", objective: "练习", user_answer: "回答", reflection: "反思" })).toBe(true);
    });
    expect(result.current.activities).toEqual([activity]);
    expect(result.current.learningGoal).toEqual(goal);
  });

  it("服务端返回已有活动身份时更新该条，不在复盘列表复制一条", async () => {
    const oldActivity = { activity_id: "same", unit_id: "unit1", reflection: "旧反思" };
    const savedActivity = { ...oldActivity, reflection: "服务端保存后的反思" };
    const request = vi.fn().mockResolvedValueOnce({ status: 201, data: savedActivity })
      .mockResolvedValueOnce({ status: 200, data: { goal_id: "g1", completed_count: 1 } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, false));
    act(() => result.current.setActivities([oldActivity] as unknown as typeof result.current.activities));
    await act(async () => {
      await result.current.submitLearningActivity({ unit_id: "unit1", objective: "练习", user_answer: "回答", reflection: "新反思" });
    });
    expect(result.current.activities).toEqual([savedActivity]);
  });
});
