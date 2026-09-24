/**
 * M5（2026-09-15 第二轮路线图）F01/F02：确认弹窗的「说明」三态。
 * - 默认 `requireNote` → 显示且必填（原则确认 / 投资逻辑确认沿用，未被本轮放宽）；
 * - `requireNote={false}` → 不显示说明框（自选移除）；
 * - `requireNote={false}` + `noteOptional` → 显示但选填（持仓移除）。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfirmDialog } from "./ConfirmDialog";

const base = {
  title: "移除自选「中国通号」",
  summary: "该标的将从本地账本移除。",
  diffs: [{ label: "标的", before: "688009 · 中国通号", after: "移除" }],
  confirmLabel: "确认移除",
};

describe("ConfirmDialog 说明三态（M5-F01/F02）", () => {
  it("默认必填：显示「必填」说明框，说明不足 4 字时不能确认", () => {
    render(<ConfirmDialog {...base} onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const note = screen.getByPlaceholderText("用一句话说明这次确认的原因…");
    expect(screen.getByText("确认说明（必填，将写入审计记录）")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "确认移除" });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(note, { target: { value: "确认" } });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(note, { target: { value: "确认移除这只自选" } });
    expect(confirm).toHaveProperty("disabled", false);
  });

  it("F01 自选：requireNote={false} 时不渲染说明框，确认按钮可直接点", () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...base} requireNote={false} onConfirm={onConfirm} onCancel={vi.fn()} />);
    expect(screen.queryByPlaceholderText("用一句话说明这次确认的原因…")).toBeNull();
    expect(screen.queryByText(/确认说明/)).toBeNull();
    const confirm = screen.getByRole("button", { name: "确认移除" });
    expect(confirm).toHaveProperty("disabled", false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("");
  });

  it("F02 持仓：noteOptional 时显示说明框但选填，空说明也能确认", () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...base} requireNote={false} noteOptional onConfirm={onConfirm} onCancel={vi.fn()} />);
    expect(screen.getByText("确认说明（选填，留空则审计记为固定说明）")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "确认移除" });
    expect(confirm).toHaveProperty("disabled", false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("");
  });

  it("F02 持仓：填了说明则原样交给调用方（后端写入审计）", () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...base} requireNote={false} noteOptional onConfirm={onConfirm} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("可以留空；填了会写进审计记录…"), { target: { value: "估值到位" } });
    fireEvent.click(screen.getByRole("button", { name: "确认移除" }));
    expect(onConfirm).toHaveBeenCalledWith("估值到位");
  });
});
