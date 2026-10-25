/**
 * A02（前端设计与架构优化任务路线图 2026-09-19）：启动分层的运行验收。
 *
 * 口径说明：这里跑的是**真实的壳层启动路径**（AppShell → loadAll/loadDeferred → coreClient 的 HTTP 分支
 * → fetch），但环境是 jsdom + 受控 fetch，不是桌面宿主。因此本文件证明的是行为
 * （延后批次不阻塞可交互、局部失败有局部提示、恢复后刷新），**不证明真实耗时**——
 * 桌面端的启动到可交互耗时与截图仍按 V01 单独取。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AppShell } from "./AppShell";
import { SCHEMA_VERSION, type LearningActivity, type LearningGoal, type LearningUnit } from "@investment-steward/domain-contracts";

/** 单对象端点返回 null（列表端点返回 [] 才是安全默认）。 */
const OBJECT_PATHS = new Set([
  "/overview", "/personal/settings", "/investor/profile", "/brief/today", "/review/weekly",
  "/learning/unit/today", "/learning/goals/current", "/research/questions/latest", "/quant/artifacts",
]);

/** 延后批次（loadDeferred）里的代表路径——首批 10 项不含它们，用来判定分层是否真的分开。 */
const DEFERRED_SAMPLE = ["/brief/today", "/research/runs", "/model-profiles"];

function pathOf(input: RequestInfo | URL): string {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  return new URL(url).pathname;
}

function respond(status: number, body: unknown): Response {
  return {
    ok: status < 400,
    status,
    text: async () => (body === null ? "" : JSON.stringify(body)),
  } as Response;
}

interface Harness {
  calls: string[];
  /** 延后批次的放行阀门：未放行时这些请求保持在途。 */
  releaseDeferred: () => void;
}

