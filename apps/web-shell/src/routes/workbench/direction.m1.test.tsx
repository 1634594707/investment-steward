/**
 * M1（2026-09-15 第二轮路线图）B04/B05 前端自动化。
 * B04（2026-09-15 第二轮方向研判路线图 M1）：候选表代码列只有核验通过的 6 位码才显示代码，无效时写「无有效代码」，
 *      禁止把公司名（模型原文）印在代码行——用今晚样报的 payload 作回归基准。
 * B05：补码成功后可勾选/可送雷达；补码失败的仍排除并计入「无效代码 N」。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { DirectionReport } from "./researchTypes";
import { DirectionPoolTable } from "./direction";
import { poolCodeHint, poolCodeText } from "./format";

/** 今晚样报（2026-09-15 22:55）候选池：symbol 被模型写成公司名 → 服务端已补码。 */
function backfilledReport(): DirectionReport {
  return {
    ok: true,
    topic: "低估板块",
    direction_summary: "正文",
    catalysts: [],
    risks: [],
    model: "m",
    generated_at: "2026-09-15T00:00:00Z",
    report_id: "dir-m1",
    stock_pool: [
      { symbol_raw: "中国通号", symbol: "688009", name: "中国通号", sector: "轨交设备Ⅱ", identity_status: "verified", identity_status_label: "身份已核验", symbol_backfilled: true },
      { symbol_raw: "时代电气", symbol: "688187", name: "时代电气", sector: "轨交设备Ⅱ", identity_status: "verified", identity_status_label: "身份已核验", symbol_backfilled: true },
      { symbol_raw: "格力电器", symbol: "000651", name: "格力电器", sector: "白色家电", identity_status: "verified", identity_status_label: "身份已核验", symbol_backfilled: true },
      // 补码失败的一条：证据里没有 → 保持 invalid
      { symbol_raw: "查无此股", symbol: "", name: "查无此股", sector: "未知", identity_status: "invalid", identity_status_label: "无效代码" },
    ],
  } as DirectionReport;
}

describe("poolCodeText（M1-B04 纯函数）", () => {
  it("核验通过的 6 位码才作为代码显示", () => {
    expect(poolCodeText({ symbol: "688009", name: "中国通号", identity_status: "verified" })).toBe("688009");
  });

  it("无效/缺失代码显示「无有效代码」，绝不回落到公司名", () => {
    expect(poolCodeText({ symbol: "中国通号", symbol_raw: "中国通号", name: "中国通号", identity_status: "invalid" })).toBe("无有效代码");
    expect(poolCodeText({ symbol: "", symbol_raw: "中国通号", name: "中国通号", identity_status: "invalid" })).toBe("无有效代码");
  });

  it("旧记录（无 identity_status）：合法 6 位码走前端前缀校验 → 仍显示代码", () => {
    expect(poolCodeText({ symbol: "600519", name: "贵州茅台" })).toBe("600519");
    // 旧记录里 symbol 是公司名 → 无数字 → 无有效代码
    expect(poolCodeText({ symbol: "中国通号", symbol_raw: "中国通号", name: "中国通号" })).toBe("无有效代码");
  });

  it("ETF 代码不是股票候选身份 → 无有效代码", () => {
    expect(poolCodeText({ symbol: "510300", name: "沪深300ETF", identity_status: "invalid" })).toBe("无有效代码");
  });

  it("悬停说明区分「服务端补码」与「模型输出」", () => {
    expect(poolCodeHint({ symbol: "688009", name: "中国通号", identity_status: "verified", symbol_backfilled: true })).toContain("回填");
    expect(poolCodeHint({ symbol: "688009", name: "中国通号", identity_status: "verified" })).toContain("模型输出");
  });
});

describe("DirectionPoolTable 代码列渲染（M1-B04/B05）", () => {
  it("补码成功后名称是中国通号、代码行是 688009；失败行显示「无有效代码」", () => {
    const { container } = render(
      <DirectionPoolTable report={backfilledReport()} onSendToTactics={vi.fn()} onStudySingle={vi.fn()} />,
    );
    const codeCells = Array.from(container.querySelectorAll(".wb-pool-code")).map((el) => el.textContent);
    expect(codeCells).toEqual(["688009", "688187", "000651", "无有效代码"]);
    // 公司名绝不作为代码出现
    expect(codeCells).not.toContain("中国通号");
  });

  it("B05：补码成功可勾选（3 个复选框）；补码失败计入「无效代码 1（不随发送）」", () => {
    render(<DirectionPoolTable report={backfilledReport()} onSendToTactics={vi.fn()} onStudySingle={vi.fn()} />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(3);
    expect(screen.getByText((_, el) => (el?.textContent ?? "").includes("无效代码 1（不随发送）") && el?.children.length === 0)).toBeTruthy();
  });
});
