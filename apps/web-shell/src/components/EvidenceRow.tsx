import type { Evidence } from "@investment-steward/domain-contracts";
import { EvidenceRef } from "./EvidenceRef";
import { formatDate } from "../state/format";

interface Props {
  item: Evidence;
  expanded?: boolean;
  onOpen?: (evidence_id: string) => void;
}

interface KeyValue {
  key: string;
  value: string;
}

/**
 * 后端把财务细目拼成一条字符串下发（core-api app.py:6308-6328）：
 *   `{statement_type}·{period_end}：{k=v；k=v；…}`，lead 用全角 `·`/`：`，细目用全角 `；`。
 * 整段塞进一个标题里会糊成 4 行。这里只在结构无歧义时拆开：lead 段不含 `=`，
 * 且其后的段至少 2 对、每对都是 `key=value`；键名保持源字段原样，不翻译、不改值，只做千分位分组。
 */
export function parseKeyValueSummary(summary: string): { lead: string; pairs: KeyValue[] } | null {
  let lead = "";
  let body = summary;
  const colon = summary.indexOf("：");
  if (colon > 0 && !summary.slice(0, colon).includes("=")) {
    lead = summary.slice(0, colon).trim();
    body = summary.slice(colon + 1);
  }
  const parts = body.split(/[；;]/).map((s) => s.trim()).filter(Boolean);
  if (parts.length < 2) return null;
  const pairs: KeyValue[] = [];
  for (const part of parts) {
    const m = /^(?<k>[A-Za-z_][A-Za-z0-9_.-]*)=(?<v>.+)$/.exec(part);
    const key = m?.groups?.k;
    const value = m?.groups?.v;
    if (key === undefined || value === undefined) return null;
    pairs.push({ key, value: value.trim() });
  }
  return pairs.length >= 2 ? { lead, pairs } : null;
}

/** 千分位分组：只对纯整数/纯小数生效，其它值原样透出（不四舍五入、不换单位）。 */
function formatValue(value: string): string {
  const m = /^(?<sign>-?)(?<int>\d+)(?<dec>\.\d+)?$/.exec(value);
  const { sign, int, dec } = m?.groups ?? {};
  if (!m || int === undefined) return value;
  return `${sign ?? ""}${int.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${dec ?? ""}`;
}

export function EvidenceRow({ item, expanded = false, onOpen }: Props) {
  const kv = parseKeyValueSummary(item.summary);
  const open = onOpen ? () => onOpen(item.evidence_id) : undefined;
  return (
    <article className={`evidence-row ${expanded ? "expanded" : ""} ${kv ? "is-kv" : ""}`}>
      <div className="evidence-meta">
        <span className={`evidence-dot ${item.relation}`} />
        <span>{item.source_name}</span>
        <time>{formatDate(item.observed_at ?? item.collected_at)}</time>
      </div>
      {kv ? (
        <>
          <EvidenceRef title={kv.lead || item.source_name} onOpen={open} />
          <dl className="evidence-kv">
            {kv.pairs.map((pair) => (
              <div className="evidence-kv-row" key={pair.key}>
                <dt>{pair.key}</dt>
                <dd>{formatValue(pair.value)}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : (
        <EvidenceRef title={item.summary} onOpen={open} />
      )}
      <div className="evidence-foot">
        <span className={`source-status ${item.status === "active" ? "ok" : ""}`}>{item.status === "active" ? "当前有效" : item.status}</span>
        <span className="hash">{item.content_hash.slice(0, 8)}</span>
      </div>
      {expanded && item.limitations.length > 0 && <p className="evidence-limit">{item.limitations[0]}</p>}
    </article>
  );
}
