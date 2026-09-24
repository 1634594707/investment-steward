/**
 * v32 价值投资升级（2026-09-13 方案）前端面板测试：
 * 首屏驾驶舱三行结论词 / 正常化估值卡 / 追问复盘页签 / 导出附录。
 * 颜色语义断言聚焦「信息状态」（mint=正向、coral=风险、warn=待确认），不是买卖方向。
 */
import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnalysisTurn, FollowUpSupplement, FollowupClaim, StockReport } from "./researchTypes";
import {
  FollowUpPanel,
  EMPTY_FOLLOWUP_DRAFT,
  followupStatusAxes,
  LEGACY_ANSWERABILITY_LABEL,
  DATA_BASIS_LABEL,
  type FollowUpDraft,
} from "./followup";
import { ReportFirstScreen, ValuationCard } from "./panels";
import { buildStockReportMarkdown } from "./export";

function baseReport(overrides: Partial<StockReport> = {}): StockReport {
  return {
    ok: true,
    symbol: "600000",
    title: "测试研报",
    report: "### 技术面\nMA20 6.35 之上运行，量能温和放大。\n### 综合判断\n总体偏多。",
    citations: ["S1"],
    limitations: [],
    confidence: "medium",
    model: "test-model",
    source_keys: ["S1"],
    source_errors: {},
    generated_at: "2026-09-13T00:00:00Z",
    report_chars: 2153,
    report_mode: "standard",
    ...overrides,
  };
}

describe("ReportFirstScreen · 首屏驾驶舱（v32 §二.1）", () => {
  it("渲染三行结论词与适用期限，颜色按信息状态", () => {
    render(
      <ReportFirstScreen
        report={baseReport({
          pillar_verdicts: { fundamental: "偏强", valuation: "无法判断", technical: "待确认", horizon: ["3~12个月", "3~5年"] },
        })}
      />,
    );
    expect(screen.getByText("偏强")).toBeTruthy();
    expect(screen.getByText("无法判断")).toBeTruthy();
    expect(screen.getByText("待确认")).toBeTruthy();
    // 期限归一：全角波浪线与连字符写法并入统一词表
    expect(screen.getByText("3~12个月")).toBeTruthy();
    expect(screen.getByText("3~5年")).toBeTruthy();
    // （期限归一在服务端 normalize_horizon 完成，后端测试覆盖；前端只渲染归一后的值）
    // 三行判断的容器存在（首屏驾驶舱区块）
    expect(screen.getByRole("list", { name: /首屏驾驶舱/ })).toBeTruthy();
  });

  it("缺失驾驶舱字段时整体不渲染（不编造结论），旧报告首屏不受影响", () => {
    const { container } = render(<ReportFirstScreen report={baseReport()} />);
    expect(container.querySelector(".wb-cockpit")).toBeNull();
  });

  it("越界结论词按原值如实展示（灰色），不猜测语义", () => {
    render(<ReportFirstScreen report={baseReport({ pillar_verdicts: { fundamental: "买入", valuation: "", technical: "" } })} />);
    expect(screen.getByText("买入")).toBeTruthy();
    expect(screen.queryByText("低估")).toBeNull();
  });
});

