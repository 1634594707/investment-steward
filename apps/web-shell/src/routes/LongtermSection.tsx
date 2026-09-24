/**
 * 研究长期面板（路线图阶段 8 / 8.1-8.6 前端接入）。
 * 决策时间线 · 可检验判断 · 观察事项 · 组合风险 · 研究快照与模板。
 * 全部走 coreRequest 通道;无通道时整体降级为提示。
 */
import { useEffect, useState } from "react";
import { DatePicker } from "../components/DatePicker";
import type { CoreRequest } from "../state/coreClient";
import type { AppView } from "../shell/nav";
import { detailOf } from "../state/coreClient";
import type { DecisionEntry } from "@investment-steward/domain-contracts";
import { formatDate } from "../state/format";

interface CoreResponse<T> { status: number; data: T }

async function call<T>(coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>>, req: CoreRequest): Promise<T> {
  const { status, data } = await coreRequest(req);
  if (status >= 400) throw new Error(detailOf(data) ?? `Core 返回错误（HTTP ${status}）`);
  return data as T;
}

const KIND_LABEL: Record<string, string> = {
  research_question: "研究问题", research_run: "研究运行", evidence: "证据",
  thesis_version: "投资逻辑版本", plan: "计划", decision: "决定",
  outcome: "执行结果", retrospective: "复盘",
};
const SCOPE_LABEL: Record<string, string> = { at_the_time: "当时信息", afterwards: "事后信息", unknown: "缺口" };
const JUDGMENT_STATUS: Record<string, string> = {
  pending: "未到期", verified: "已验证", refuted: "被否定", insufficient_data: "数据不足",
};
const WATCH_STATUS: Record<string, string> = {
  active: "观察中", paused: "已暂停", triggered: "已触发", closed: "已结束",
};
const CYCLE_LABEL: Record<string, string> = { daily: "每日", weekly: "每周", monthly: "每月", manual: "手动" };
const GRADE_LABEL: Record<string, string> = {
  invalidation_triggered: "直接触发失效条件", needs_research: "需要研究", reference: "信息参考",
};

function nowDate(): string {
  return new Date().toISOString().slice(0, 10);
}

// ———————— 决策时间线 ————————

interface TimelineEvent {
  kind: string; ref_id: string | null; title: string; detail: string;
  at: string | null; knowledge_scope: string; link_available: boolean;
}
interface Timeline {
  decided_at: string | null;
  events: TimelineEvent[];
  gaps: Array<{ kind: string; ref_id: string | null; title: string }>;
}

