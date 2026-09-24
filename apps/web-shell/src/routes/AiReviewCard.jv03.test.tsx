/**
 * JV03（Jev 决策模型接入路线图 2026-09-21）：AI 复核卡片的呈现契约。
 *
 * 锁住两件最容易做错、且用户能直接看见的事：
 * 1. **判定来源要标出来**——判定分是 Jev 给的还是 chat 给的，口径不同，用户有权知道；
 * 2. **叙述缺失时不能把默认值当结论**——合并时 `verdict` 会填中性默认值，
 *    若照原样渲染，用户会看到一枚「中性」徽章，误以为某个模型给出了中性判断。
 *    实际上那是「没有叙述」的占位，必须显示成缺失说明而不是结论。
 */
import { describe, expect, it, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import { AiReviewCard, type AiReviewRecord } from "./TacticsPage";

function record(overrides: Partial<AiReviewRecord> = {}, payload: Record<string, unknown> = {}): AiReviewRecord {
  return {
    review_id: "r-1",
    symbol: "600176",
    name: "600176",
    model: "Jev／jev-1.13.0",
    rule_score: 58,
    ai_score: 75,
    verdict: "bullish",
    agreement: "partial",
    summary: "突破形态成立但量能未同步放大",
    engine: "jev",
    confidence: 0.71,
    ai_score_raw: 3,
    payload: {
      review: {
        ai_score: 75,
        verdict: "bullish",
        agreement: "partial",
        summary: "突破形态成立但量能未同步放大",
        strengths: ["均线多头排列"],
        concerns: ["量能背离"],
        fake_breakout_risk: "high",
        key_levels: { support: 10.5, resistance: 12.8 },
        watch_points: ["回踩 10.5 是否守住"],
        rule_score_comment: "规则分略高估",
      },
      narrative_status: "ok",
      ...payload,
    },
    created_at: "2026-09-21T10:00:00+00:00",
    ...overrides,
  };
}

describe("JV03 AI 复核卡片", () => {
  afterEach(() => {
    cleanup();
  });

  it("标出判定层来源与置信度（Jev 与 chat 是两套口径）", () => {
    render(<AiReviewCard record={record()} />);
    expect(screen.getByText(/判定 Jev/)).toBeTruthy();
    expect(screen.getByText(/置信 0\.71/)).toBeTruthy();
  });

  it("Jev 未参与时标为 chat，且不显示置信度（那题没有 confidence）", () => {
    render(<AiReviewCard record={record({ engine: "chat", confidence: null })} />);
    expect(screen.getByText(/判定 chat/)).toBeTruthy();
    expect(screen.queryByText(/置信/)).toBeNull();
  });

  it("叙述缺失时给出缺失说明，且不把中性默认值渲染成结论", () => {
    render(
      <AiReviewCard
        // 合并结果里 verdict 退化成中性默认值——那是占位，不是模型结论。
        record={record(
          { summary: "", verdict: "neutral", ai_score: 25, confidence: null },
          {
            narrative_status: "unparsable",
            narrative_error: "模型输出无法解析为约定的复核 JSON",
            review: {
              ai_score: 25,
              verdict: "neutral",
              agreement: "partial",
              summary: "",
              strengths: [],
              concerns: [],
              fake_breakout_risk: "low",
              key_levels: { support: null, resistance: null },
              watch_points: [],
              rule_score_comment: "",
            },
          },
        )}
      />,
    );
    expect(screen.getByText(/本轮没有叙述部分（模型没按约定格式输出）/)).toBeTruthy();
    // 判定层结论仍在（AI 分照常显示、风险标签照常显示）
    expect(screen.getByText(/AI 25/)).toBeTruthy();
    expect(screen.getByText(/假突破风险低/)).toBeTruthy();
    // 但没有「技术面偏多/中性/偏空」徽章，也没有空的叙述段落
    expect(screen.queryByText("中性")).toBeNull();
    expect(screen.queryByText("技术面偏多")).toBeNull();
  });

  it("JV03 之前的旧记录（没有 engine / narrative_status）按今天的样子照常渲染", () => {
    const legacy = record({ engine: undefined, confidence: undefined });
    delete legacy.payload.narrative_status;
    render(<AiReviewCard record={legacy} />);
    expect(screen.queryByText(/判定 /)).toBeNull();
    expect(screen.queryByText(/本轮没有叙述部分/)).toBeNull();
    expect(screen.getByText("技术面偏多")).toBeTruthy();
    expect(screen.getByText("突破形态成立但量能未同步放大")).toBeTruthy();
  });
});
