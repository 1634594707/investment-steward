import { useEffect, useMemo, useState } from "react";
import type {
  QuantCrossBoard,
  QuantCrossRow,
  QuantPanelMineSummary,
  QuantPanelReportCard,
  QuantPanelSnapshot,
  QuantPanelSummary,
} from "../hooks/useQuant";
import { createCoreClient } from "../state/coreClient";
import { useQuant } from "../hooks/useQuant";

import "./quant-cross.css";

/**
 * QL10 / QL11 / QL12 横截面研究区块（《量化研究实验室质量提升任务路线图》2026-09-22，P2 数据面质变）。
 *
 * 与单标的时序通道并列：本地面板（QL10）→ 横截面挖掘（QL11）→ 报告卡闭环（QL12）。
 * 诚实口径贯穿 UI：进度逐标的、失败逐条列、缺口比例显式标注、不显著就写不显著，
 * 「未挖到」与「样本不足」分别给出原因，绝不留空白让人自行揣测。
 */

const PANEL_WINDOW_TIERS = [250, 750, 1250] as const;
const PANEL_BOARDS = [
  ["turnover", "成交额"],
  ["turnover_rate", "换手率"],
  ["gainers", "涨幅"],
  ["losers", "跌幅"],
] as const;

type BoardKey = (typeof PANEL_BOARDS)[number][0];

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function money(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return "—";
  return Math.round(value).toLocaleString();
}