function TimelinePanel({ coreRequest, decisions }: { coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>>; decisions: DecisionEntry[] }) {
  const [selected, setSelected] = useState("");
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load(decisionId: string) {
    setSelected(decisionId);
    setTimeline(null);
    setError(null);
    if (!decisionId) return;
    try {
      setTimeline(await call<Timeline>(coreRequest, { method: "GET", path: `/timeline/decision/${decisionId}` }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "时间线加载失败");
    }
  }

  return (
    <div className="lt-block">
      <h4>决策时间线 <small>当时信息 = 事件时间 ≤ 决定时间;缺口如实显示,不补写历史</small></h4>
      <div className="lt-row">
        <select value={selected} onChange={(event) => void load(event.target.value)} aria-label="选择决定">
          <option value="">选择一条决定…</option>
          {decisions.map((item) => (
            <option key={item.decision_id} value={item.decision_id}>
              {formatDate(item.made_at)} · {item.theme.slice(0, 30)}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {timeline && (
        <ol className="lt-timeline">
          {timeline.events.map((event, index) => (
            <li key={`${event.kind}-${event.ref_id ?? index}`} className={event.link_available ? "" : "lt-gap"}>
              <div className="lt-event-head">
                <b>{KIND_LABEL[event.kind] ?? event.kind}</b>
                <span className={`soft-tag ${event.knowledge_scope === "at_the_time" ? "" : event.knowledge_scope === "afterwards" ? "amber" : "gray"}`}>
                  {SCOPE_LABEL[event.knowledge_scope] ?? event.knowledge_scope}
                </span>
                {event.at && <time>{formatDate(event.at)}</time>}
              </div>
              <p>{event.title}{event.detail ? ` — ${event.detail}` : ""}</p>
            </li>
          ))}
        </ol>
      )}
      {selected && !timeline && !error && <p className="evidence-group-empty">时间线加载中…</p>}
    </div>
  );
}

// ———————— 可检验判断 ————————

interface Judgment {
  judgment_id: string; subject: string; direction: string; statement: string;
  due_at: string; status: string; created_at: string;
  verifications?: Array<{ verification_id: string; result: string; outcome: string; checked_at: string }>;
}

function JudgmentsPanel({ coreRequest }: { coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>> }) {
  const [items, setItems] = useState<Judgment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [subject, setSubject] = useState("");
  const [direction, setDirection] = useState("看多");
  const [statement, setStatement] = useState("");
  const [dueAt, setDueAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [verifyFor, setVerifyFor] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState("verified");
  const [verifyOutcome, setVerifyOutcome] = useState("");

  async function load() {
    try {
      setItems(await call<Judgment[]>(coreRequest, { method: "GET", path: "/judgments" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "判断加载失败");
    }
  }
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function create() {
    if (!subject.trim() || !statement.trim() || !dueAt || busy) return;
    setBusy(true); setError(null);
    try {
      await call(coreRequest, {
        method: "POST", path: "/judgments",
        body: { subject: subject.trim(), direction: direction.trim(), statement: statement.trim(), due_at: `${dueAt}T00:00:00` },
      });
      setOpen(false); setSubject(""); setStatement(""); setDueAt("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally { setBusy(false); }
  }

  async function verify(judgmentId: string) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      await call(coreRequest, {
        method: "POST", path: `/judgments/${judgmentId}/verify`,
        body: { result: verifyResult, outcome: verifyOutcome.trim() },
      });
      setVerifyFor(null); setVerifyOutcome("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "验证失败");
    } finally { setBusy(false); }
  }

  return (
    <div className="lt-block">
      <div className="lt-head">
        <h4>可检验判断 <small>原文冻结;到期验证只追加结果</small></h4>
        <button className="text-button" onClick={() => setOpen(!open)}>{open ? "收起" : "记录判断"}</button>
      </div>
      {open && (
        <div className="lt-form">
          <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="判断对象（如 510300）" aria-label="判断对象" />
          <input value={direction} onChange={(e) => setDirection(e.target.value)} placeholder="方向" aria-label="方向" />
          <DatePicker value={dueAt} onChange={setDueAt} ariaLabel="验证时间" />
          <textarea value={statement} onChange={(e) => setStatement(e.target.value)} placeholder="判断原文（保存后不可修改）" aria-label="判断原文" rows={2} />
          <button className="pbtn" disabled={busy} onClick={() => void create()}>{busy ? "保存中…" : "保存判断"}</button>
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      {items.length === 0 && <p className="evidence-group-empty">暂无可检验判断。</p>}
      {items.map((item) => (
        <div className="lt-item" key={item.judgment_id}>
          <div className="lt-event-head">
            <b>{item.subject} · {item.direction}</b>
            <span className={`soft-tag ${item.status === "verified" ? "" : item.status === "refuted" ? "danger" : "gray"}`}>{JUDGMENT_STATUS[item.status]}</span>
            <small>验证时间 {formatDate(item.due_at)}</small>
          </div>
          <p className="lt-statement">{item.statement}</p>
          {item.verifications && item.verifications.length > 0 && (
            <ul className="lt-verifications">
              {item.verifications.map((v) => (
                <li key={v.verification_id}>{JUDGMENT_STATUS[v.result] ?? v.result} · {v.outcome || "（无补充说明）"} · {formatDate(v.checked_at)}</li>
              ))}
            </ul>
          )}
          {verifyFor === item.judgment_id ? (
            <div className="lt-form">
              <select value={verifyResult} onChange={(e) => setVerifyResult(e.target.value)} aria-label="验证结果">
                <option value="verified">已验证</option>
                <option value="refuted">被否定</option>
                <option value="insufficient_data">数据不足</option>
              </select>
              <input value={verifyOutcome} onChange={(e) => setVerifyOutcome(e.target.value)} placeholder="实际结果（只追加,不改原文）" aria-label="实际结果" />
              <button className="pbtn" disabled={busy} onClick={() => void verify(item.judgment_id)}>提交验证</button>
              <button className="pbtn danger" onClick={() => setVerifyFor(null)}>取消</button>
            </div>
          ) : (
            <button className="pbtn" onClick={() => setVerifyFor(item.judgment_id)}>到期验证</button>
          )}
        </div>
      ))}
    </div>
  );
}

// ———————— 观察事项 ————————

interface WatchItem {
  watch_id: string; title: string; indicator: string; condition_text: string;
  check_cycle: string; status: string; created_at: string;
}
interface WatchCheckItem { check_id: string; observed: string; triggered: boolean; note: string; checked_at: string }

function WatchItemsPanel({ coreRequest }: { coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>> }) {
  const [items, setItems] = useState<WatchItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [indicator, setIndicator] = useState("");
  const [condition, setCondition] = useState("");
  const [cycle, setCycle] = useState("weekly");
  const [busy, setBusy] = useState(false);
  const [checkFor, setCheckFor] = useState<string | null>(null);
  const [observed, setObserved] = useState("");
  const [triggered, setTriggered] = useState(false);
  const [checksFor, setChecksFor] = useState<string | null>(null);
  const [checks, setChecks] = useState<WatchCheckItem[]>([]);

  async function load() {
    try {
      setItems(await call<WatchItem[]>(coreRequest, { method: "GET", path: "/watch-items" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "观察事项加载失败");
    }
  }
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function create() {
    if (!title.trim() || !indicator.trim() || !condition.trim() || busy) return;
    setBusy(true); setError(null);
    try {
      await call(coreRequest, {
        method: "POST", path: "/watch-items",
        body: { title: title.trim(), indicator: indicator.trim(), condition_text: condition.trim(), check_cycle: cycle },
      });
      setOpen(false); setTitle(""); setIndicator(""); setCondition("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally { setBusy(false); }
  }

  async function recordCheck(watchId: string) {
    if (!observed.trim() || busy) return;
    setBusy(true); setError(null);
    try {
      await call(coreRequest, {
        method: "POST", path: `/watch-items/${watchId}/checks`,
        body: { observed: observed.trim(), triggered, dedup_value: nowDate() },
      });
      setCheckFor(null); setObserved(""); setTriggered(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "检查失败");
    } finally { setBusy(false); }
  }

  async function transition(watchId: string, target: string) {
    setBusy(true); setError(null);
    try {
      await call(coreRequest, { method: "POST", path: `/watch-items/${watchId}/transition`, body: { target } });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "状态变更失败");
    } finally { setBusy(false); }
  }

  async function showChecks(watchId: string) {
    if (checksFor === watchId) { setChecksFor(null); return; }
    try {
      setChecks(await call<WatchCheckItem[]>(coreRequest, { method: "GET", path: `/watch-items/${watchId}/checks` }));
      setChecksFor(watchId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "检查记录加载失败");
    }
  }

  return (
    <div className="lt-block">
      <div className="lt-head">
        <h4>观察事项 <small>检查只追加;同日重复检查自动去重</small></h4>
        <button className="text-button" onClick={() => setOpen(!open)}>{open ? "收起" : "新建观察"}</button>
      </div>
      {open && (
        <div className="lt-form">
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="标题（如 跌破年线观察）" aria-label="标题" />
          <input value={indicator} onChange={(e) => setIndicator(e.target.value)} placeholder="观察指标口径" aria-label="观察指标" />
          <input value={condition} onChange={(e) => setCondition(e.target.value)} placeholder="触发/失效条件" aria-label="触发条件" />
          <select value={cycle} onChange={(e) => setCycle(e.target.value)} aria-label="检查周期">
            <option value="daily">每日</option><option value="weekly">每周</option>
            <option value="monthly">每月</option><option value="manual">手动</option>
          </select>
          <button className="pbtn" disabled={busy} onClick={() => void create()}>{busy ? "保存中…" : "创建"}</button>
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      {items.length === 0 && <p className="evidence-group-empty">暂无观察事项。</p>}
      {items.map((item) => (
        <div className="lt-item" key={item.watch_id}>
          <div className="lt-event-head">
            <b>{item.title}</b>
            <span className={`soft-tag ${item.status === "triggered" ? "amber" : item.status === "closed" ? "gray" : ""}`}>{WATCH_STATUS[item.status]}</span>
            <small>{CYCLE_LABEL[item.check_cycle] ?? item.check_cycle}</small>
          </div>
          <p>{item.indicator} — 触发条件:{item.condition_text}</p>
          <div className="lt-actions">
            {item.status !== "closed" && <button className="pbtn" onClick={() => { setCheckFor(checkFor === item.watch_id ? null : item.watch_id); setObserved(""); setTriggered(false); }}>记录检查</button>}
            <button className="pbtn" onClick={() => void showChecks(item.watch_id)}>{checksFor === item.watch_id ? "收起记录" : "检查记录"}</button>
            {item.status === "active" && <button className="pbtn danger" onClick={() => void transition(item.watch_id, "paused")}>暂停</button>}
            {item.status === "paused" && <button className="pbtn" onClick={() => void transition(item.watch_id, "active")}>恢复</button>}
            {item.status !== "closed" && <button className="pbtn danger" onClick={() => void transition(item.watch_id, "closed")}>结束</button>}
            {item.status === "closed" && <button className="pbtn" onClick={() => void transition(item.watch_id, "active")}>历史恢复</button>}
          </div>
          {checkFor === item.watch_id && (
            <div className="lt-form">
              <input value={observed} onChange={(e) => setObserved(e.target.value)} placeholder="本次观察结果" aria-label="观察结果" />
              <label className="lt-check"><input type="checkbox" checked={triggered} onChange={(e) => setTriggered(e.target.checked)} />触发条件</label>
              <button className="pbtn" disabled={busy} onClick={() => void recordCheck(item.watch_id)}>提交检查</button>
            </div>
          )}
          {checksFor === item.watch_id && (
            <ul className="lt-verifications">
              {checks.length === 0 && <li>暂无检查记录。</li>}
              {checks.map((check) => (
                <li key={check.check_id}>
                  {check.triggered ? "【触发】" : ""}{check.observed} · {formatDate(check.checked_at)}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}

// ———————— 组合风险 ————————

interface PortfolioRisk {
  instruments: Array<{ instrument: string; thesis_count: number; invalidation_conditions: string[] }>;
  concentration_by_logic_count: Array<{ instrument: string; logic_share: number }>;
  shared_invalidation_conditions: Array<{ condition: string; instruments: string[] }>;
  gaps: Array<{ dimension: string; reason: string }>;
}

function RiskPanel({ coreRequest, onNavigate }: {
  coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>>;
  onNavigate?: (view: AppView) => void;
}) {
  const [risk, setRisk] = useState<PortfolioRisk | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void (async () => {
      try { setRisk(await call<PortfolioRisk>(coreRequest, { method: "GET", path: "/portfolio/risk" })); }
      catch (err) { setError(err instanceof Error ? err.message : "风险视图加载失败"); }
    })();
  }, [coreRequest]);
  if (error) return <div className="lt-block"><h4>组合风险</h4><p className="form-error" role="alert">{error}</p></div>;
  if (!risk) return <div className="lt-block"><h4>组合风险</h4><p className="evidence-group-empty">加载中…</p></div>;
  return (
    <div className="lt-block">
      <h4>组合风险 <small>集中度=逻辑数量口径(非市值);算不出的显式缺口</small></h4>
      {risk.shared_invalidation_conditions.length > 0 && (
        <div className="lt-item">
          <b>共同失效条件（统计相关,非因果）</b>
          <ul className="lt-verifications">
            {risk.shared_invalidation_conditions.map((item) => (
              <li key={item.condition}>
                {item.condition} — {item.instruments.join("、")}
                {onNavigate && <button className="lt-jump" onClick={() => onNavigate("workbench")}>发起研究</button>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ul className="lt-verifications">
        {risk.concentration_by_logic_count.map((item) => (
          <li key={item.instrument}>
            {item.instrument} · 逻辑占比 {(item.logic_share * 100).toFixed(1)}%
            {onNavigate && (
              <span className="lt-jumps">
                <button className="lt-jump" onClick={() => onNavigate("investment")}>投资逻辑</button>
                <button className="lt-jump" onClick={() => onNavigate("quant")}>量化实验</button>
              </span>
            )}
          </li>
        ))}
      </ul>
      <ul className="lt-verifications">
        {risk.gaps.map((gap) => <li key={gap.dimension} className="lt-gap-text">{gap.reason}</li>)}
      </ul>
    </div>
  );
}

// ———————— 研究快照与模板 ————————

interface SnapshotItem { snapshot_id: string; title: string; question: string; created_at: string }
interface TemplateInfo { template_id: string; name: string; description: string; required_context: string[]; user_id: string | null }

function SnapshotsPanel({ coreRequest }: { coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>> }) {
  const [snapshots, setSnapshots] = useState<SnapshotItem[]>([]);
  const [templates, setTemplates] = useState<{ builtin: TemplateInfo[]; user: TemplateInfo[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [list, tpl] = await Promise.all([
        call<SnapshotItem[]>(coreRequest, { method: "GET", path: "/research/snapshots" }),
        call<{ builtin: TemplateInfo[]; user: TemplateInfo[] }>(coreRequest, { method: "GET", path: "/research/templates" }),
      ]);
      setSnapshots(list); setTemplates(tpl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "快照加载失败");
    }
  }
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function createSnapshot() {
    if (!title.trim() || busy) return;
    setBusy(true); setError(null);
    try {
      await call(coreRequest, { method: "POST", path: "/research/snapshots", body: { title: title.trim(), question: question.trim() } });
      setTitle(""); setQuestion("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "快照保存失败");
    } finally { setBusy(false); }
  }

  return (
    <div className="lt-block">
      <h4>研究快照与模板 <small>快照恢复 = 只读回看,不替换当前数据</small></h4>
      <div className="lt-form">
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="快照标题" aria-label="快照标题" />
        <input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="当时的研究问题（可选）" aria-label="研究问题" />
        <button className="pbtn" disabled={busy} onClick={() => void createSnapshot()}>{busy ? "保存中…" : "冻结当前上下文"}</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {snapshots.length > 0 && (
        <ul className="lt-verifications">
          {snapshots.map((item) => <li key={item.snapshot_id}>{item.title} · {formatDate(item.created_at)}</li>)}
        </ul>
      )}
      {templates && (
        <div className="lt-item">
          <b>研究模板（内置 {templates.builtin.length} + 自定义 {templates.user.length}）</b>
          <ul className="lt-verifications">
            {[...templates.builtin, ...templates.user].map((t) => (
              <li key={t.template_id}>{t.name} — 必填:{t.required_context.join("、") || "无"}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ———————— 主入口 ————————

export function LongtermSection({ decisions, coreRequest, onNavigate }: {
  decisions: DecisionEntry[];
  coreRequest?: (req: CoreRequest) => Promise<CoreResponse<unknown>>;
  /** 跳转链路:风险项 → 相关标的(量化实验)/投资逻辑(投资管家)/研究问题(研究工作台)。 */
  onNavigate?: (view: AppView) => void;
}) {
  if (!coreRequest) {
    return (
      <section className="review-longterm">
        <div className="section-heading"><div><span className="eyebrow">LONG-TERM RESEARCH</span><h3>决策与研究长期能力</h3></div></div>
        <p className="evidence-group-empty">当前环境没有 Core 请求通道,此面板不可用。</p>
      </section>
    );
  }
  return (
    <section className="review-longterm">
      <div className="section-heading"><div><span className="eyebrow">LONG-TERM RESEARCH</span><h3>决策与研究长期能力</h3></div></div>
      <TimelinePanel coreRequest={coreRequest} decisions={decisions} />
      <JudgmentsPanel coreRequest={coreRequest} />
      <WatchItemsPanel coreRequest={coreRequest} />
      <RiskPanel coreRequest={coreRequest} onNavigate={onNavigate} />
      <SnapshotsPanel coreRequest={coreRequest} />
    </section>
  );
}