describe("ValuationCard · 正常化估值（v32 §二.3）", () => {
  it("展示估值状态、正常化盈利、三情景公允价值与安全边际", () => {
    const { container } = render(
      <ValuationCard
        report={baseReport({
          valuation: {
            pe_ttm: "19.73",
            pe_percentile: "近5年 6.7% 分位",
            peer_position: "低于同业中位",
            verdict: "偏便宜",
            basis: "PE 分位与同业比较",
            normalized_earnings: { value: 1.1, basis: "剔除政府补助后的年化盈利", confidence: "medium" },
            valuation_cases: [
              { name: "bear", name_label: "悲观", earnings: 0.9, multiple: 12, fair_value: 10.8 },
              { name: "base", name_label: "基准", earnings: 1.1, multiple: 15, fair_value: 16.5 },
              { name: "bull", name_label: "乐观", earnings: 1.3, multiple: 18, fair_value: 23.4 },
            ],
            margin_of_safety: -0.05,
            valuation_status: "undervalued",
            valuation_limitations: ["正常化未剔除一次性收益"],
          },
        })}
      />,
    );
    expect(screen.getByText(/正常化口径 · 低估/)).toBeTruthy();
    const normalizedBlock = container.querySelector(".wb-val-normalized");
    expect(normalizedBlock?.textContent).toContain("1.1");
    expect(normalizedBlock?.textContent).toContain("剔除政府补助后的年化盈利");
    expect(screen.getByText("悲观")).toBeTruthy();
    expect(screen.getByText("基准")).toBeTruthy();
    expect(screen.getByText("乐观")).toBeTruthy();
    expect(screen.getByText(/安全边际/)).toBeTruthy();
    expect(screen.getByText(/现价高于基准公允价值/)).toBeTruthy(); // 负边际的语义说明
    expect(screen.getByText("正常化未剔除一次性收益")).toBeTruthy();
  });

  it("低 PE 自称低估被降级时显示原因（amber 提示）", () => {
    render(
      <ValuationCard
        report={baseReport({
          valuation: {
            pe_ttm: "19.73", pe_percentile: "6.7% 分位", peer_position: "", verdict: "偏便宜", basis: "",
            valuation_status: "undetermined",
            valuation_status_note: "模型声称「低估」但未给出正常化盈利值或依据；低 PE 分位不能排除利润处于周期高点、一次性收益或现金流较弱，按「无法判断」处理。",
          },
        })}
      />,
    );
    expect(screen.getByText(/正常化口径 · 无法判断/)).toBeTruthy();
    expect(screen.getByText(/未给出正常化盈利值或依据/)).toBeTruthy();
  });

  it("旧记录（无 v32 字段）不渲染新区块，估值卡保持原样", () => {
    const { container } = render(
      <ValuationCard
        report={baseReport({
          valuation: { pe_ttm: "19.73", pe_percentile: "", peer_position: "", verdict: "偏便宜", basis: "" },
        })}
      />,
    );
    expect(container.querySelector(".wb-val-normalized")).toBeNull();
    expect(container.querySelector(".wb-val-margin")).toBeNull();
    expect(screen.queryByText(/正常化口径/)).toBeNull();
  });

  /**
   * Q05（2026-09-19 路线图）：PE(TTM) 11.70 / 分位 / 同业位置都在，卡片却只给一个「无数据」chip。
   * 指标已取得但正常化盈利未验证 = 「合理价值未评估」；「估值指标缺失」留给真的没取到指标的场景。
   */
  it("Q05：有 PE 却未做正常化验证时显示「合理价值未评估」，不再是「无数据」", () => {
    const { container } = render(
      <ValuationCard
        report={baseReport({
          valuation: {
            pe_ttm: "11.70", pe_percentile: "近5年 1.7% 分位", peer_position: "PE 低于同业中位",
            verdict: "无数据", verdict_raw: "偏便宜", verdict_note: "结论缺少正常化盈利支撑，按无法判断归一。",
            basis: "PE 低分位",
            valuation_status: "undetermined",
            normalized_earnings: { value: null, basis: "未单列非经常性损益", confidence: "low" },
            valuation_evidence: {
              state: "normalization_unverified",
              label: "指标已取得，合理价值尚未评估",
              verdict_display: "合理价值未评估（指标已有，缺正常化盈利验证）",
              note: "pe_ttm 11.70 已取得，但正常化盈利未经验证。",
              metrics: { pe_ttm: "11.70" },
              metric_count: 1,
            },
          },
        })}
      />,
    );
    const text = container.querySelector(".wb-valuation")!.textContent!;
    expect(text).toContain("合理价值未评估（指标已有，缺正常化盈利验证）");
    expect(text).toContain("指标已取得，合理价值尚未评估");
    expect(text).toContain("pe_ttm 11.70 已取得");
    expect(text).not.toContain("无数据");
  });

  it("Q05：真的没取到指标时才说「估值指标缺失」", () => {
    const { container } = render(
      <ValuationCard
        report={baseReport({
          valuation: { pe_ttm: "无", pe_percentile: "—", peer_position: "", verdict: "无数据", basis: "" },
        })}
      />,
    );
    const text = container.querySelector(".wb-valuation")!.textContent!;
    expect(text).toContain("估值指标缺失");
    expect(text).toContain("无估值指标数据");
  });
});

