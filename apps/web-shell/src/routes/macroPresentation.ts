import type { MacroSnapshot } from "@investment-steward/domain-contracts";

export function macroFrequencyLabel(frequency?: string | null): string {
  const labels: Record<string, string> = { annual: "年度", quarterly: "季度", monthly: "月度", daily: "日度" };
  return frequency ? labels[frequency] ?? frequency : "未标注";
}

export function macroScoreCoverage(snapshot: MacroSnapshot) {
  const dims = ["growth", "employment", "inflation", "monetary"];
  const labels: Record<string, string> = { growth: "增长", employment: "就业", inflation: "通胀", monetary: "货币" };
  const scores = snapshot.positioning?.dims ?? [];
  const active = dims.filter((key) => scores.some((dim) => dim.dim === key && dim.score != null));
  return {
    active: active.map((key) => labels[key]!),
    missing: dims.filter((key) => !active.includes(key)).map((key) => labels[key]!),
    periods: scores.filter((dim) => dim.score != null).flatMap((dim) => dim.indicators_used.map((key) => {
      const row = snapshot.indicators.find((reading) => reading.indicator === key);
      return `${labels[dim.dim] ?? dim.dim} · ${row?.label || key}：${row?.obs_date || "观测期未标注"}（${macroFrequencyLabel(row?.frequency)}）`;
    })),
  };
}
