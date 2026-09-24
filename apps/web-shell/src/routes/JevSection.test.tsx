/**
 * JV02（Jev 决策模型接入路线图 2026-09-21）：Jev 设置分区的呈现契约。
 *
 * 锁住四件用户可感知、且最容易做错的事：
 * 1. **数据去向摆在明面上**——state 会出网到美国托管的第三方，开启前必须先看见政策；
 * 2. **总闸关闭时如实降级**——三个按钮全禁用 + 明说不会发出请求，不假装能测；
 * 3. **界面不出现 Jev 根本不返回的字段**——`noul` 题没有 confidence，就别摆一个「置信度」；
 * 4. **保存只提交白名单字段**，且明确告知密钥不进本配置；
 * 5. **粘贴明文密钥时服务端会转成 key_id 并搬进凭据库**——保存回执里的 `credential_note`
 *    必须显示出来、输入框必须回填 key_id，否则用户会以为明文还留在配置里。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

import type { JevConfigView } from "@investment-steward/domain-contracts";

import { JevSection } from "./JevSection";

const VIEW: JevConfigView = {
  ok: true,
  config: {
    user_id: "local",
    enabled: false,
    base_url: "https://api.typesafe.ai/v1",
    model: "jev-latest",
    credential_ref: "jev_api_key",
    timeout_secs: 60,
    created_at: "2026-09-21T10:00:00+00:00",
    updated_at: "2026-09-21T10:00:00+00:00",
    schema_version: "1.0",
  },
  saved: true,
  access_enabled: true,
  schema_version: "v1",
  defaults: { base_url: "https://api.typesafe.ai/v1", model: "jev-latest", timeout_secs: 60 },
  limits: { choice_max_options: 255, score_min_levels: 2, score_max_levels: 10 },
  noul_thresholds: { yes: 0.8, no: 0.2 },
  data_handling: {
    trains_on_input: false,
    zero_data_retention: "enterprise_only",
    zdr_applied: false,
    retention: "默认非零留存、无固定期限（企业版可申请 ZDR）",
    hosted_in: "美国",
  },
  notes: ["Jev 只回类型化答案，不生成正文；判定结果只做标注、降级、排序。"],
};

const PROBE = {
  ok: true,
  latency_ms: 812,
  detail: "连通：Jev 已应答。",
  models: ["jev-latest", "jev-1.13.0"],
  model: "jev-1.13.0",
  requested_model: "jev-latest",
  probe_noul: 0.93,
  probe_verdict: "yes",
  input_tokens: 120,
  output_tokens: 8,
};

function stubFetch(
  overrides: { view?: Partial<JevConfigView>; probe?: unknown; save?: Record<string, unknown> } = {},
) {
  const view = { ...VIEW, ...overrides.view };
  return vi.fn(async (req: { method: string; path: string }) => {
    if (req.path === "/jev/config" && req.method === "GET") return { status: 200, data: view };
    if (req.path === "/jev/config" && req.method === "PUT") {
      return { status: 200, data: { ok: true, config: view.config, ...overrides.save } };
    }
    if (req.path === "/jev/probe") return { status: 200, data: overrides.probe ?? PROBE };
    if (req.path === "/jev/models") return { status: 200, data: PROBE };
    return { status: 404, data: { detail: "unknown" } };
  });
}

describe("JV02 Jev 决策模型设置分区", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    cleanup();
  });

  it("把数据去向与官方上限摆在明面上（开启前可判断风险）", async () => {
    render(<JevSection coreRequest={stubFetch() as never} />);
    expect(await screen.findByText("数据去向（开启前请先确认）")).toBeTruthy();
    // 托管地必须写清楚——这是「要不要开」的关键信息。
    expect(screen.getByText("美国")).toBeTruthy();
    expect(screen.getByText("否（官方承诺）")).toBeTruthy();
    expect(screen.getByText(/仅企业版（需联系厂商）/)).toBeTruthy();
    // 官方协议上限本地先拦。
    expect(screen.getByText("255")).toBeTruthy();
    expect(screen.getByText("2–10 级（有序等级数组）")).toBeTruthy();
  });

  it("§E2 拍板后：明说本项目不申请 ZDR，且不让人误以为有零留存兜底", async () => {
    render(<JevSection coreRequest={stubFetch() as never} />);
    await screen.findByText("数据去向（开启前请先确认）");
    // 「仅企业版」说的是厂商政策，「本项目不申请」说的是我们的立场——两件事必须都写出来。
    expect(screen.getByText(/本项目不申请（既有决定）/)).toBeTruthy();
    expect(screen.getByText(/白名单按最严口径执行/)).toBeTruthy();
  });

  it("不出现 Jev 根本不返回的字段（noul 无 confidence）", async () => {
    render(<JevSection coreRequest={stubFetch() as never} />);
    await screen.findByText("数据去向（开启前请先确认）");
    // 「置信度」是 choice 才有的概念，noul 题没有——界面上不得摆这个字段。
    expect(screen.queryByText(/置信度/)).toBeNull();
    // 反而要主动说清这个反直觉点。
    expect(screen.getByText(/noul 题不返回 confidence/)).toBeTruthy();
    expect(screen.getByText(/score 不是 0–100 分/)).toBeTruthy();
  });

  it("总闸关闭时三个按钮全禁用，并明说不会发出任何请求", async () => {
    render(<JevSection coreRequest={stubFetch({ view: { access_enabled: false } }) as never} />);
    expect(await screen.findByText(/模型出网总闸已关闭/)).toBeTruthy();

    for (const name of ["保存", "测连通性", "拉取模型"]) {
      const button = screen.getByRole("button", { name });
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it("保存只提交白名单字段，并明确告知密钥不进本配置", async () => {
    const coreRequest = stubFetch();
    render(<JevSection coreRequest={coreRequest as never} />);
    const save = await screen.findByRole("button", { name: "保存" });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);

    await waitFor(() =>
      expect(coreRequest.mock.calls.some(([req]) => (req as { method: string }).method === "PUT")).toBe(true),
    );
    const put = coreRequest.mock.calls.find(([req]) => (req as { method: string }).method === "PUT");
    expect((put?.[0] as { body?: Record<string, unknown> } | undefined)?.body).toEqual({
      enabled: false,
      base_url: "https://api.typesafe.ai/v1",
      model: "jev-latest",
      credential_ref: "jev_api_key",
      timeout_secs: 60,
    });
    // 密钥绝不出现在请求体里——它只属于「凭据与安全」分区。
    expect(JSON.stringify(put?.[0])).not.toMatch(/sk-|api_key"\s*:\s*"[A-Za-z0-9]{20}/);
    expect(await screen.findByText(/密钥不在本配置里/)).toBeTruthy();
  });

  it("保存回执带 credential_note 时如实显示，并把输入框回填成 key_id", async () => {
    // 服务端在保存时把明文密钥搬进凭据库并改写成 key_id（api/app.py `_normalize_jev_credential_ref`）。
    // 若前端不回填，输入框会继续显示明文，用户下次保存又会把明文送一遍。
    const coreRequest = stubFetch({
      save: {
        config: { ...VIEW.config, credential_ref: "jev_api_key" },
        credential_note: "检测到明文密钥，已存入本机凭据库（key_id=jev_api_key，尾号 b77e）。",
      },
    });
    render(<JevSection coreRequest={coreRequest as never} />);
    const save = await screen.findByRole("button", { name: "保存" });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);

    expect(await screen.findByText(/已存入本机凭据库/)).toBeTruthy();
    expect(screen.getByText(/密钥不在本配置里/)).toBeTruthy();
    expect((screen.getByPlaceholderText("jev_api_key") as HTMLInputElement).value).toBe("jev_api_key");
  });

  it("探测回执给出实际应答版本与中文探测题，并提示 CJK 精度风险", async () => {
    const coreRequest = stubFetch();
    render(<JevSection coreRequest={coreRequest as never} />);
    const probe = await screen.findByRole("button", { name: "测连通性" });
    await waitFor(() => expect((probe as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(probe);

    expect(await screen.findByText("探测回执")).toBeTruthy();
    // 请求名与回执版本分开显示：jev-latest 会漂移，实测版本才是可追溯的那个。
    expect(screen.getByText("jev-1.13.0")).toBeTruthy();
    expect(screen.getByText("jev-latest")).toBeTruthy();
    expect(screen.getByText("812 ms")).toBeTruthy();
    expect(screen.getByText(/noul=0\.93/)).toBeTruthy();
    expect(screen.getByText(/中文（CJK）可处理但精度较低/)).toBeTruthy();
  });

  it("超时非法时禁止保存，并给出边界而不是静默取默认", async () => {
    render(<JevSection coreRequest={stubFetch() as never} />);
    const input = await screen.findByDisplayValue("60");
    fireEvent.change(input, { target: { value: "3" } });

    expect(screen.getByText("需为 5–600 的整数")).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("未提供 Core 通道时如实降级，而不是空表格", async () => {
    render(<JevSection />);
    expect(screen.getByText(/当前通道不支持读取 Jev 配置/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  });
});
