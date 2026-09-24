/**
 * Y4-06 / Y4-07（游资二期）+ U05（桌面端升级路线图 2026-09-18）：学习页的内容与副作用契约。
 *
 * 这一组守的是路线图 §7 明令的几条：
 *   1. 十一课完整；L6–L10 的**可见性边界必须在页面上可见**（不能只写在 JSON 里）；
 *   2. 术语表逐词给出证据边界，不可观测的词显式标注；
 *   3. 页面不出现交易指导与预测性措辞；
 *   4. 页面**不查询持仓名单**（学习页只取课程内容，不触发任何业务取数）。
 *
 * U05 之后页面改为从 Core 只读端点 `/youzi/curriculum` 取同一份 curriculum.json（单一数据源，
 * 前端不再手抄副本）。本测试的 mock 按路径路由：curriculum 请求回放**真实的** curriculum.json，
 * 因此「页面与 JSON 同源」的核对现在走的是与线上完全相同的取数路径。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const request = vi.fn();

vi.mock("../state/coreClient", () => ({
  createCoreClient: () => ({ request }),
}));

import { YouziPage } from "./YouziPage";

/** 读真实课程 JSON（用 import.meta.url 反推仓库根，见下方同源测试里的说明）。 */
function loadCurriculum(): object {
  const here = dirname(fileURLToPath(import.meta.url));
  const repoRoot = resolve(here, "../../../..");
  return JSON.parse(readFileSync(resolve(repoRoot, "plugins/official/youzi-radar/curriculum.json"), "utf8"));
}

function mockRoutes(): void {
  const curriculum = loadCurriculum();
  request.mockImplementation((req: { path: string }) => {
    if (req.path.startsWith("/youzi/curriculum")) {
      return Promise.resolve({ status: 200, data: { ok: true, curriculum, source: "plugins/official/youzi-radar/curriculum.json" } });
    }
    return Promise.resolve({
      status: 200,
      data: { available: true, trading_day: "20260917", watchlist_version: "v1", rows: [] },
    });
  });
}

function openLearn() {
  mockRoutes();
  render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);
  // ★ 页签 title 已随 Y4 从「六课」改成「十一课」，这里必须跟着改；
  // 用正则匹配前缀，避免下次再改数字时又断一次。
  fireEvent.click(screen.getByTitle(/推荐首站：先学怎么读/));
}

