import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import type { Evidence, Thesis } from "@investment-steward/domain-contracts";
import { InvestmentPage } from "./InvestmentPage";
import { useSteward } from "../hooks/useSteward";
import type { CoreClient } from "../state/coreClient";

afterEach(cleanup);
const thesis: Thesis = { thesis_id: "t1", user_id: "u1", instrument: "TEST", original_statement: "原始判断",
  core_assumptions: ["假设"], supporting_conditions: [], invalidation_conditions: ["失效条件"],
  observation_metrics: [], evidence_refs: ["missing"], status: "active", schema_version: "1.0",
  created_at: "2026-10-26T00:00:00Z", updated_at: "2026-10-26T00:00:00Z" };
function evidence(id: string, subject: string): Evidence {
  return { evidence_id: id, tenant_id: "u1", subject_refs: [subject], evidence_type: "financial",
    source_name: id, license_status: "unknown", collected_at: thesis.created_at, schema_version: "1.0",
    summary: "合成证据", content_hash: id, relation: "contradicting", freshness: "current",
    quality_flags: [], limitations: [], status: "retracted" };
}
function show(overrides: Partial<ComponentProps<typeof InvestmentPage>> = {}) {
  const save = vi.fn(async (_thesis: Thesis) => true);
  const create = vi.fn(async (_input: Parameters<ComponentProps<typeof InvestmentPage>["onCreateThesis"]>[0]) => true);
  const page = (next: Partial<ComponentProps<typeof InvestmentPage>> = {}) => <InvestmentPage policy={null} evidence={[evidence("exact", "TEST"), evidence("alias", "instrument:TEST")]}
    holdings={[{ holding_id: "h1", user_id: "u1", instrument: "TEST", label: "合成标的", status: "watchlist",
      strategy_note: "", schema_version: "1.0", created_at: thesis.created_at, updated_at: thesis.updated_at }]}
    theses={[thesis]} candles={null} candlesLoading={false} candlePeriod="day" isDemo={false}
    marketPluginEnabled={false} onNavigate={vi.fn()} onSaveThesis={save} onCreateThesis={create}
    onCreateHolding={async () => true} onDeleteHolding={async () => true}
    onLookupInstrument={async () => ({ ok: true, hits: [] })} onCreatePolicyDraft={async () => null}
    onConfirmPolicy={async () => true} onSelectInstrument={vi.fn()} onSelectCandlePeriod={vi.fn()}
    onPullEvidence={async () => null} onDedupEvidence={async () => null} onOpenEvidence={vi.fn()} {...overrides} {...next} />;
  const view = render(page());
  return { save, create, rerender: (next: Partial<ComponentProps<typeof InvestmentPage>>) => view.rerender(page(next)) };
}
async function confirm() {
  fireEvent.click(screen.getByRole("button", { name: /预览并确认/ }));
  fireEvent.change(screen.getByPlaceholderText("用一句话说明这次确认的原因…"), { target: { value: "核对证据关联" } });
  fireEvent.click(screen.getByRole("button", { name: "确认并启用" }));
}
describe("投资逻辑证据关联", () => {
  it("只读详情显示撤回与缺详情历史引用，点击仍可追溯", () => {
    const open = vi.fn();
    show({ theses: [{ ...thesis, evidence_refs: ["exact", "missing"] }], onOpenEvidence: open });
    fireEvent.click(screen.getByRole("tab", { name: "论点" }));
    const panel = within(screen.getByRole("region", { name: "已关联证据" }));
    expect(panel.getByText("已撤回")).toBeTruthy();
    expect(panel.getByText("详情未取得，状态未知")).toBeTruthy();
    fireEvent.click(panel.getByRole("button", { name: /missing/ }));
    expect(open).toHaveBeenCalledWith("missing");
  });
  it("实际保存钩子的服务端记录进入只读详情，清空后旧引用消失", async () => {
    const returned = { ...thesis, evidence_refs: ["exact"] };
    const request = vi.fn().mockResolvedValueOnce({ status: 200, data: returned })
      .mockResolvedValueOnce({ status: 200, data: { ...returned, evidence_refs: [] } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, true));
    const page = show({ onSaveThesis: result.current.saveThesis });
    fireEvent.click(screen.getByRole("button", { name: /编辑投资逻辑/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /missing/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /exact/ }));
    await confirm();
    await waitFor(() => expect(result.current.theses).toEqual([returned]));
    page.rerender({ theses: result.current.theses, onSaveThesis: result.current.saveThesis });
    const panel = () => within(screen.getByRole("region", { name: "已关联证据" }));
    expect(panel().getByRole("button", { name: /exact/ })).toBeTruthy();
    expect(panel().queryByRole("button", { name: /missing/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /编辑投资逻辑/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /exact/ }));
    await confirm();
    await waitFor(() => expect(result.current.theses[0]?.evidence_refs).toEqual([]));
    page.rerender({ theses: result.current.theses });
    expect(panel().getByText("无")).toBeTruthy();
    expect(panel().queryByRole("button")).toBeNull();
  });
  it("仅新增精确标的引用，缺详情旧引用保留，显式清空才移除", async () => {
    const { save } = show();
    fireEvent.click(screen.getByRole("button", { name: /编辑投资逻辑/ }));
    expect(screen.getByRole("checkbox", { name: /missing/ })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /alias/ })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /exact/ }));
    await confirm();
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ evidence_refs: ["missing", "exact"], invalidation_conditions: ["失效条件"] })));
    fireEvent.click(screen.getByRole("button", { name: /编辑投资逻辑/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /missing/ }));
    expect(screen.getByRole("checkbox", { name: /missing/ })).not.toBeChecked();
    await confirm();
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ evidence_refs: [] })));
  });
  it("创建时发送选择，失败保留编辑内容及引用", async () => {
    const create = vi.fn().mockResolvedValue(false);
    show({ theses: [], onCreateThesis: create });
    fireEvent.click(screen.getByRole("button", { name: /新建投资逻辑/ }));
    fireEvent.change(screen.getByLabelText("投资逻辑陈述"), { target: { value: "新判断" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /exact/ }));
    fireEvent.click(screen.getByRole("button", { name: /保存投资逻辑/ }));
    await waitFor(() => expect(create).toHaveBeenCalledWith(expect.objectContaining({ evidence_refs: ["exact"] })));
    expect(await screen.findByText("保存失败，编辑内容已保留，请重试。")).toBeTruthy();
    expect(screen.getByLabelText("投资逻辑陈述")).toHaveValue("新判断");
    expect(screen.getByRole("checkbox", { name: /exact/ })).toBeChecked();
  });
  it("实际创建钩子传递关联，更新钩子保留显式空数组", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: thesis });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, true));
    await act(async () => { await result.current.createThesis({ ...thesis, evidence_refs: ["exact"] }); });
    expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: "POST", path: "/thesis", body: expect.objectContaining({ evidence_refs: ["exact"] }) }));
    await act(async () => { await result.current.saveThesis({ ...thesis, evidence_refs: [] }); });
    expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ method: "PUT", path: "/thesis/t1", body: expect.objectContaining({ evidence_refs: [] }) }));
  });
  it("更新被服务端拒绝时恢复原关联，而非保留乐观清空", async () => {
    const request = vi.fn().mockResolvedValueOnce({ status: 201, data: thesis })
      .mockResolvedValueOnce({ status: 422, data: { detail: "invalid evidence" } });
    const { result } = renderHook(() => useSteward({ request } as unknown as CoreClient, true));
    await act(async () => { await result.current.createThesis(thesis); });
    let saved: boolean | undefined;
    await act(async () => { saved = await result.current.saveThesis({ ...thesis, evidence_refs: [] }); });
    expect(saved).toBe(false);
    expect(result.current.theses).toEqual([thesis]);
  });
});
