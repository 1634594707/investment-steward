/**
 * T1（用户视角路线图 2026-09-26）：背景层分组卡「实拉」分母必须按**声明数**。
 *
 * 改造前分母用的是过滤后的行数，而 Core 侧本就会滤掉全部非 ok 行，于是
 * 「声明 4 项、只拉到 1 项」被渲染成绿色「1/1 实拉」，空组穿「0/0 实拉」健康色。
 * 这里锁定四件事：分母、计数、未接入点名、健康色判据。
 */
import { describe, expect, it } from "vitest";
import { summarizeBackgroundGroup } from "./MacroPage";

/** Core 只会下发 status === "ok" 的行（macro_feed 返回前过滤）。这里复刻该形状。 */
const onlyOk = (...keys: string[]) =>
  keys.map((key) => ({ key, status: "ok", label: key, latest: 1, obs_date: "2026-09-26" }));

const FX_GROUP = { keys: ["dxy_twi", "cfets_rmb", "eur_fx", "jpy_fx"] };

describe("summarizeBackgroundGroup · 分母按声明数而非到达行数", () => {
  it("只拉到 1 项时报 1/4，而不是被过滤塌成 1/1", () => {
    const r = summarizeBackgroundGroup(FX_GROUP, onlyOk("dxy_twi"));
    expect(r.declared).toBe(4);
    expect(r.okCount).toBe(1);
    expect(r.allOk).toBe(false);
    expect(r.missing).toEqual(["cfets_rmb", "eur_fx", "jpy_fx"]);
  });

  it("空组报 0/4 且不算健康（旧实现里 [].every() 恒真，会穿 mint 色）", () => {
    const r = summarizeBackgroundGroup(FX_GROUP, []);
    expect(r.declared).toBe(4);
    expect(r.okCount).toBe(0);
    expect(r.allOk).toBe(false);
    expect(r.missing).toEqual(["dxy_twi", "cfets_rmb", "eur_fx", "jpy_fx"]);
  });

  it("全部实拉时才是 N/N 且 allOk", () => {
    const r = summarizeBackgroundGroup(FX_GROUP, onlyOk("dxy_twi", "cfets_rmb", "eur_fx", "jpy_fx"));
    expect(r.okCount).toBe(4);
    expect(r.declared).toBe(4);
    expect(r.allOk).toBe(true);
    expect(r.missing).toEqual([]);
  });

  it("组内 key 顺序按声明顺序返回，不受后端下发顺序影响", () => {
    const r = summarizeBackgroundGroup(FX_GROUP, onlyOk("jpy_fx", "dxy_twi"));
    expect(r.rows.map((row) => row.key)).toEqual(["dxy_twi", "jpy_fx"]);
  });

  it("后端多下的、不属于本组的行不会进入本组计数", () => {
    const r = summarizeBackgroundGroup(FX_GROUP, [...onlyOk("dxy_twi"), ...onlyOk("gold", "wti")]);
    expect(r.declared).toBe(4);
    expect(r.okCount).toBe(1);
    expect(r.rows.map((row) => row.key)).toEqual(["dxy_twi"]);
  });

  it("分组声明数为 0 时不算健康（避免空声明穿绿）", () => {
    const r = summarizeBackgroundGroup({ keys: [] }, onlyOk("gold"));
    expect(r.declared).toBe(0);
    expect(r.allOk).toBe(false);
  });
});
