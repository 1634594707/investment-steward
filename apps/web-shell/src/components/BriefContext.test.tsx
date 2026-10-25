import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BriefItem } from "@investment-steward/domain-contracts";
import { BriefContext } from "./BriefContext";

const first: BriefItem = { display_order: 0, title: "第一标的", summary: "待复核", signal: "research",
  related_instrument: "CN:STOCK:600618", evidence_refs: ["e1"] };

describe("简报处理上下文", () => {
  it("连续跳转替换标的和依据，不转换标识符或保留上次依据", () => {
    const onOpenEvidence = vi.fn();
    const onClose = vi.fn();
    const page = render(<BriefContext item={first} onOpenEvidence={onOpenEvidence} onClose={onClose} />);
    expect(screen.getByText("本次处理标的：CN:STOCK:600618")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看依据 e1" }));
    expect(onOpenEvidence).toHaveBeenLastCalledWith("e1");
    page.rerender(<BriefContext item={{ ...first, related_instrument: "300750", evidence_refs: ["e2"] }}
      onOpenEvidence={onOpenEvidence} onClose={onClose} />);
    expect(screen.getByText("本次处理标的：300750")).toBeTruthy();
    expect(screen.queryByText("本次处理标的：CN:STOCK:600618")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看依据 e1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看依据 e2" }));
    expect(onOpenEvidence).toHaveBeenLastCalledWith("e2");
    page.rerender(<BriefContext item={{ ...first, related_instrument: null, evidence_refs: [] }}
      onOpenEvidence={onOpenEvidence} onClose={onClose} />);
    expect(screen.getByText("本次处理标的：未关联标的，请自行选择处理对象")).toBeTruthy();
    expect(screen.getByText("本条未附证据引用")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "结束本次简报处理" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
