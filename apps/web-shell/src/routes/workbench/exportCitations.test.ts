import { describe, expect, it } from "vitest";
import { markdownToHtml } from "./export";

const catalog = (sid: string) => [
  "## 来源目录",
  "| 编号 | 名称 | 可用链接 | 数据日期 | 获取时间 | 等级 | 引用状态 | 被哪些判断使用 | 口径限制 |",
  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
  `| ${sid} | 来源 | 未提供链接 | 未标注 | 未标注 | B | 已引用 | C1 | 摘要 |`,
].join("\n");

describe("H03 离线引用跳转", () => {
  it("每个引用有独立返回位置，缺目录项不生成无效链接，目录无需外链", () => {
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml(`正文 [S4]，再次 [S4]，缺项 [S99]。\n${catalog("S4")}`);
    const refs = [...root.querySelectorAll<HTMLAnchorElement>("a.cite")];
    expect(refs).toHaveLength(2);
    expect(refs[0]!.hash).toBe(refs[1]!.hash);
    const source = root.querySelector(refs[0]!.hash)!;
    expect(source.textContent).toContain("未提供链接");
    for (const ref of refs) expect(source.querySelector(`a[href="#${ref.id}"]`)).toBeTruthy();
    expect(root.textContent).toContain("[S99]（来源目录缺项）");
    expect(root.querySelector('a[href^="http"]')).toBeNull();
  });

  it("原报告与追问同名编号分别定位，不跨附录套用来源", () => {
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml(`正文 [S3A]\n${catalog("S3A")}\n### 附录 #1 · 未改变\n追问 [S3A]\n${catalog("S3A")}\n### 附录 #2 · 未改变\n无来源追问 [S3A]`);
    const refs = [...root.querySelectorAll<HTMLAnchorElement>("a.cite")];
    expect(refs).toHaveLength(2);
    expect(refs[0]!.hash).not.toBe(refs[1]!.hash);
    expect(root.textContent).toContain("无来源追问 [S3A]（来源目录缺项）");
    const ids = [...root.querySelectorAll("[id]")].map((el) => el.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("引用形式的真实外链标签不产生嵌套链接", () => {
    const root = document.createElement("div");
    root.innerHTML = markdownToHtml(`[S4](https://example.com/report)\n${catalog("S4")}`);
    expect(root.querySelectorAll("a a")).toHaveLength(0);
    expect(root.querySelector('a[href="https://example.com/report"]')?.textContent).toBe("S4");
  });
});