describe("FollowUpPanel（v32 §三 追问复盘）", () => {
  const turn: AnalysisTurn = {
    ok: true,
    analysis_turn_id: "t1",
    parent_report_id: "r1",
    base_report_version: 1,
    turn_index: 1,
    question: "如果行业价格战重启会怎样？",
    supplement_evidence: [{
      supplement_id: "U1", origin: "user", text: "头部企业重启价格战", source_name: "用户自述",
      url: "", event_date: "2026-09-10", input_at: "2026-09-13T00:00:00Z",
      verified: false, trust_label: "用户补充材料/未独立验证",
    }],
    fact_normalizations: [],
    affected_claims: [
      { claim_id: "C1", effect: "weakens", reason: "单票收入承压" },
      { claim_id: "C2", effect: "cannot_judge", reason: "证据不足" },
    ],
    dropped_claim_ids: ["C99"],
    conclusion_change: "weakened",
    scenario_changes: [{ name: "中性", change: "盈利增速假设下修" }],
    new_watchpoints: [{ signal: "单票收入同比", verify_by: "2026-10-31", expected_if_true: "继续下行则进一步削弱", due_on: "2026-10-31", status: "pending_review" }],
    answer: "**先说结论**：营收判断削弱。",
    limitations: ["补充材料未经独立验证"],
    model: "test-model",
    prompt_version: "1.0",
    created_at: "2026-09-13T08:00:00Z",
  };

  it("附录展示：结论变化、受影响判断、未验证标签、自造编号剔除提示", () => {
    render(<FollowUpPanel reportId="r1" turns={[turn]} loading={false} busy={false} error={null} draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />);
    expect(screen.getByText("附录 #1")).toBeTruthy();
    expect(screen.getByText(/结论：结论减弱/)).toBeTruthy();
    // 削弱：主区展开一次 + 审计细节里逐条留痕一次（Q02 折叠，不删）
    expect(screen.getAllByText("削弱").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("2026-10-31")).toBeTruthy();
    // 补充材料信任标签 + 底部说明都含该文案，按出现次数断言（标签 + 说明 ≥ 2）
    expect(screen.getAllByText(/用户补充材料\/未独立验证/).length).toBeGreaterThanOrEqual(2);
    // Q04：无法判断不再单独成行刷屏，与共同限制合并成一行（审计细节里仍可逐条回查）
    expect(screen.getByText(/未取得可改变这些判断的新材料/).textContent).toContain("C2");
    expect(screen.getByText(/已剔除 1 条模型自造的判断编号/)).toBeTruthy();
  });

  it("提问与提交：模板填充问题、补充材料随提交传出、成功后清空输入", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(true);
    // 受控草稿需要真实的 state 容器（模拟调用方按报告保存草稿的行为）
    function Harness() {
      const [draft, setDraft] = useState(EMPTY_FOLLOWUP_DRAFT);
      return <FollowUpPanel reportId="r1" turns={[]} loading={false} busy={false} error={null} draft={draft} onDraftChange={setDraft} onSubmit={onSubmit} />;
    }
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "追问核心判断依据" }));
    const questionBox = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(questionBox.value).toContain("最不可靠");
    // 添加一条补充材料
    await user.click(screen.getByRole("button", { name: /\+ 添加补充材料/ }));
    const textareas = screen.getAllByRole("textbox") as HTMLTextAreaElement[];
    await user.type(textareas[1]!, "美联储加息 50bp");
    await user.click(screen.getByRole("button", { name: "提交追问" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const [, supplements] = onSubmit.mock.calls[0]!;
    expect(supplements[0]).toMatchObject({ text: "美联储加息 50bp" });
    // 成功后问题输入被清空
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  });

  it("列表与详情分离：默认选中最新一条，其余不同时展开（A13）", () => {
    const secondTurn: AnalysisTurn = {
      ...turn,
      analysis_turn_id: "t2",
      turn_index: 2,
      question: "只看 3~5 年价值逻辑还成立吗？",
      conclusion_change: "strengthened",
      created_at: "2026-09-13T09:00:00Z",
    };
    render(<FollowUpPanel reportId="r1" turns={[turn, secondTurn]} loading={false} busy={false} error={null} draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />);
    // 详情默认展示最新一条（附录 #2）；旧附录只有列表条目，不同时展开全文
    expect(screen.getByText("附录 #2")).toBeTruthy();
    expect(screen.queryByText("附录 #1")).toBeNull();
    // 列表里两条都在；选中条目的问题同时出现在列表与详情
    expect(screen.getByText("如果行业价格战重启会怎样？")).toBeTruthy();
    expect(screen.getAllByText("只看 3~5 年价值逻辑还成立吗？").length).toBeGreaterThanOrEqual(2);
  });

  it("失败时保留输入并展示错误提示（原报告保持不变的口径如实传达）", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(false);
    render(<FollowUpPanel reportId="r1" turns={[]} loading={false} busy={false} error="追问未完成：模型调用或解析未通过（原报告保持不变）。请调整问题或补充材料后重试。" draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={onSubmit} />);
    const questionBox = screen.getByRole("textbox") as HTMLTextAreaElement;
    await user.type(questionBox, "测试问题");
    fireEvent.click(screen.getByRole("button", { name: "提交追问" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("buildStockReportMarkdown · v32 导出", () => {
  it("导出包含驾驶舱行、估值状态与追问附录（未验证标签保留文字）", () => {
    const report = baseReport({
      pillar_verdicts: { fundamental: "偏强", valuation: "无法判断", technical: "确认", horizon: ["3~5年"] },
      valuation: {
        pe_ttm: "19.73", pe_percentile: "6.7% 分位", peer_position: "", verdict: "偏便宜", basis: "",
        normalized_earnings: { value: 1.1, basis: "剔除政府补助", confidence: "medium" },
        margin_of_safety: 0.12,
        valuation_status: "undervalued",
      },
    });
    const md = buildStockReportMarkdown(report, [{
      ...({} as AnalysisTurn),
      analysis_turn_id: "t1",
      parent_report_id: report.symbol,
      base_report_version: 1,
      turn_index: 1,
      question: "价格战重启会怎样？",
      supplement_evidence: [],
      fact_normalizations: [],
      affected_claims: [{ claim_id: "C1", effect: "weakens", reason: "单票收入承压" }],
      dropped_claim_ids: [],
      conclusion_change: "weakened",
      scenario_changes: [],
      new_watchpoints: [],
      answer: "结论往谨慎方向修正。",
      limitations: [],
      model: "m",
      created_at: "2026-09-13T08:00:00Z",
    }]);
    expect(md).toContain("驾驶舱：基本面 偏强 · 价值估值 无法判断 · 技术状态 确认（适用期限：3~5年）");
    expect(md).toContain("估值状态 | 低估（正常化口径）");
    expect(md).toContain("正常化盈利 | 1.1 亿/年");
    expect(md).toContain("安全边际：12.0%");
    expect(md).toContain("追问分析附录（不可变追加记录）");
    expect(md).toContain("附录 #1 · 结论减弱");
    // Q04：影响表带判断名称列（编号配合名称展示），三列老格式已被四列取代
    expect(md).toContain("| C1 | — | 削弱 | 单票收入承压 |");
  });

  it("旧记录 + 无追问时导出不含 v32 区块（向后兼容）", () => {
    const md = buildStockReportMarkdown(baseReport());
    expect(md).not.toContain("驾驶舱");
    expect(md).not.toContain("追问分析附录");
    expect(md).not.toContain("估值状态");
  });
});

/* ==========================================================================
   v39（2026-09-19 研报与追问质量路线图 P0/P1）：Q02/Q03/Q04/Q05/Q06/Q07/Q08/Q09/Q10
   回归素材取自用户实拍的那条追问（「现在可以买入吗，是不是双十一的时候股会涨？」）：
   5 条判断全部「无法判断」、答案被压到折叠线以下、凭空出现的 11-30 截止日。
   ========================================================================== */

/** 1.2 契约的样报附录：C1—C5 全部无法判断，另有一条真正被削弱的判断。 */
function v12Turn(overrides: Partial<AnalysisTurn> = {}): AnalysisTurn {
  const cannotJudge = (id: string, name: string): FollowupClaim => ({
    claim_id: id, claim_name: name, effect: "cannot_judge", reason: "本轮无新材料，无法判断",
  });
  return {
    ok: true,
    analysis_turn_id: "t9",
    parent_report_id: "r1",
    base_report_version: 3,
    turn_index: 1,
    question: "现在可以买入吗，是不是双十一的时候股会涨？",
    supplement_evidence: [],
    fact_normalizations: [],
    affected_claims: [
      { claim_id: "C1", effect: "weakens", reason: "单票收入仍在同比下降通道" },
      { claim_id: "C2", effect: "cannot_judge", reason: "本轮无新材料，无法判断" },
      { claim_id: "C3", effect: "cannot_judge", reason: "本轮无新材料，无法判断" },
      { claim_id: "C4", effect: "cannot_judge", reason: "本轮无新材料，无法判断" },
      { claim_id: "C5", effect: "cannot_judge", reason: "本轮无新材料，无法判断" },
    ],
    expanded_claims: [{ claim_id: "C1", claim_name: "单票收入企稳支撑盈利修复", effect: "weakens", reason: "单票收入仍在同比下降通道" }],
    unresolved_claims: [
      cannotJudge("C2", "旺季业务量抬升单票收入"),
      cannotJudge("C3", "价格战已缓和"),
      cannotJudge("C4", "成本管控延续"),
      cannotJudge("C5", "产能利用率改善"),
    ],
    shared_limitation: "C2、C3、C4、C5：本轮沿用原报告判断，未更新行情——未取得可改变这些判断的新材料，原结论按未复核处理（不因此转为支持或削弱）。",
    dropped_claim_ids: [],
    conclusion_change: "unchanged",
    revision_status: "unrevised",
    revision_status_label: "原判断未修订",
    answerability: "partially_answered",
    answerability_label: "部分回答（存在未证实环节）",
    data_basis: "report_only",
    data_basis_label: "沿用原报告判断，未更新行情",
    state_line: "沿用原报告判断，未更新行情（未复核最新数据）",
    status_conflict: "",
    direct_answer: "不能给出「可买」结论：旺季效应无当年证据，只看得到 2026-09-19 快照。",
    key_conditions: ["10 月业务量简报确认量价同步改善（数据截至 2026-09-19 未覆盖）"],
    evidence_gaps: ["缺 2026 年双十一期间的单价与业务量数据"],
    scenario_changes: [],
    new_watchpoints: [{
      signal: "10 月业务量与单票收入（量增价稳口径）",
      verify_by: "10 月经营简报披露后",
      expected_if_true: "单票收入环比转正则盈利修复成立",
      date_basis: "disclosed_schedule",
      date_basis_label: "已披露的披露/事件日程",
      date_kind: "factual",
      date_note: "日期来自已披露的月度经营简报日程。",
      date_demoted_from: "2026-11-30",
      assumptions: ["量增价稳＝业务量上升且单票收入基本持平"],
      due_on: "",
      status: "pending_review",
    }],
    price_refs: [{
      level: 14.9, role: "ma", as_of: "2026-09-19", window: "未来 5–10 个交易日",
      snapshot_only: true, forward_looking: true,
      note: "本轮未刷新行情，该价位为历史快照，不作为未来时点的实时阈值。",
    }],
    valuation_view: {
      state: "normalization_unverified",
      label: "指标已取得，合理价值尚未评估",
      verdict_display: "合理价值未评估（指标已有，缺正常化盈利验证）",
      note: "pe_ttm 11.70｜pe_percentile 近5年 1.7% 已取得，但正常化盈利未经验证。",
      metrics: { pe_ttm: "11.70" },
      metric_count: 1,
    },
    probability_view: {
      calibrated: false,
      probability_label: "倾向（未统计校准）",
      note: "无历史基准：本期尚无同类事件/同市场状态的后验统计，故只用低/中/高表达，不给出小数概率。",
      scenarios: [{
        name: "双十一上涨", probability_level: "中", probability_display: "中（未统计校准，非上涨概率）",
        probability_basis: "none",
        bounds: [
          { side: "low", value: 14.2, kind: "technical", note: "" },
          { side: "high", value: 18.95, kind: "reference", note: "18.95 来自「历史高点」，只是历史参考位：该情景未给出推导过程，只作参考压力/支撑位，不作为未来窗口的目标价。" },
        ],
      }],
      reference_bounds: [{ scenario: "双十一上涨", level: 18.95, note: "18.95 来自「历史高点」，只是历史参考位" }],
    },
    data_as_of: "2026-09-19",
    mode: "interpret",
    mode_label: "解读本报告（只依据原报告与你的补充材料，不获取新事实）",
    retrieval_status: { requested: 0, succeeded: 0, failed: 0, state: "none", note: "", failed_items: [] },
    evidence_snapshot: [],
    source_catalog: [
      {
        source_id: "S1", name: "腾讯财经行情", url: "https://qt.gtimg.cn/q=sz002468",
        data_date: "2026-09-19", retrieved_at: "2026-09-19T18:10:21", quality: "C",
        scope_note: "前复权日线", cited: true, used_by: ["C1"],
      },
      {
        source_id: "S5", name: "东方财富估值", url: "", url_note: "来源未提供可访问链接（如实标注，不猜测）",
        data_date: "2026-09-18", retrieved_at: "2026-09-19T18:10:22", quality: "C",
        scope_note: "", cited: true, used_by: ["C2"],
      },
    ],
    answer: "**先说结论**：不构成买卖建议；双十一效应无法用现有证据确认。",
    limitations: ["本轮未刷新行情"],
    model: "test-model",
    prompt_version: "1.2",
    created_at: "2026-09-19T18:18:44Z",
    ...overrides,
  } as AnalysisTurn;
}

describe("AnalysisTurnCard · Q02 回答前置与审计折叠", () => {
  it("直接回答排在判断影响表之前（首屏能看到答案）", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[v12Turn()]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const card = container.querySelector(".wb-followup-turn")!;
    const html = card.innerHTML;
    const direct = html.indexOf("不能给出「可买」结论");
    const auditTable = html.indexOf("<table");
    expect(direct).toBeGreaterThan(-1);
    expect(auditTable).toBeGreaterThan(direct);
    // 状态说明行紧跟问题，且明确「未复核最新数据」
    const state = card.querySelector(".wb-followup-state")!.textContent!;
    expect(state).toContain("沿用原报告判断，未更新行情");
    expect(state).toContain("未复核最新数据");
  });

  it("判断影响表与模型/提示词版本落在可展开的审计细节里", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[v12Turn()]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const audit = container.querySelector("details.wb-followup-audit")!;
    expect(audit.querySelector("summary")!.textContent).toContain("审计细节（判断影响表 · 模型与提示词版本）");
    // 审计细节里逐条留痕（模型/提示词版本 + 完整判断影响表），默认折叠但不删除
    expect(audit.textContent).toContain("提示词 1.2");
    expect(audit.querySelector("table")!.textContent).toContain("单票收入企稳支撑盈利修复");
    expect(audit.getAttribute("open")).toBeNull();
  });

  /** Q04：样报里 5 行全说「无法判断」的判断影响表压住了回答本体；现在只留一行共同限制。 */
  it("五条无法判断合并成一行共同限制，不再逐条刷屏，也不被改判成支持/削弱", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[v12Turn()]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const main = container.querySelector(".wb-followup-turn") as HTMLElement;
    const collapsed = main.querySelector(".wb-followup-unresolved")!;
    expect(collapsed.textContent).toContain("C2、C3、C4、C5：本轮沿用原报告判断，未更新行情");
    expect(collapsed.textContent).toContain("旺季业务量抬升单票收入");
    // 主区里「无法判断」每档只出现一次（清单里的口径标签），不再有 4 张相同的行
    expect(collapsed.querySelectorAll(".wb-followup-unresolved-item").length).toBe(4);
    // 不重判：四条仍是无法判断，只有一条被削弱的判断被展开
    expect(within(main).getAllByText("无法判断").length).toBe(4);
    expect(within(main).getAllByText("削弱").length).toBeGreaterThanOrEqual(1);
  });

  it("Q06/Q07/Q08：价位带截至与快照口径、被降级的 11-30 留痕、倾向不写成概率、18.95 标为参考压力位", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[v12Turn()]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const text = container.querySelector(".wb-followup-turn")!.textContent!;
    expect(text).toContain("历史快照（本轮未刷新）");
    expect(text).toContain("2026-09-19");
    expect(text).toContain("原值 2026-11-30 已降级");
    expect(text).toContain("量增价稳＝业务量上升且单票收入基本持平");
    expect(text).toContain("倾向（未统计校准）");
    expect(text).toContain("参考压力位");
    // Q05：估值三态而不是「无数据」
    expect(text).toContain("合理价值未评估（指标已有，缺正常化盈利验证）");
    expect(text).not.toContain("无数据");
  });

  it("Q09：屏显来源目录给出真实链接，缺链接处如实标注未提供", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[v12Turn()]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const link = container.querySelector("a[href='https://qt.gtimg.cn/q=sz002468']");
    expect(link).toBeTruthy();
    expect(container.textContent).toContain("（未提供链接");
  });
});