function installChannel(options: { gateDeferred: boolean; failPaths?: string[] }): Harness {
  const calls: string[] = [];
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const failedOnce = new Set(options.failPaths ?? []);
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const path = pathOf(input);
    calls.push(path);
    if (failedOnce.has(path)) {
      failedOnce.delete(path); // 只失败一次：随后的「重试加载」应恢复正常
      return respond(500, { detail: "core 暂时不可用" });
    }
    if (options.gateDeferred && DEFERRED_SAMPLE.includes(path)) await gate;
    if (path === "/portfolio/risk" || path === "/research/templates") return respond(503, { detail: "此测试未提供该对象接口数据" });
    return respond(200, OBJECT_PATHS.has(path) ? null : []);
  }) as unknown as typeof globalThis.fetch;
  return { calls, releaseDeferred: () => release() };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("AppShell 启动分层（A02 · 前端设计与架构优化任务路线图 2026-09-19）", () => {
  it.each([
    { saveStatus: 201, goalStatus: 200 },
    { saveStatus: 201, goalStatus: 503 },
    { saveStatus: 500, goalStatus: 200 },
  ])("学习保存后直接复盘，不依赖刷新：保存 $saveStatus，进度 $goalStatus", async ({ saveStatus, goalStatus }) => {
    installChannel({ gateDeferred: false });
    localStorage.setItem("steward.onboarding-deferred-at", String(Date.now()));
    const baseFetch = globalThis.fetch;
    const unit: LearningUnit = {
      unit_id: "unit1", user_id: "user1", unit_type: "exercise", title: "本次练习", objective: "检验判断依据",
      content: "练习内容", guidance: "练习指导", related_conditions: [], source_plugin_id: "test",
      source_plugin_release: "test", data_time: "2026-10-25", schema_version: SCHEMA_VERSION,
    };
    const goal: LearningGoal = {
      goal_id: "goal1", user_id: "user1", period_label: "本周", target_count: 10, completed_count: 2,
      created_at: "2026-10-25T00:00:00Z", updated_at: "2026-10-25T00:00:00Z", schema_version: SCHEMA_VERSION,
    };
    const activity: LearningActivity = {
      activity_id: "activity1", user_id: unit.user_id, unit_id: unit.unit_id, unit_type: unit.unit_type,
      objective: unit.objective, user_answer: "先核对来源", reflection: "下次核对期间",
      completed_at: "2026-10-25T00:00:00Z", created_at: "2026-10-25T00:00:00Z", schema_version: SCHEMA_VERSION,
    };
    let submitted = false;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = pathOf(input);
      if (path === "/learning/unit/today") return respond(200, unit);
      if (path === "/learning/activities" && init?.method === "POST") {
        submitted = true;
        expect(JSON.parse(String(init.body))).toMatchObject({ unit_id: unit.unit_id, user_answer: activity.user_answer, reflection: activity.reflection });
        return respond(saveStatus, saveStatus < 400 ? activity : { detail: "保存失败" });
      }
      if (path === "/learning/goals/current") return respond(submitted ? goalStatus : 200,
        submitted && goalStatus >= 400 ? { detail: "进度暂不可用" } : { ...goal, completed_count: submitted ? 7 : 2 });
      return baseFetch(input, init);
    });
    globalThis.fetch = fetch as typeof globalThis.fetch;
    render(<AppShell />);
    fireEvent.change(await screen.findByLabelText("你的回答"), { target: { value: activity.user_answer } });
    fireEvent.change(screen.getByLabelText("反思（可导出）"), { target: { value: activity.reflection } });
    fireEvent.click(screen.getByRole("button", { name: "保存练习与反思" }));
    if (saveStatus < 400) {
      await screen.findByText("已保存本次练习与反思，可在复盘页查看记录。");
      expect(screen.getByText(goalStatus < 400 ? "07 / 10" : "02 / 10")).toBeTruthy();
    } else {
      await waitFor(() => expect(screen.getByRole("button", { name: "保存练习与反思" })).toBeEnabled());
      expect(screen.getByLabelText("你的回答")).toHaveValue(activity.user_answer);
      expect(screen.getByText("02 / 10")).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("button", { name: "复盘" }));
    await screen.findByRole("heading", { name: "学习活动与反思" });
    if (saveStatus < 400) {
      expect(screen.getByText(activity.user_answer)).toBeVisible();
      expect(screen.getByText(activity.reflection)).toBeVisible();
    } else {
      expect(screen.getByText("还没有学习活动记录。")).toBeVisible();
    }
    // 列表只在启动时读取一次：复盘内容必须来自保存回执后的共享状态。
    expect(fetch.mock.calls.filter(([input, init]) => pathOf(input) === "/learning/activities" && init?.method === "GET")).toHaveLength(1);
    expect(fetch.mock.calls.filter(([input]) => pathOf(input) === "/learning/goals/current")).toHaveLength(saveStatus < 400 ? 2 : 1);
  });

  it("延后批次仍在途时，骨架已解除、壳层与落地页可用", async () => {
    const channel = installChannel({ gateDeferred: true });
    render(<AppShell />);

    // 首批（10 项）就绪即解除 booting 骨架——此时延后样本仍挂起。
    await waitFor(() => expect(document.querySelector(".booting-pane")).toBeNull(), { timeout: 4000 });
    expect(document.getElementById("mainScroll")).not.toBeNull();
    expect(screen.getByRole("complementary", { name: "工作区切换" })).toBeInTheDocument();
    for (const path of DEFERRED_SAMPLE) expect(channel.calls).toContain(path);

    // 延后数据回来后仍正常渲染，不出现「尚在加载」被当成「没有数据」的整页空白。
    channel.releaseDeferred();
    await waitFor(() => expect(document.getElementById("mainScroll")?.textContent ?? "").not.toBe(""), { timeout: 4000 });
    expect(document.querySelector(".booting-pane")).toBeNull();
  });

  it("局部失败：失败项进告警条，其余内容仍可阅读", async () => {
    installChannel({ gateDeferred: false, failPaths: ["/notifications/pending"] });
    render(<AppShell />);

    const banner = await screen.findByText("部分数据加载失败（Host 桥或 Core 未就绪）", {}, { timeout: 4000 });
    expect(banner.parentElement?.getAttribute("role")).toBe("alert");
    expect(document.querySelector(".load-error-list")?.textContent).toContain("通知加载失败");
    // 失败不吞掉整页：壳层与内容区仍在。
    expect(document.getElementById("mainScroll")).not.toBeNull();
    expect(screen.getByRole("complementary", { name: "工作区切换" })).toBeInTheDocument();
  });

  it("恢复后重试加载：告警条撤下，重新拉取该路径", async () => {
    const channel = installChannel({ gateDeferred: false, failPaths: ["/notifications/pending"] });
    render(<AppShell />);
    await screen.findByText("部分数据加载失败（Host 桥或 Core 未就绪）", {}, { timeout: 4000 });
    const before = channel.calls.filter((path) => path === "/notifications/pending").length;

    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    await waitFor(() => expect(document.querySelector(".load-error-banner")).toBeNull(), { timeout: 4000 });
    expect(channel.calls.filter((path) => path === "/notifications/pending").length).toBeGreaterThan(before);
  });
});
