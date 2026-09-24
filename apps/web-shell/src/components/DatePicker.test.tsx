/**
 * D04（2026-09-19 用户要求）：日期选择走项目自己的日历，配色跟主题令牌走。
 * 这里锁住三件事：值格式（compact / ISO 两套）、手填输入框仍然存在（多处测试与用户都靠它）、
 * 面板开关（点按钮开、选日后关、Escape 关）。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { DatePicker, compactToIso, isoToCompact } from "./DatePicker";

describe("DatePicker（D04）", () => {
  it("紧凑格式：选日回写 YYYYMMDD 并关闭面板", () => {
    const onChange = vi.fn();
    render(<DatePicker compact value="20260918" onChange={onChange} ariaLabel="起始日" />);
    fireEvent.click(screen.getByRole("button", { name: "起始日：打开日历" }));
    const dialog = screen.getByRole("dialog", { name: "起始日 日历" });
    expect(dialog.textContent).toContain("2026 年 09 月");
    fireEvent.click(screen.getByRole("button", { name: "2026-09-30" }));
    expect(onChange).toHaveBeenCalledWith("20260930");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ISO 格式：选日回写 YYYY-MM-DD；「清除」交空值", () => {
    const onChange = vi.fn();
    render(<DatePicker value="2026-09-18" onChange={onChange} ariaLabel="验证时间" />);
    fireEvent.click(screen.getByRole("button", { name: "验证时间：打开日历" }));
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("手填输入框保留（placeholder 与可输入），Escape 收起面板", () => {
    const onChange = vi.fn();
    render(<DatePicker compact value="" onChange={onChange} ariaLabel="结束日" />);
    const input = screen.getByPlaceholderText("YYYYMMDD");
    fireEvent.change(input, { target: { value: "20260919" } });
    expect(onChange).toHaveBeenLastCalledWith("20260919");
    fireEvent.click(screen.getByRole("button", { name: "结束日：打开日历" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("格式互转工具：不合格式不猜", () => {
    expect(isoToCompact("2026-09-18")).toBe("20260918");
    expect(compactToIso("20260918")).toBe("2026-09-18");
    expect(compactToIso("2026-09-18")).toBe("");
    expect(compactToIso("abc")).toBe("");
  });
});