describe("AnalysisTurnCard · 旧 1.1 记录回退（Q03 验收「历史记录可兼容显示」）", () => {
  /** 1.1 落库的真实形状：三条状态轴、直接回答、分组、价位、来源目录全都没有。 */
  const legacyTurn: AnalysisTurn = {
    ok: true,
    analysis_turn_id: "t-legacy",
    parent_report_id: "r1",
    base_report_version: 1,
    turn_index: 1,
    question: "补充材料进来后结论要不要改？",
    supplement_evidence: [],
    fact_normalizations: [],
    affected_claims: [
      { claim_id: "C1", effect: "cannot_judge", reason: "证据不足" },
      { claim_id: "C2", effect: "cannot_judge", reason: "证据不足" },
    ],
    dropped_claim_ids: [],
    conclusion_change: "unchanged",
    scenario_changes: [],
    new_watchpoints: [{ signal: "单票收入", verify_by: "三季报披露后", expected_if_true: "环比转正" }],
    answer: "旧版回答正文。",
    limitations: [],
    model: "old-model",
    prompt_version: "1.1",
    created_at: "2026-09-13T08:00:00Z",
  };

  it("缺状态轴时回退为「沿用原报告判断，未更新行情」+「旧版记录未评估」，不暗示已复核", () => {
    const axes = followupStatusAxes(legacyTurn);
    expect(axes.legacy).toBe(true);
    expect(axes.dataBasis.status).toBe("report_only");
    expect(axes.stateLine).toContain("沿用原报告判断，未更新行情");
    expect(axes.stateLine).toContain("不代表已复核最新数据");
    expect(axes.answerability.label).toBe(LEGACY_ANSWERABILITY_LABEL);
    expect(axes.revision.label).toBe("原判断未修订");
  });

  it("渲染不崩、不出现 undefined，且不编造直接回答/估值三态/来源目录", () => {
    const { container } = render(
      <FollowUpPanel reportId="r1" turns={[legacyTurn]} loading={false} busy={false} error={null}
        draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />,
    );
    const text = container.textContent!;
    expect(text).toContain("旧版回答正文。");
    expect(text).toContain("旧版记录未评估");
    expect(text).not.toMatch(/undefined/);
    // 旧记录没有 direct_answer / valuation_view / source_catalog / data_as_of：新区块整体不渲染
    expect(container.querySelector(".wb-followup-direct")).toBeNull();
    expect(container.querySelector(".wb-followup-turn table caption")).toBeNull();
    expect(text).not.toContain("来源目录");
    expect(text).not.toContain("数据截至");
    // 无补充材料的旧记录不能出现「使用用户补充材料」口径
    expect(text).not.toContain("使用用户补充材料");
  });

  it("带补充材料的旧记录按 user_supplement 口径回退（与导出同一函数）", () => {
    const axes = followupStatusAxes({ ...legacyTurn, supplement_evidence: [{ ...legacySupplement }] });
    expect(axes.dataBasis.status).toBe("user_supplement");
    expect(axes.dataBasis.label).toBe(DATA_BASIS_LABEL.user_supplement);
    expect(axes.stateLine).toContain("未更新行情");
  });
});