beforeEach(() => {
  request.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("YouziPage · 学习页（Y4-06/Y4-07 + U05）", () => {
  it("十一课全部渲染（L0–L10），新增五课不能只存在于 JSON", async () => {
    openLearn();
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());
    for (const level of ["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10"]) {
      expect(screen.getByText(level)).toBeTruthy();
    }
    expect(screen.getByText("上榜原因能说明什么，不能说明什么")).toBeTruthy();
    expect(screen.getByText("披露之后：逐期限复盘与信息边界")).toBeTruthy();
  });

  it("★ 新增五课逐课在页面上显示「可见性边界」", async () => {
    openLearn();
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());
    // 五条边界文案各出现一次（HTML 中「可见性边界」前缀应至少 5 次）。
    const occurrences = screen.getAllByText(/可见性边界：/);
    expect(occurrences.length).toBeGreaterThanOrEqual(5);
    // ★ 用 getAllByText：这两句免责文案在页面上不止一处（L8 的边界段落与术语表
    // 「返场」「锁仓」的证据边界会重复表述同一件事），getByText 会因多命中而报错。
    // 判据是「页面上出现了」，不是「只出现一次」。
    expect(screen.getAllByText(/同一营业部不等于同一账户/).length).toBeGreaterThanOrEqual(1);
    // U05 实测漂移证据：旧手抄副本的 L10 边界带「空值不是 0」，JSON 原文没有——
    // 单一数据源后页面只可能出现 JSON 原文，故按 JSON 原文断言。
    expect(screen.getAllByText(/不是收益预测/).length).toBeGreaterThanOrEqual(1);
  });

  it("★ U05：每课显示「原理」「学到什么程度算过（acceptance）」四段字段", async () => {
    openLearn();
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());
    // 四段渲染：原理（principle）/ 对应机制（mechanism）/ 练习（exercise）/ 验收（acceptance）。
    expect(screen.getAllByText(/^原理：/).length).toBe(11);
    expect(screen.getAllByText(/^学到什么程度算过：/).length).toBe(11);
    expect(screen.getAllByText(/^对应机制：/).length).toBe(11);
    expect(screen.getAllByText(/^练习：/).length).toBe(11);
  });

  it("★ U05：页头显示课程版本与更新时间（课上的是哪一版可核对）", async () => {
    openLearn();
    const curriculum = loadCurriculum() as { version: string; updated_at: string };
    await waitFor(() => expect(screen.getByText(curriculum.version)).toBeTruthy());
    // updated_at 单独成 span，用全等匹配（version 串里也含日期前缀，避免多命中）。
    expect(screen.getByText(curriculum.updated_at)).toBeTruthy();
  });

  it("★ 术语表逐词给证据边界，并显式标「当前不可观测」", async () => {
    openLearn();
    await waitFor(() => expect(screen.getByText("术语表（市场语言注释）")).toBeTruthy());
    for (const term of ["首板", "接力", "反核", "一日游", "返场", "锁仓", "翘板", "地天板", "核按钮"]) {
      expect(screen.getByText(term)).toBeTruthy();
    }
    const badges = screen.getAllByText("当前不可观测");
    expect(badges.length).toBeGreaterThanOrEqual(9);
  });

  it("★ 页面不含交易指导与预测性措辞", async () => {
    openLearn();
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());
    const text = document.body.textContent ?? "";
    for (const banned of [
      "建议买入", "建议卖出", "可以买入", "可以卖出", "应当买入", "应当卖出",
      "必涨", "稳赚", "止盈", "止损位",
    ]) {
      expect(text).not.toContain(banned);
    }
  });

  it("★ 学习页只取课程内容：不发起业务取数（尤其不碰持仓）", async () => {
    mockRoutes();
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);
    // 等首屏两个请求（today / watchlist）落地后再快照计数。
    await waitFor(() => expect(request.mock.calls.length).toBeGreaterThanOrEqual(2));
    const before = request.mock.calls.length;
    fireEvent.click(screen.getByTitle(/推荐首站：先学怎么读/));
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());

    const after = request.mock.calls.map((call) => (call[0] as { path: string }).path);
    // U05：学习页只新增一次 /youzi/curriculum（只读内容端点）。
    expect(after.filter((path) => path.startsWith("/youzi/curriculum")).length).toBe(1);
    // 尤其不得触碰持仓相关端点。
    expect(after.some((path) => path.includes("cross-check") || path.includes("portfolio"))).toBe(false);
  });

  it("★ 页面课程与 curriculum.json 同源（U05 后走端点路径：mock 回放真实 JSON）", async () => {
    // ★ 不要写死相对层数、也不要依赖 `process.cwd()`（vitest 的 cwd 是 `apps/web-shell`，
    // `../..` 会指到 `D:\Administrator\Desktop\quant` 而不是仓库根）。
    // 用 vitest 注入的 `import.meta.url`（本文件在 `apps/web-shell/src/routes/`）反推仓库根：
    // url → `src/routes` → 向上一级到 `src`，再向上一级到 `apps/web-shell`，再两级到仓库根。
    const here = dirname(fileURLToPath(import.meta.url));
    const repoRoot = resolve(here, "../../../..");
    const raw = readFileSync(
      resolve(repoRoot, "plugins/official/youzi-radar/curriculum.json"),
      "utf8",
    );
    const curriculum = JSON.parse(raw) as {
      lessons: Array<{ level: string; title: string }>;
      glossary: { terms: Array<{ term: string }> };
    };

    openLearn();
    await waitFor(() => expect(screen.getByText("L6")).toBeTruthy());

    const text = document.body.textContent ?? "";
    for (const lesson of curriculum.lessons) {
      expect(text, `课程 ${lesson.level} 的标题未出现在学习页`).toContain(lesson.title);
    }
    for (const item of curriculum.glossary.terms) {
      expect(text, `术语 ${item.term} 未出现在学习页`).toContain(item.term);
    }
    expect(curriculum.lessons).toHaveLength(11);
  });
});
