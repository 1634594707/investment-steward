/**
 * K06（2026-09-18 排版与研报呈现一致性路线图）：宽度档单一来源。
 * tierForWidth 是纯函数——AppShell 的 narrow（<1024）与 CSS body[data-width-tier] 共用它，
 * sm 上界=1024 与 narrow 对齐，消除「壳窄而内容仍多列」的两套并行错位。
 */
import { describe, expect, it } from "vitest";
import { WIDTH_BREAKPOINTS, tierForWidth } from "../state/widthTier";

describe("tierForWidth", () => {
  it("四档边界 720/1024/1280/1600（左闭右开）", () => {
    expect(WIDTH_BREAKPOINTS).toEqual({ sm: 720, md: 1024, lg: 1280, xl: 1600 });
    expect(tierForWidth(600)).toBe("xs");
    expect(tierForWidth(719)).toBe("xs");
    expect(tierForWidth(720)).toBe("sm");
    expect(tierForWidth(950)).toBe("sm");   // 曾「壳窄内容仍多列」的过渡带
    expect(tierForWidth(1023)).toBe("sm");
    expect(tierForWidth(1024)).toBe("md");  // 恰为 AppShell 进入宽布局的阈值
    expect(tierForWidth(1279)).toBe("md");
    expect(tierForWidth(1280)).toBe("lg");
    expect(tierForWidth(1599)).toBe("lg");
    expect(tierForWidth(1600)).toBe("xl");
  });
});