const legacySupplement: FollowUpSupplement = {
  supplement_id: "U1", origin: "user", text: "头部企业重启价格战", source_name: "用户自述",
  url: "", event_date: "2026-09-10", input_at: "2026-09-13T00:00:00Z",
  verified: false, trust_label: "用户补充材料/未独立验证",
};

describe("buildStockReportMarkdown · v39 追问附录与来源目录", () => {
  it("导出顺序与卡片一致：直接回答在判断表之前，无法判断合并为一行", () => {
    const md = buildStockReportMarkdown(baseReport(), [v12Turn()]);
    const directAt = md.indexOf("**直接回答：**");
    const tableAt = md.indexOf("| 编号 | 判断名称 | 影响 | 理由 |");
    expect(directAt).toBeGreaterThan(-1);
    expect(tableAt).toBeGreaterThan(directAt);
    expect(md).toContain("C2、C3、C4、C5：本轮沿用原报告判断，未更新行情");
    expect(md).toContain("**未展开的判断（C2、C3、C4、C5）**");
    // 五档不再逐条刷屏：只展开被削弱的那一条
    expect(md).toContain("| C1 | 单票收入企稳支撑盈利修复 | 削弱 | 单票收入仍在同比下降通道 |");
    expect(md).not.toContain("| 本轮无新材料，无法判断 |");
  });

  it("屏显与导出状态措辞同源（followupStatusAxes 一份实现）", () => {
    const md = buildStockReportMarkdown(baseReport(), [v12Turn()]);
    const axes = followupStatusAxes(v12Turn());
    expect(md).toContain(axes.stateLine);
    expect(md).toContain(`修订「${axes.revision.label}」 · 可回答「${axes.answerability.label}」`);
    const legacyMd = buildStockReportMarkdown(baseReport(), [{ ...v12Turn(), revision_status: undefined, answerability: undefined, state_line: undefined, data_basis: undefined }]);
    expect(legacyMd).toContain("沿用原报告判断，未更新行情（未更新行情，不代表已复核最新数据）（1.1 旧版记录，未重新评估）");
  });

  it("验证点日期口径、价位时点表、倾向列名与参考位、估值三态全部随导出走", () => {
    const md = buildStockReportMarkdown(baseReport(), [v12Turn()]);
    expect(md).toContain("| 观察信号 | 验证时点 | 日期类型 | 日期依据 | 若成立则预期 | 假设拆分 |");
    expect(md).toContain("事实时间");
    expect(md).toContain("已披露的披露/事件日程");
    expect(md).toContain("原值 2026-11-30 已降级");
    expect(md).toContain("量增价稳＝业务量上升且单票收入基本持平");
    expect(md).toContain("| 价位 | 是什么位 | 数据截至 | 适用窗口 | 口径 | 说明 |");
    expect(md).toContain("均线");
    expect(md).toContain("历史快照（本轮未刷新）");
    expect(md).toContain("| 情景 | 倾向（未统计校准） | 区间下界 | 区间上界 |");
    expect(md).toContain("参考压力位 18.95");
    expect(md).toContain("**本次估值口径（三态）**：指标已取得，合理价值尚未评估 · 合理价值未评估（指标已有，缺正常化盈利验证）");
    expect(md).toContain("**问：** 现在可以买入吗，是不是双十一的时候股会涨？");
  });

  it("Q09：来源目录含真实链接，缺链接如实标注（未提供链接）；不伪造 URL", () => {
    const md = buildStockReportMarkdown(baseReport({
      source_catalog: [
        { source_id: "S1", name: "腾讯财经行情", url: "https://qt.gtimg.cn/q=sz002468", data_date: "2026-09-19", retrieved_at: "2026-09-19T18:10:21", quality: "C", scope_note: "前复权日线", cited: true, used_by: ["C1"] },
        { source_id: "S2", name: "公司公告", url: "", url_note: "来源未提供可访问链接（如实标注，不猜测）", data_date: "2026-09-01", retrieved_at: "2026-09-19T18:10:22", quality: "B", scope_note: "", cited: false, used_by: [] },
      ],
    }), [v12Turn()]);
    expect(md).toContain("## 来源目录");
    expect(md).toContain("https://qt.gtimg.cn/q=sz002468");
    expect(md).toContain("| S2 | 公司公告 | （未提供链接：来源未提供可访问链接（如实标注，不猜测））");
    expect(md).toContain("| 可得未引用 |");
    expect(md).toContain("| 已引用 | C1 |");
    // 附录自带来源目录（本次分析可用的来源），标题层级低于研报节
    expect(md).toContain("#### 本附录来源目录");
  });

  it("Q09：核心结论判定「2/3」点名被统计的判断", () => {
    const md = buildStockReportMarkdown(baseReport({
      claims: [
        { claim_id: "C1", text: "a", sources: ["S1"], dropped_sources: [], requires: ["valuation"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", importance: "high", support: "full" },
        { claim_id: "C2", text: "b", sources: ["S1"], dropped_sources: [], requires: ["valuation"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", importance: "high", support: "full" },
        { claim_id: "C3", text: "c", sources: [], dropped_sources: [], requires: ["valuation"], missing_coverage: ["valuation"], evidence_quality: null, status: "no_source", note: "", importance: "high", support: "none" },
      ] as never,
      claim_findings: {
        claims: [
          { claim_id: "C1", text: "a", sources: ["S1"], dropped_sources: [], requires: ["valuation"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", unknown_requires: [], importance: "high", support: "full" },
          { claim_id: "C2", text: "b", sources: ["S1"], dropped_sources: [], requires: ["valuation"], missing_coverage: [], evidence_quality: "B", status: "supported", note: "", unknown_requires: [], importance: "high", support: "full" },
          { claim_id: "C3", text: "c", sources: [], dropped_sources: [], requires: ["valuation"], missing_coverage: ["valuation"], evidence_quality: null, status: "no_source", note: "", unknown_requires: [], importance: "high", support: "none" },
        ],
        counts: { supported: 2, downgraded: 0, no_source: 1 },
        warnings: [],
      } as never,
    }));
    expect(md).toContain("核心结论判定：部分支撑（2/3）");
    expect(md).toContain("计入统计的核心判断：C1、C2、C3（完整支撑 C1、C2；未完整支撑 C3）");
    expect(md).toContain("是 · 计入分子（完整支撑）");
    expect(md).toContain("是 · 仅计入分母");
  });

  it("Q11：补充研究的补证计数与失败项如实导出", () => {
    const md = buildStockReportMarkdown(baseReport(), [v12Turn({
      mode: "supplement_research",
      mode_label: "补充研究",
      data_basis: "refreshed_data",
      data_basis_label: "使用本次刷新获取的数据",
      state_line: "使用本次刷新获取的数据",
      retrieval_status: {
        requested: 3, succeeded: 1, failed: 2, state: "partial", note: "两项取数失败，未用推测填补。",
        failed_items: [{ kind: "announcement", label: "公告", error: "上游 502" }],
      },
      evidence_snapshot: [{
        kind: "price", label: "日线行情", source_name: "腾讯财经", url: "https://qt.gtimg.cn/q=sz002468",
        event_date: "2026-09-19", retrieved_at: "2026-09-19T18:18:00", trust_label: "系统取数（未独立核验）",
        status: "ok", error: "", supported_claims: ["C1"], analysis_version: "1.2",
      }],
    })]);
    expect(md).toContain("- 入口：补充研究");
    expect(md).toContain("补证 1/3 成功");
    expect(md).toContain("部分失败");
    expect(md).toContain("公告（上游 502）");
    expect(md).toContain("日线行情");
    expect(md).toContain("https://qt.gtimg.cn/q=sz002468");
  });
});

describe("FollowUpPanel · Q10 两种追问入口", () => {
  it("默认「解读本报告」，切换到补充研究后随提交带出 mode", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(true);
    function Harness() {
      const [draft, setDraft] = useState<FollowUpDraft>(EMPTY_FOLLOWUP_DRAFT);
      return <FollowUpPanel reportId="r1" turns={[]} loading={false} busy={false} error={null} draft={draft} onDraftChange={setDraft} onSubmit={onSubmit} />;
    }
    render(<Harness />);
    const interpret = screen.getByRole("radio", { name: "解读本报告" });
    const research = screen.getByRole("radio", { name: "补充研究" });
    expect(interpret.getAttribute("aria-checked")).toBe("true");
    expect(research.getAttribute("aria-checked")).toBe("false");
    expect(screen.getByText("只依据原报告与你的补充材料作答：不更新行情、不获取新事实。")).toBeTruthy();
    await user.click(research);
    expect(screen.getByText(/本次会重新获取行情\/公告等数据/)).toBeTruthy();
    await user.type(screen.getByRole("textbox"), "双十一有过证据吗？");
    await user.click(screen.getByRole("button", { name: "提交追问" }));
    expect(onSubmit).toHaveBeenCalledWith("双十一有过证据吗？", [], "supplement_research");
  });
});

/* ==========================================================================
   v39 Q13（2026-09-19 路线图）：催化事件证据链——缺哪环标哪环，
   越界断言（无预期数据却写「超预期」、链条断了却写「必涨」）由服务端点破，
   但**不改写模型正文**：屏显与导出必须同时看到链条与标注。
   ========================================================================== */

const CATALYST_CHAIN = {
  steps: [
    { key: "volume", label: "业务量", status: "evidenced", evidence: "8 月业务量同比 +18%" },
    { key: "price_cost", label: "价格与成本", status: "evidenced", evidence: "单票收入同比 -3%（量增价降）" },
    { key: "profit", label: "利润", status: "evidenced", evidence: "归母净利 10.35 亿元" },
    { key: "expectation", label: "市场预期", status: "missing", note: "缺一致预期或已披露预告，不能宣称超预期。" },
    { key: "price_reaction", label: "价格反应", status: "missing", note: "缺区间价格反应统计。" },
  ],
  complete: false,
  broken_at: "expectation",
  note: "任一环缺失时结论只能写到该环为止。",
  guardrail: "禁止由「收入增长」直接推导「股价必涨」。",
};

describe("FollowUpPanel · Q13 催化证据链与越界标注", () => {
  it("缺环显眼标出、护栏与服务端标注都可见，正文逐字不改", () => {
    const turn = v12Turn({
      catalyst_chain: CATALYST_CHAIN,
      answer_flags: ["正文出现「超预期」表述，但本轮无一致预期可依：该说法不构成超预期判断。"],
      answer: "双十一的量与价都在改善，股价必涨。",
    });
    render(<FollowUpPanel reportId="r1" turns={[turn]} loading={false} busy={false} error={null} draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />);
    expect(screen.getByText(/催化证据链（业务量 → 价格与成本 → 利润 → 市场预期 → 价格反应）/)).toBeTruthy();
    expect(screen.getAllByText("缺证据")).toHaveLength(2);
    expect(screen.getByText(/不能宣称超预期/)).toBeTruthy();
    expect(screen.getByText(/护栏：禁止由「收入增长」直接推导/)).toBeTruthy();
    expect(screen.getByText(/服务端标注（不改写正文）/)).toBeTruthy();
    expect(screen.getByText(/不构成超预期判断/)).toBeTruthy();
    // 被点破的是断言，正文仍按模型原话展示（不改写、不删除）。
    expect(screen.getByText("双十一的量与价都在改善，股价必涨。")).toBeTruthy();
  });

  it("导出附录同样带证据链与服务端标注（屏显与导出同义）", () => {
    const markdown = buildStockReportMarkdown(baseReport(), [v12Turn({
      catalyst_chain: CATALYST_CHAIN,
      answer_flags: ["正文出现「必涨」式断言，而证据链在 expectation 环节即缺证据。"],
    })]);
    expect(markdown).toContain("催化证据链");
    expect(markdown).toContain("市场预期：缺证据");
    expect(markdown).toContain("护栏：禁止由「收入增长」直接推导");
    expect(markdown).toContain("服务端标注（不改写正文）");
  });
});

/* ==========================================================================
   v39 Q07（后半句验收）：重复验证点要可识别、触发条件与预期方向要一致。
   条目一律保留（删不删由人判断），服务端只负责点名。
   ========================================================================== */

describe("FollowUpPanel · Q07 验证点重复与方向核对", () => {
  const reviewTurn = v12Turn({
    new_watchpoints: [
      {
        signal: "双十一业务量与单票收入是否量价齐升", verify_by: "11 月经营简报披露后",
        expected_if_true: "量价齐升则强化偏多结论", due_on: "", status: "pending_review",
        date_kind: "event", date_basis: "", date_note: "", date_demoted_from: "",
        duplicate_of: 0, direction_note: "与第 2 条同一时点但预期相反（一条偏强化、一条偏削弱）：两条都要成立需各自限定触发条件，不能合成一句结论。",
        assumptions: ["量价齐升＝业务量上升且单票收入上升"],
      },
      {
        signal: "双十一前后是否放量跌破 MA20 或 14.07", verify_by: "11 月经营简报披露后",
        expected_if_true: "放量跌破则短期转弱", due_on: "", status: "pending_review",
        date_kind: "event", date_basis: "", date_note: "", date_demoted_from: "",
        duplicate_of: 1, direction_note: "与第 1 条为同一信号（文本去标点后一致），合并看待即可，不必重复占位。",
        assumptions: [],
      },
    ],
  });

  it("卡片里给出「与第 N 条重复」标签与方向核对说明，两条都不被删", () => {
    render(<FollowUpPanel reportId="r1" turns={[reviewTurn]} loading={false} busy={false} error={null} draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />);
    expect(screen.getByText("与第 1 条重复")).toBeTruthy();
    expect(screen.getAllByText(/同一时点但预期相反/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("双十一前后是否放量跌破 MA20 或 14.07")).toBeTruthy();
    expect(screen.getByText("双十一业务量与单票收入是否量价齐升")).toBeTruthy();
  });

  it("导出同样点名重复与相反预期（与屏显共用 watchpointReview）", () => {
    const markdown = buildStockReportMarkdown(baseReport(), [reviewTurn]);
    expect(markdown).toContain("重复验证点");
    expect(markdown).toContain("与第 1 条为同一信号");
    expect(markdown).toContain("同一时点但预期相反");
  });
});

describe("AnalysisTurnCard · 不适用与「没取到」要分开（方向研判追问）", () => {
  it("后端把 valuation_view/probability_view 置 null 时，估值口径与倾向表整块不渲染", () => {
    const turn = v12Turn({ valuation_view: null, probability_view: null, price_refs: [] });
    render(<FollowUpPanel reportId="r1" turns={[turn]} loading={false} busy={false} error={null} draft={EMPTY_FOLLOWUP_DRAFT} onDraftChange={() => {}} onSubmit={vi.fn()} />);
    // 方向研判没有估值/情景结构：写「估值指标缺失」会把「不适用」说成「没取到」。
    expect(screen.queryByText(/本次估值口径/)).toBeNull();
    expect(screen.queryByText(/不是上涨概率/)).toBeNull();
    expect(screen.queryByText(/本次引用的价位与时点/)).toBeNull();
    // 其余部分照常渲染，不因缺视图字段而崩。
    expect(screen.getByText("直接回答")).toBeTruthy();
    expect(screen.getAllByText(/沿用原报告判断，未更新行情/).length).toBeGreaterThanOrEqual(2);
  });
});