export function QuantCrossSection({ active = true }: { active?: boolean } = {}) {
  const client = useMemo(() => createCoreClient(), []);
  const {
    quantPanelProgress, quantPanelSnapshots, quantPanelJobBusy,
    fetchQuantPanelSnapshots, loadQuantPanel, buildQuantPanel, mineQuantPanel, stopQuantPanelJob,
  } = useQuant(client);

  const [universeSize, setUniverseSize] = useState(30);
  const [board, setBoard] = useState<BoardKey>("turnover");
  const [windowBars, setWindowBars] = useState<number>(250);
  const [refresh, setRefresh] = useState(false);
  const [panel, setPanel] = useState<QuantPanelSummary | null>(null);
  const [selected, setSelected] = useState<QuantPanelSnapshot | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);

  const [forwardDays, setForwardDays] = useState(1);
  const [topN, setTopN] = useState(3);
  const [quantiles, setQuantiles] = useState(5);
  const [folds, setFolds] = useState(3);
  const [neutralise, setNeutralise] = useState<"none" | "demean" | "rank">("none");
  const [industryGroups, setIndustryGroups] = useState(false);
  const [shuffleSeed, setShuffleSeed] = useState("");
  const [mined, setMined] = useState<QuantPanelMineSummary | null>(null);
  const [mineError, setMineError] = useState<string | null>(null);

  useEffect(() => {
    // T6（用户视角路线图 2026-09-26）：KeepAlive 页面切走再切回时本组件仍在，
    // 依赖数组只有 [] → 面板快照索引永不刷新，服务端跑完的构建结果看不到。
    // 与 useTactics / useYouzi / QuantPage 同一口径：进入页面即拉一次。
    if (!active) return;
    void fetchQuantPanelSnapshots();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  async function onBuild() {
    setPanelError(null);
    const result = await buildQuantPanel({ universeSize, board, window: windowBars, refresh });
    if (result.ok && result.summary) setPanel(result.summary);
    else if (!result.cancelled) setPanelError(result.detail ?? "面板构建失败。");
  }

  async function onSelectSnapshot(snapshot: QuantPanelSnapshot) {
    setSelected(snapshot);
    setPanelError(null);
    const loaded = await loadQuantPanel(snapshot.content_hash, snapshot.file_name);
    if (loaded) setPanel(loaded);
    else setPanelError("快照哈希复核未通过（文件被改动或损坏），未使用该面板。");
  }

  async function onMine() {
    if (!selected) return;
    setMineError(null);
    const result = await mineQuantPanel(selected.content_hash, selected.file_name, {
      forwardDays, topN, quantiles, folds, neutralise, industryGroups,
      labelShuffleSeed: shuffleSeed.trim() === "" ? undefined : Number(shuffleSeed),
    });
    if (result.ok && result.summary) setMined(result.summary);
    else if (!result.cancelled) setMineError(result.detail ?? "横截面挖掘失败。");
  }

  const boardInfo: QuantCrossBoard | null = mined?.board ?? null;

  return (
    <section className="cross-section" aria-label="横截面研究">
      <header className="cross-head">
        <div>
          <span className="kicker">P2 · 横截面口径</span>
          <h3>横截面研究（面板 → 挖掘 → 报告卡）</h3>
        </div>
        {quantPanelJobBusy && (
          <div className="cross-progress" role="status">
            <span>进行中 {quantPanelProgress ? `${quantPanelProgress.done}/${quantPanelProgress.total}` : ""}</span>
            <button className="ghost-btn" onClick={() => void stopQuantPanelJob()}>停止</button>
          </div>
        )}
      </header>
      <p className="quant-risk">
        单标的时序 IC 无法回答「同一时点买谁」；横截面口径把因子还原为标准形态：逐日 rank IC、分组中性化、分位多空。
        成本按多空两腿的名单进出换手<b>双边</b>计费，建仓日按满仓换手（不假装免费入场）。
      </p>

      <div className="cross-grid">
        <div className="cross-card">
          <h4>① 本地面板（QL10）</h4>
          <div className="cross-fields">
            <label>Universe 规模（成交额前 N）
              <input type="number" min={5} max={200} value={universeSize}
                onChange={(event) => setUniverseSize(Number(event.target.value))} />
            </label>
            <label>榜单口径
              <select value={board} onChange={(event) => setBoard(event.target.value as BoardKey)}>
                {PANEL_BOARDS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
            </label>
            <label>历史窗口（根）
              <select value={windowBars} onChange={(event) => setWindowBars(Number(event.target.value))}>
                {PANEL_WINDOW_TIERS.map((tier) => <option key={tier} value={tier}>{tier}</option>)}
              </select>
            </label>
            <label className="cross-check">
              <input type="checkbox" checked={refresh} onChange={(event) => setRefresh(event.target.checked)} />
              忽略当日缓存强制重取
            </label>
          </div>
          <button className="primary-btn" disabled={quantPanelJobBusy} onClick={() => void onBuild()}>
            {quantPanelJobBusy ? "构建中…" : "构建面板"}
          </button>
          {panelError && <div className="ps-alert" role="alert">{panelError}</div>}

          {panel && (
            <div className="cross-summary">
              <div className="ps-metrics">
                <span>标的 <b>{panel.symbol_count}</b></span>
                <span>交易日 <b>{panel.date_count}</b></span>
                <span>格子 <b>{panel.cells}</b></span>
                <span>覆盖度 <b>{pct(panel.coverage)}</b></span>
                <span>对齐 <b>{panel.alignment}</b></span>
              </div>
              <div className="ps-meta">
                {panel.first_date && <span>{panel.first_date} → {panel.last_date}</span>}
                {panel.alignment_loss && (
                  <span title="交集口径只保留全体标的都有的交易日；被裁掉的天数在此显式报出">
                    并集 {panel.alignment_loss.union_dates} 天 → 交集 {panel.alignment_loss.common_dates} 天（裁掉 {panel.alignment_loss.dropped_by_intersection}）
                  </span>
                )}
                {panel.snapshot && <span className="mono">快照 {panel.snapshot.snapshot_hash.slice(0, 16)}…</span>}
                {panel.universe && (
                  <span>{panel.universe.rule}（{panel.universe.source} @ {panel.universe.observed_at.slice(0, 19)}）</span>
                )}
                {panel.caliber?.use_cache === false && <span>本次忽略缓存</span>}
              </div>
              {panel.degraded_reason && <p className="cross-warn">口径说明：{panel.degraded_reason}</p>}
              {(panel.excluded?.length ?? 0) > 0 && (
                <details className="cross-details">
                  <summary>
                    因历史长度不足剔除 {panel.excluded!.length} 只（门槛 {panel.history_floor} 根；不剔除它会把整个面板压成它的历史长度）
                  </summary>
                  <ul>
                    {panel.excluded!.map((item) => (
                      <li key={item.symbol} className="mono">{item.symbol}：{item.reason}</li>
                    ))}
                  </ul>
                </details>
              )}
              {panel.failures.length > 0 && (
                <details className="cross-details">
                  <summary>取数失败 {panel.failures.length} 只（不补数、不假装市场有这只票）</summary>
                  <ul>{panel.failures.map((item) => <li key={item.symbol} className="mono">{item.symbol}：{item.reason}</li>)}</ul>
                </details>
              )}
              <details className="cross-details">
                <summary>逐标的取数留痕（来源 / 行数 / 是否命中当日缓存）</summary>
                <ul>
                  {panel.steps.map((step) => (
                    <li key={step.symbol} className="mono">
                      {step.symbol} {step.ok
                        ? `${step.rows ?? 0} 行 · ${step.source ?? "—"}${step.from_cache ? " · 缓存" : ""}`
                        : `失败：${step.reason ?? "未知"}`}
                    </li>
                  ))}
                </ul>
              </details>
            </div>
          )}
        </div>

        <div className="cross-card">
          <h4>② 面板快照（内容寻址）</h4>
          <p className="cross-hint">从快照重建会复核哈希：内容被改动或损坏即拒绝使用，不做「大概一致」的降级。</p>
          {quantPanelSnapshots.length === 0 && <p className="cross-hint">还没有面板快照。先构建一次。</p>}
          <ul className="cross-snapshots">
            {quantPanelSnapshots.map((snapshot) => (
              <li key={snapshot.content_hash} className={selected?.content_hash === snapshot.content_hash ? "on" : ""}>
                <button className="text-button" onClick={() => void onSelectSnapshot(snapshot)}>
                  {snapshot.name || snapshot.file_name}
                </button>
                <div className="ps-meta">
                  <span>{snapshot.payload_meta.first_date} → {snapshot.payload_meta.last_date}</span>
                  <span>覆盖度 {pct(snapshot.payload_meta.coverage)}</span>
                  <span>{snapshot.payload_meta.universe_rule}</span>
                  <span className="mono">{snapshot.content_hash.slice(0, 16)}…</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="cross-card">
        <h4>③ 横截面挖掘 → 报告卡（QL11 + QL12）</h4>
        <div className="cross-fields">
          <label>前瞻天数<input type="number" min={1} max={20} value={forwardDays}
            onChange={(event) => setForwardDays(Number(event.target.value))} /></label>
          <label>Top N<input type="number" min={1} max={10} value={topN}
            onChange={(event) => setTopN(Number(event.target.value))} /></label>
          <label>分位组数<input type="number" min={2} max={10} value={quantiles}
            onChange={(event) => setQuantiles(Number(event.target.value))} /></label>
          <label>Fold 数<input type="number" min={1} max={10} value={folds}
            onChange={(event) => setFolds(Number(event.target.value))} /></label>
          <label>中性化口径
            <select value={neutralise} onChange={(event) => setNeutralise(event.target.value as typeof neutralise)}>
              <option value="none">不做（原始口径）</option>
              <option value="demean">组内去均值</option>
              <option value="rank">组内取秩</option>
            </select>
          </label>
          <label className="cross-check">
            <input type="checkbox" checked={industryGroups} onChange={(event) => setIndustryGroups(event.target.checked)} />
            反查行业分组（取不到就如实报缺口）
          </label>
          <label>过拟合自检 seed（可选）
            <input type="text" inputMode="numeric" placeholder="如 7 → 标签置换后应空榜" value={shuffleSeed}
              onChange={(event) => setShuffleSeed(event.target.value)} />
          </label>
        </div>
        <button className="primary-btn" disabled={!selected || quantPanelJobBusy} onClick={() => void onMine()}>
          {quantPanelJobBusy ? "挖掘中…" : "开始挖掘并生成报告卡"}
        </button>
        {!selected && <p className="cross-hint">先在 ② 里选一个面板快照。</p>}
        {mineError && <div className="ps-alert" role="alert">{mineError}</div>}

        {mined && (
          <div className="cross-result">
            <div className="ps-metrics">
              <span>候选评估 <b>{boardInfo?.multiple_testing?.candidates_evaluated ?? "—"}</b></span>
              <span>进入检验 <b>{boardInfo?.multiple_testing?.candidates_tested ?? "—"}</b></span>
              <span>BH 通过 <b>{boardInfo?.multiple_testing?.rejected_total ?? "—"}</b></span>
              <span>去重后入选 <b>{boardInfo?.top.length ?? 0}</b></span>
              <span>未达显著 <b>{boardInfo?.marginal.length ?? 0}</b></span>
            </div>
            <div className="ps-meta">
              {boardInfo?.split && (
                <span title="三段式切分：train 选模 / valid 调阈值 / test 只确认一次">
                  切分 train {boardInfo.split.planned.train} · valid {boardInfo.split.planned.valid} · test {boardInfo.split.planned.test}
                  {boardInfo.split.degraded ? "（已退化）" : ""}
                </span>
              )}
              <span>IC 定义 {boardInfo?.ic_definition?.kind ?? "—"} · 重叠抽样 {boardInfo?.ic_definition?.effective_sampling ?? "—"}</span>
              <span>搜索 {boardInfo?.search?.strategy ?? "—"}（选择段 {boardInfo?.search?.selection_segment ?? "—"}，候选 {boardInfo?.search?.candidates_evaluated ?? "—"}）</span>
              <span className="mono">快照 {mined.panel_snapshot.digest.slice(0, 16)}…</span>
            </div>

            {mined.industry_groups?.mode && (
              <p className={mined.industry_groups.usable ? "cross-hint" : "cross-warn"}>
                行业分组：{mined.industry_groups.usable
                  ? `已匹配 ${mined.industry_groups.assigned ?? 0} 只 / ${mined.industry_groups.sectors_scanned ?? 0} 个板块`
                  : `缺口 ${pct(mined.industry_groups.unassigned_ratio)} —— ${mined.industry_groups.degraded_reason ?? "未做行业中性化"}`}
              </p>
            )}
            {boardInfo?.neutralise && boardInfo.neutralise.mode !== "none" && (
              <p className="cross-hint">
                中性化 {boardInfo.neutralise.mode} · 分组 {boardInfo.neutralise.groups_used} 个 · 未分组比例 {pct(boardInfo.neutralise.unassigned_ratio)}
                {boardInfo.neutralise.comparison && boardInfo.neutralise.comparison.pairs > 0 && (
                  <> ｜ 中性化前后 test IC（并列展示）：{num(boardInfo.neutralise.comparison.neutralised_test_ic_mean)} vs {num(boardInfo.neutralise.comparison.raw_test_ic_mean)}
                    （差 {num(boardInfo.neutralise.comparison.delta_test_ic_mean)}）</>
                )}
              </p>
            )}
            {boardInfo?.label_shuffle && (
              <p className="cross-hint">
                过拟合自检：标签已按标的维度置换 seed={boardInfo.label_shuffle.seed}（{boardInfo.label_shuffle.values_permuted} 个值）——榜单应为空。
              </p>
            )}
            {boardInfo?.degraded_reason && <p className="cross-warn">{boardInfo.degraded_reason}</p>}

            <CrossBoardTable rows={boardInfo?.top ?? []} title="入选榜单（校正后显著 + 相关性去重）" />
            <CrossBoardTable rows={boardInfo?.marginal ?? []} title="未达统计显著（显式列出，不可当作有效因子）" muted />

            {mined.reports.length === 0 && (
              <p className="cross-hint">没有入选公式，因此没有报告卡——空榜是允许的结果，不是错误。</p>
            )}
            {mined.reports.map((report) => <ReportCard key={report.report_hash ?? report.formula} report={report} />)}
          </div>
        )}
      </div>
    </section>
  );
}

function CrossBoardTable({ rows, title, muted = false }: { rows: QuantCrossRow[]; title: string; muted?: boolean }) {
  if (rows.length === 0) return null;
  return (
    <div className={muted ? "cross-table-wrap muted" : "cross-table-wrap"}>
      <h5>{title}</h5>
      <table className="cross-table">
        <thead>
          <tr>
            <th>公式</th><th>复杂度</th><th>train IC</th><th>valid IC</th><th>test IC</th>
            <th>同号率</th><th>test p</th><th>复杂度收缩</th><th>校正后得分</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.formula}>
              <td className="mono">{row.formula}</td>
              <td>{row.complexity}</td>
              <td>{num(row.train_ic)}</td>
              <td>{num(row.valid_ic)}</td>
              <td>{num(row.test_ic)}</td>
              <td>{num(row.sign_consistency, 2)}</td>
              <td title={row.significance_note ?? "test 段横截面 IC 达 BH 校正后显著"}>
                {row.test_p_value === null ? "—" : row.test_p_value.toExponential(2)}
                {row.test_significant ? " ✓" : ""}
              </td>
              <td>{num(row.complexity_shrink, 3)}</td>
              <td>{num(row.rank_score_adjusted, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReportCard({ report }: { report: QuantPanelReportCard }) {
  const leg = report.long_short;
  return (
    <div className="ps-item cross-report">
      <div className="ps-head">
        <h4 className="mono">{report.formula}</h4>
        <span className="type-tag">{report.test_significant ? "test 显著" : "仅展示"}</span>
        {report.report_hash && (
          <span className="mono cross-hash" title="由「面板快照 + 公式 + 全部口径参数」派生，可复算">
            report {report.report_hash.slice(0, 16)}…
          </span>
        )}
      </div>
      {report.degraded_reason && <p className="cross-warn">{report.degraded_reason}</p>}
      <div className="ps-metrics">
        <span>截面 IC <b>{num(report.ic.mean)}</b></span>
        <span>ICIR <b>{num(report.ic.icir, 2)}</b></span>
        <span>IC&gt;0 占比 <b>{pct(report.ic.positive_ratio, 1)}</b></span>
        <span>有效交易日 <b>{report.ic.effective_samples}</b></span>
        <span>p 值 <b>{report.ic.p_value === null ? "—" : report.ic.p_value.toExponential(2)}</b></span>
      </div>
      {report.ic.degraded_reason && <p className="cross-hint">{report.ic.degraded_reason}</p>}

      {leg && (
        <>
          <div className="ps-metrics">
            <span>毛收益 <b>{pct(leg.gross_total)}</b></span>
            <span>成本后 <b>{pct(leg.net_total)}</b></span>
            <span>成本合计 <b>{pct(leg.cost_total)}</b></span>
            <span>年化 <b>{pct(leg.net_annualised)}</b></span>
            <span>Sharpe <b>{num(leg.net_sharpe, 2)}</b></span>
            <span>最大回撤 <b>{pct(leg.max_drawdown)}</b></span>
            <span>日均换手 <b>{num(leg.turnover_mean, 3)}</b></span>
          </div>
          <div className="ps-meta">
            <span>{leg.first_date} → {leg.last_date}（{leg.samples} 个调仓日）</span>
            <span title="按因子值升序切组，靠前 = 因子值最低">
              分位前瞻收益 {leg.quantile_mean_forward_returns.map((value) => pct(value, 2)).join(" / ")}
            </span>
          </div>
        </>
      )}

      {report.folds && report.folds.length > 0 && (
        <details className="cross-details">
          <summary>分段 fold 稳定性（每折独立算 IC 与成本后收益）</summary>
          <table className="cross-table">
            <thead><tr><th>折</th><th>区间</th><th>IC</th><th>成本后累计</th><th>样本</th></tr></thead>
            <tbody>
              {report.folds.map((fold) => (
                <tr key={fold.fold}>
                  <td>{fold.fold}</td>
                  <td className="mono">{fold.first_date} → {fold.last_date}</td>
                  <td>{num(fold.ic_mean)}</td>
                  <td>{pct(fold.net_total)}</td>
                  <td>{fold.ic_samples}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {report.capacity && (
        <p className="cross-hint" title={report.capacity.caveat ?? ""}>
          容量量级提示：universe 中位成交额（代理）约 {money(report.capacity.universe_median_adv_cny)} 元，
          按 {pct(report.capacity.participation)} 参与率估算单只约 {money(report.capacity.per_name_notional_cny)} 元。
          {report.capacity.caveat ? `（${report.capacity.caveat}）` : ""}
        </p>
      )}

      {report.caliber && (
        <details className="cross-details">
          <summary>口径（版本号 / 成本 / 年化 / 换手定义）</summary>
          <ul>
            {Object.entries(report.caliber).map(([key, value]) => (
              <li key={key} className="mono">{key} = {String(value)}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
