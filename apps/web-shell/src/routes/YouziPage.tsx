import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { createCoreClient, type CoreClient } from "../state/coreClient";
import { DatePicker } from "../components/DatePicker";
import {
  COVERAGE_LABELS,
  EXCLUDE_LABELS,
  FACT_LABELS,
  HORIZON_STATUS_LABELS,
  OMITTED_LABELS,
  REPLAY_REASON_LABELS,
  factsEndingOn,
  useYouzi,
  type YouziReplayEvent,
  type YouziReplayPage,
  type YouziReplayQuery,
  type YouziSeatRow,
  type YouziTab,
  type YouziTactics,
  type YouziTacticsFact,
} from "../hooks/useYouzi";
import "./youzi.css";

/**
 * P3-D03（席位证据与游资学习插件路线图 §7）：游资雷达页面。
 *
 * 八个页签（U07 与实际对齐）：学习（推荐首站）/ 今日席位 / 跨日事实 / 案例复盘 / 练习 /
 * 席位档案 / 我的持仓 / 指数席位（D05 对照页）。
 *
 * 三条贯穿全页的展示原则（来自方案 §三 铁律）：
 *   1. **取不到就说取不到**。`available=false` 渲染「未取得 + 原因」，
 *      绝不用空列表冒充「今天没上榜」——两者含义完全不同。
 *   2. **桶不是游资**。机构专用 / 沪深股通 / 券商总部标【匿名汇总桶】，
 *      与观察名单命中在视觉上区分开。
 *   3. **不出现胜率/排行**。数据商夹带的「成功率」只作原文标注。
 */

interface Props {
  isDemo: boolean;
  /** official.youzi-radar 是否已启用：未启用时 /youzi/* 全部 409。 */
  pluginEnabled: boolean;
  /** 一键启用插件；启用后回到本页即可取数。 */
  onEnablePlugin: () => void;
  /** 当前视图是否为游资雷达页（KeepAlive 常驻挂载，据此决定是否取数）。 */
  active: boolean;
  /** U07（桌面端升级路线图 2026-09-18）：个股行跳研究工作台（AppShell 注入 openStockReport）。 */
  onOpenReport?: (symbol: string) => void;
}

const TABS: Array<{ id: YouziTab; label: string; hint: string }> = [
  { id: "today", label: "今日席位", hint: "当日上榜与买卖席位" },
  { id: "tactics", label: "跨日事实", hint: "五交易日窗口内的已披露路径事实" },
  { id: "replay", label: "案例复盘", hint: "按证券代码与日期区间查看披露事件时间线" },
  { id: "practice", label: "练习", hint: "用真实披露事实做识别与复盘练习" },
  { id: "profile", label: "席位档案", hint: "某营业部近 N 日记录" },
  { id: "cross", label: "我的持仓", hint: "持仓/自选与今日上榜的交叉提示" },
  { id: "cffex", label: "指数席位", hint: "中金所会员排名对照" },
  { id: "learn", label: "学习", hint: "十一课：学会读游资 + 术语表" },
];

/** 金额渲染：亿元/万元分档，保留两位；null 写「未披露」（不写 0）。 */
function money(value: number | null | undefined): string {
  if (value == null) return "未披露";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(2)} 万`;
  return `${sign}${abs.toFixed(0)} 元`;
}

function pct(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** 交易日 `20260916` → `2026-09-16`；格式不符则原样返回（不猜）。 */
function dayLabel(day: string | undefined): string {
  if (!day || day.length !== 8) return day || "—";
  return `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}`;
}

/** 来源报告名 → 人读简称；未登记的报告名原样显示（不猜是哪个榜）。 */
function shortReport(report: string): string {
  const known: Record<string, string> = {
    RPT_BILLBOARD_DAILYDETAILSBUY: "买榜明细",
    RPT_BILLBOARD_DAILYDETAILSSELL: "卖榜明细",
    RPT_DAILYBILLBOARD_DETAILS: "当日榜单",
  };
  return known[report] || report || "未标注";
}

function SeatTable({
  title,
  rows,
  onProfile,
}: {
  title: string;
  rows: YouziSeatRow[];
  /** U02（桌面端升级路线图 2026-09-18）：行内「查档案」——任意席位的档案都可查（端点本就支持精确代码）。 */
  onProfile?: (operatedeptCode: string) => void;
}) {
  if (rows.length === 0) {
    return (
      <div className="youzi-seat-block">
        <h4>{title}</h4>
        <p className="youzi-muted">本侧席位未披露。</p>
      </div>
    );
  }
  return (
    <div className="youzi-seat-block">
      <h4>{title}</h4>
      <table className="youzi-table">
        <thead>
          <tr>
            <th>营业部 / 通道</th>
            <th className="num">买入</th>
            <th className="num">卖出</th>
            <th className="num">买卖轧差净额</th>
            <th>标记</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.operatedept_code}-${title}-${row.operatedept_name}`}>
              <td>{row.operatedept_name}</td>
              <td className="num">{money(row.buy)}</td>
              <td className="num">{money(row.sell)}</td>
              <td className={`num ${row.net != null && row.net < 0 ? "neg" : ""}`}>{money(row.net)}</td>
              <td>
                {row.watchlist_hit && <span className="youzi-badge hit">观察名单命中</span>}
                {row.is_bucket && <span className="youzi-badge bucket">匿名汇总桶</span>}
                {/* G03：席位行的原始披露页（构造式 URL：交易日 + 代码）。 */}
                {row.source_url ? (
                  <a
                    className="youzi-src-link"
                    href={row.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    title="东财龙虎榜个股页（由交易日 + 证券代码拼出，上游不返回 URL）"
                  >
                    原始页 ↗
                  </a>
                ) : null}
                {onProfile && !row.is_bucket && (
                  <button
                    className="ghost-btn small"
                    title="查看该席位的档案（近 N 日上榜记录）"
                    onClick={() => onProfile(row.operatedept_code)}
                  >
                    查档案
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function YouziPage({ isDemo, pluginEnabled, onEnablePlugin, active, onOpenReport }: Props) {
  const client = useMemo(() => createCoreClient(), []);
  const {
    tab, setTab, today, todayDay, setTodayDay, seats, profile, watchlist, cffex, cross, tactics, loading, error,
    selectedCode, loadToday, loadSeats, closeSeats, loadProfile, loadCffex, loadCrossCheck, loadTactics,
    replay, replayEvents, replayQuery, loadReplay, resetReplay,
  } = useYouzi(client, active && pluginEnabled);

  // —— U01/U02/U04（桌面端升级路线图 2026-09-18）：今日席位页签的展开/筛选/排序与档案直查 ——
  /** 行内展开的席位明细所属股票（U01：明细嵌进被点的那一行，不再甩到表尾）。 */
  const [expandedCode, setExpandedCode] = useState<string | null>(null);
  /** U04：只看观察名单命中。 */
  const [todayFilter, setTodayFilter] = useState<"all" | "hits">("all");
  /** U04：表头排序键（default=观察命中优先 + 榜单原序）。★ 只是已披露字段的显示顺序，
   * 不得引入胜率/涨跌概率/排行键语义（文件头铁律 3、课文 L5/L9）。 */
  const [todaySort, setTodaySort] = useState<"default" | "net" | "change" | "buy">("default");
  /** U02：档案页签的精确代码输入框（数据源不支持模糊查询）。 */
  const [profileCodeInput, setProfileCodeInput] = useState("");
  /** U04：日期选择器的中间态（YYYY-MM-DD；应用时转 YYYYMMDD）。 */
  const [todayDayInput, setTodayDayInput] = useState("");
  /** U07：学习页签访问过之后，「学习」不再标「推荐首站」。 */
  const [learnVisited, setLearnVisited] = useState(false);

  /** U06（桌面端升级路线图 2026-09-18）：练习答案落产品自己的学习存储（POST /learning/activities）。
   * unit_id 由前端生成（端点要求 UUID）；objective/user_answer 按服务端长度上限截断。 */
  const saveLearningActivity = useCallback(
    async (input: SaveActivityInput): Promise<boolean> => {
      try {
        const response = await client.request<{ ok: boolean }>({
          method: "POST",
          path: "/learning/activities",
          body: {
            unit_id: crypto.randomUUID(),
            unit_type: input.unitType,
            objective: input.objective.slice(0, 2000),
            user_answer: input.userAnswer.slice(0, 10000),
            bound_instrument: input.boundInstrument ?? null,
          },
        });
        return response.status < 400;
      } catch {
        return false;
      }
    },
    [client],
  );

  const todayRows = useMemo(() => {
    if (!today?.available) return [];
    const rows = [...today.rows];
    const hitsOf = (row: (typeof rows)[number]) => row.watchlist_hits.length;
    const num = (value: number | null | undefined) => (typeof value === "number" && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY);
    if (todayFilter === "hits") return rows.filter((row) => hitsOf(row) > 0);
    if (todaySort === "default") {
      // 默认：观察命中优先（组内保持榜单原序，JS sort 稳定）。
      return rows.sort((a, b) => hitsOf(b) - hitsOf(a));
    }
    const key = todaySort === "net" ? "billboard_net_amt" : todaySort === "change" ? "change_rate" : "billboard_buy_amt";
    return rows.sort((a, b) => num(b[key as keyof (typeof rows)[number]] as number | null) - num(a[key as keyof (typeof rows)[number]] as number | null));
  }, [today, todayFilter, todaySort]);

  /** U01：点「看席位」= 在该行内展开/收起；点另一行切换（不清旧明细数据，只换展开位置）。 */
  function toggleSeats(code: string): void {
    if (expandedCode === code) {
      setExpandedCode(null);
      closeSeats();
      return;
    }
    setExpandedCode(code);
    void loadSeats(code);
  }

  /** U02：从席位行直达档案页签（两步以内：今日席位 → 查档案）。 */
  function openSeatProfile(code: string): void {
    setTab("profile");
    void loadProfile(code);
  }

  if (!pluginEnabled) {
    return (
      <section className="youzi-page">
        <header className="youzi-header">
          <div><span className="youzi-tag">席位证据 · 游资雷达</span><h2>游资雷达</h2></div>
          <span>读席位、学游资；不构成买卖建议</span>
        </header>
        <div className="chart-pending">
          <strong>游资雷达未启用</strong>
          <span>启用 official.youzi-radar 后开始查看中金所席位与龙虎榜。</span>
          <p className="youzi-muted">
            启用后首次取数会访问公开席位数据；查询内容为交易日与证券代码，<strong>不包含你的持仓</strong>。
          </p>
          <button className="primary-button mt-10" onClick={onEnablePlugin}>启用游资雷达 <span>→</span></button>
        </div>
      </section>
    );
  }

  return (
    <section className="youzi-page">
      <header className="youzi-header">
        <div><span className="youzi-tag">席位证据 · 游资雷达</span><h2>游资雷达</h2></div>
        <span>
          {today?.trading_day ? `交易日 ${dayLabel(today.trading_day)}` : "交易日待取数"}
          {isDemo ? " · 演示模式" : ""}
        </span>
      </header>

      <nav className="youzi-tabs">
        {TABS.map((item) => (
          <button
            key={item.id}
            className={tab === item.id ? "youzi-tab active" : "youzi-tab"}
            onClick={() => {
              if (item.id === "learn") setLearnVisited(true);
              setTab(item.id);
            }}
            title={item.id === "learn" && !learnVisited ? `推荐首站：先学怎么读，再看榜。${item.hint}` : item.hint}
          >
            {item.label}
            {item.id === "learn" && !learnVisited && <span className="youzi-badge hit">推荐首站</span>}
          </button>
        ))}
      </nav>

      {error && <div className="youzi-alert">{error}</div>}

      {/* —— 今日席位 —— */}
      {tab === "today" && (
        <>
          {/* U04：交易日选择——后端按可信交易日历校验，非交易日会以 available=false + 原因返回，
              前端不把自然日当交易日。 */}
          <div className="youzi-chips">
            <label className="youzi-muted">
              交易日{" "}
              <DatePicker value={todayDayInput} onChange={setTodayDayInput} ariaLabel="选择交易日" />
            </label>
            <button
              className="ghost-btn small"
              disabled={!todayDayInput}
              onClick={() => {
                const day = todayDayInput.replaceAll("-", "");
                setTodayDay(day);
                void loadToday(day);
              }}
            >
              查看该日榜单
            </button>
            <button
              className="ghost-btn small"
              disabled={!todayDay}
              onClick={() => {
                setTodayDayInput("");
                setTodayDay(null);
                void loadToday(null);
              }}
            >
              回到最新
            </button>
            <button
              className={todayFilter === "hits" ? "ghost-btn small active" : "ghost-btn small"}
              aria-pressed={todayFilter === "hits"}
              onClick={() => setTodayFilter(todayFilter === "hits" ? "all" : "hits")}
            >
              只看观察名单命中
            </button>
          </div>
          {today && !today.available && (
            <div className="youzi-alert">
              <strong>未取得席位数据</strong>
              <span>{today.reason || "数据源未返回数据。"}</span>
              <p className="youzi-muted">
                这里是「没拿到」，不是「这一天没有股票上榜」——两者含义不同，故不显示空表。
              </p>
              <button className="ghost-btn" onClick={() => void loadToday()}>重新取数</button>
            </div>
          )}
          {today?.available && (
            <>
              <p className="youzi-note">
                交易日 {dayLabel(today.trading_day)}：{today.rows.length} 只股票上榜
                {todayFilter === "hits" ? `（筛选后 ${todayRows.length} 只有观察席位参与）` : ""}；标记「观察名单命中」的行表示有观察席位参与。
                {watchlist ? ` 观察名单 v${watchlist.version}。` : ""}
                <br />
                <span className="youzi-muted">
                  排序/筛选只改变<strong>已披露字段</strong>的显示顺序，不构成任何胜率、概率或排行键；「收盘价」「市场」为榜单披露字段。
                </span>
              </p>
              <table className="youzi-table">
                <thead>
                  <tr>
                    <th title="市场（榜单披露字段）">市场</th>
                    <th>代码</th>
                    <th>名称</th>
                    <th>上榜原因</th>
                    <th
                      className="num"
                      role="button"
                      title="按涨跌幅降序（仅显示顺序）"
                      style={{ cursor: "pointer" }}
                      onClick={() => setTodaySort(todaySort === "change" ? "default" : "change")}
                    >
                      涨跌幅{todaySort === "change" ? " ▼" : ""}
                    </th>
                    <th className="num">收盘价</th>
                    <th
                      className="num"
                      role="button"
                      title="按买入额降序（仅显示顺序）"
                      style={{ cursor: "pointer" }}
                      onClick={() => setTodaySort(todaySort === "buy" ? "default" : "buy")}
                    >
                      买入{todaySort === "buy" ? " ▼" : ""}
                    </th>
                    <th className="num">卖出</th>
                    <th
                      className="num"
                      role="button"
                      title="按净额降序（仅显示顺序）"
                      style={{ cursor: "pointer" }}
                      onClick={() => setTodaySort(todaySort === "net" ? "default" : "net")}
                    >
                      净额{todaySort === "net" ? " ▼" : ""}
                    </th>
                    <th>席位</th>
                  </tr>
                </thead>
                <tbody>
                  {todayRows.map((row) => (
                    <Fragment key={row.security_code}>
                      <tr className={`${row.watchlist_hits.length > 0 ? "hl" : ""} ${expandedCode === row.security_code ? "sel" : ""}`}>
                        <td className="youzi-muted">{row.trade_market || "—"}</td>
                        <td className="mono">{row.security_code}</td>
                        <td>{row.security_name}</td>
                        <td className="youzi-reason">{row.explanation}</td>
                        <td className="num">{pct(row.change_rate)}</td>
                        <td className="num">{row.close_price != null ? row.close_price.toFixed(2) : "未披露"}</td>
                        <td className="num">{money(row.billboard_buy_amt)}</td>
                        <td className="num">{money(row.billboard_sell_amt)}</td>
                        <td className={`num ${row.billboard_net_amt != null && row.billboard_net_amt < 0 ? "neg" : ""}`}>
                          {money(row.billboard_net_amt)}
                        </td>
                        <td>
                          {/* U01：明细嵌进被点的那一行（同页案例复盘的 openEventId 行内展开范式），
                              不再甩到 58 行表尾——「点了没反应」的根因就是明细在视口之外。 */}
                          <button
                            className={expandedCode === row.security_code ? "ghost-btn small active" : "ghost-btn small"}
                            aria-expanded={expandedCode === row.security_code}
                            onClick={() => toggleSeats(row.security_code)}
                          >
                            {expandedCode === row.security_code ? "收起" : "看席位"}
                          </button>
                          {onOpenReport && (
                            <button
                              className="ghost-btn small"
                              title="带这只票去研究工作台看个股研报（U07）"
                              onClick={() => onOpenReport(row.security_code)}
                            >
                              去研报
                            </button>
                          )}
                          {row.watchlist_hits.length > 0 && (
                            <span className="youzi-badge hit" title={row.watchlist_hits.map((h) => h.name).join("、")}>
                              命中 {row.watchlist_hits.length}
                            </span>
                          )}
                          {/* G03：该上榜事实的原始披露页（构造式 URL：交易日 + 代码）。 */}
                          {row.source_url ? (
                            <a
                              className="youzi-src-link"
                              href={row.source_url}
                              target="_blank"
                              rel="noreferrer noopener"
                              title="东财龙虎榜个股页（由交易日 + 证券代码拼出，上游不返回 URL）"
                            >
                              原始页 ↗
                            </a>
                          ) : null}
                        </td>
                      </tr>
                      {expandedCode === row.security_code && (
                        <tr className="youzi-detail-row">
                          <td colSpan={10}>
                            {seats && seats.security_code === row.security_code ? (
                              <div className="youzi-detail">
                                <div className="youzi-detail-head">
                                  <h3>席位明细 · {seats.security_code}（{dayLabel(seats.trading_day)}）</h3>
                                  <button className="ghost-btn small" onClick={() => void loadSeats(seats.security_code ?? "")}>刷新</button>
                                </div>
                                {!seats.available && <div className="youzi-alert">{seats.reason || "席位明细未取得。"}</div>}
                                {seats.available && (
                                  <>
                                    <SeatTable title="买入前席位" rows={seats.buy} onProfile={openSeatProfile} />
                                    <SeatTable title="卖出前席位" rows={seats.sell} onProfile={openSeatProfile} />
                                    <p className="youzi-muted">
                                      口径：净额 = 该席位当日买入 − 卖出（买/卖两榜同义）；同一席位可能同时出现在买卖两侧。
                                      「匿名汇总桶」是交易所的匿名汇总通道（机构专用/沪深股通/券商总部），不是某家营业部，
                                      因此不计入游资观察命中。点「查档案」可看该席位近 N 日的上榜记录。
                                    </p>
                                  </>
                                )}
                              </div>
                            ) : (
                              <p className="youzi-muted">{loading ? "取数中…" : "席位明细加载中…"}</p>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {loading && <p className="youzi-muted">取数中…</p>}
        </>
      )}

      {/* —— 跨日事实（Y2-10）—— */}
      {tab === "tactics" && (
        <TacticsPanel
          tactics={tactics}
          effectiveDay={today?.trading_day ?? ""}
          loading={loading}
          onReload={loadTactics}
        />
      )}

      {/* —— 案例复盘（Y3-07）—— */}
      {tab === "replay" && (
        <ReplayPanel
          page={replay}
          events={replayEvents}
          query={replayQuery}
          loading={loading}
          effectiveDay={today?.trading_day ?? ""}
          onQuery={loadReplay}
          onReset={resetReplay}
          onOpenReport={onOpenReport}
        />
      )}

      {/* —— 练习（Y4-03 / Y4-04）—— */}
      {tab === "practice" && (
        <PracticePanel
          client={client}
          events={replayEvents}
          page={replay}
          query={replayQuery}
          loading={loading}
          effectiveDay={today?.trading_day ?? ""}
          onQuery={loadReplay}
          onReset={resetReplay}
          onSaveActivity={saveLearningActivity}
        />
      )}

      {/* —— 席位档案 —— */}
      {tab === "profile" && (
        <>
          <p className="youzi-note">
            从观察名单选一个营业部，或<strong>输入任意精确席位代码</strong>（含今日榜单里眼生的营业部），
            看它近 N 日出现过的股票与方向。
            <strong>只展示披露路径事实，不做胜率排行。</strong>
            （数据源不支持模糊查询，因此只能按精确席位代码查。）
          </p>
          {/* U02：精确代码输入——「看席位」看到一个陌生席位后，两步内能继续追问它的档案。 */}
          <div className="youzi-chips">
            <input
              type="text"
              className="mono"
              aria-label="席位代码（精确）"
              placeholder="输入精确席位代码，如 10025390"
              value={profileCodeInput}
              onChange={(event) => setProfileCodeInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && profileCodeInput.trim()) void loadProfile(profileCodeInput.trim());
              }}
            />
            <button
              className="ghost-btn small"
              disabled={!profileCodeInput.trim()}
              onClick={() => void loadProfile(profileCodeInput.trim())}
            >
              查档案
            </button>
          </div>
          <div className="youzi-chips">
            {(watchlist?.seats ?? []).map((seat) => (
              <button key={seat.operatedept_code} className="ghost-btn small" onClick={() => void loadProfile(seat.operatedept_code)}>
                {seat.name.replace(/证券|股份有限公司|营业部|有限责任公司/g, "").slice(0, 12) || seat.operatedept_code}
              </button>
            ))}
            {watchlist === null && <span className="youzi-muted">观察名单未加载。</span>}
          </div>

          {profile && !profile.available && <div className="youzi-alert">{profile.reason || "席位档案未取得。"}</div>}
          {profile?.available && (
            <div className="youzi-detail">
              <h3>
                {profile.watchlist_name || profile.operatedept_code}
                {profile.is_watchlist && <span className="youzi-badge hit">观察名单</span>}
                {!profile.is_watchlist && <span className="youzi-badge bucket">未在你的观察名单</span>}
              </h3>
              {!profile.is_watchlist && (
                <p className="youzi-muted">
                  该席位不在观察名单内（档案照样可查）。加入观察名单暂未开放（席位名单写入属名单管理能力，本期未做，故不提供空按钮）。
                </p>
              )}
              {/* U03：数据源不回 total——取满一页时必须如实声明可能截断；未满一页时不出现截断提示。 */}
              {profile.possibly_truncated ? (
                <p className="youzi-muted">
                  最近 {profile.returned ?? profile.records.length} 条（按交易日倒序；已取满单页上限 {profile.page_size ?? 50} 条，
                  <strong>可能还有更早记录未显示</strong>。数据源未提供总数，故不做翻页，宁缺不假）。
                </p>
              ) : (
                <p className="youzi-muted">
                  共 {profile.returned ?? profile.records.length} 条（按交易日倒序，未达单页上限 {profile.page_size ?? 50} 条，即已全部展示）。
                </p>
              )}
              <table className="youzi-table">
                <thead>
                  <tr>
                    <th>交易日</th><th>代码</th><th>名称</th>
                    <th className="num">买入</th><th className="num">卖出</th><th className="num">净额</th>
                    <th>上榜原因</th>
                  </tr>
                </thead>
                <tbody>
                  {profile.records.map((row, index) => (
                    <tr key={`${row.trading_day}-${row.security_code}-${index}`}>
                      <td className="mono">{dayLabel(row.trading_day)}</td>
                      <td className="mono">{row.security_code}</td>
                      <td>{row.security_name}</td>
                      <td className="num">{money(row.buy)}</td>
                      <td className="num">{money(row.sell)}</td>
                      <td className={`num ${row.net != null && row.net < 0 ? "neg" : ""}`}>{money(row.net)}</td>
                      <td className="youzi-reason">{row.explanation}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {profile.note && <p className="youzi-muted">{profile.note}</p>}
            </div>
          )}
        </>
      )}

      {/* —— 我的持仓（P4-E01 交叉提示）—— */}
      {tab === "cross" && (
        <>
          {cross && !cross.available && (
            <div className="youzi-alert">
              <strong>未取得当日上榜数据</strong>
              <span>{cross.reason || "数据源未返回数据。"}</span>
              <button className="ghost-btn" onClick={() => void loadCrossCheck()}>重新取数</button>
            </div>
          )}
          {cross?.available && (
            <>
              <p className="youzi-note">
                比对了本机 {cross.checked} 只持仓/自选与当日龙虎榜。
                <strong>只提示「今日上榜」这一披露事实，不提示买。</strong>
                比对在本地完成，仅交易日用于查询，不上传你的持仓清单。
              </p>
              {cross.hits.length === 0 ? (
                <p className="youzi-muted">
                  今日没有持仓/自选出现在龙虎榜上。
                  {cross.note ? ` ${cross.note}` : ""}
                </p>
              ) : (
                <table className="youzi-table">
                  <thead>
                    <tr>
                      <th>代码</th><th>名称</th><th>上榜原因</th>
                      <th className="num">净额</th><th>状态</th><th>席位</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cross.hits.map((hit) => (
                      <tr key={hit.security_code} className="hl">
                        <td className="mono">{hit.security_code}</td>
                        <td>{hit.security_name}</td>
                        <td className="youzi-reason">{hit.explanation}</td>
                        <td className="num">{money(hit.billboard_net_amt)}</td>
                        <td>
                          {hit.statuses.map((status) => (
                            <span key={status} className="youzi-badge">
                              {status === "holding" ? "持仓" : "自选"}
                            </span>
                          ))}
                        </td>
                        <td>
                          {hit.watchlist_hit && <span className="youzi-badge hit">观察席位参与</span>}
                          {/* G03：交叉命中的原始披露页（构造式 URL：交易日 + 代码）。 */}
                          {hit.source_url ? (
                            <a
                              className="youzi-src-link"
                              href={hit.source_url}
                              target="_blank"
                              rel="noreferrer noopener"
                              title="东财龙虎榜个股页（由交易日 + 证券代码拼出，上游不返回 URL）"
                            >
                              原始页 ↗
                            </a>
                          ) : null}
                          {/* U07：交叉命中行直达研究工作台（一次点击）。 */}
                          {onOpenReport && (
                            <button
                              className="ghost-btn small"
                              title="带这只票去研究工作台看个股研报（U07）"
                              onClick={() => onOpenReport(hit.security_code)}
                            >
                              去研报
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {cross.note && cross.hits.length > 0 && <p className="youzi-muted">{cross.note}</p>}
            </>
          )}
          {cross === null && !loading && <p className="youzi-muted">切换本页即比对…</p>}
        </>
      )}

      {/* —— 指数席位（D05 对照页）—— */}
      {tab === "cffex" && (
        <>
          {cffex && !cffex.available && (
            <div className="youzi-alert">
              <strong>未取得中金所席位统计</strong>
              <span>{cffex.reason || "数据源未返回数据。"}</span>
              <p className="youzi-muted">非交易日或收盘前拿不到当日文件属正常，已如实降级而不编造。</p>
              <button className="ghost-btn" onClick={() => void loadCffex()}>重新取数</button>
            </div>
          )}
          {cffex?.available && (
            <>
              <p className="youzi-note">
                中金所股指期货会员席位（交易日 {dayLabel(cffex.trading_day)}）。这是<strong>对照页</strong>：
                看指数层面谁在增仓/减仓，不与个股龙虎榜混用。
              </p>
              <table className="youzi-table">
                <thead>
                  <tr>
                    <th>品种</th><th>对应现货</th><th className="num">合约数</th>
                    <th className="num">成交合计</th><th className="num">持仓合计</th><th className="num">基差</th>
                  </tr>
                </thead>
                <tbody>
                  {cffex.products.map((product) => (
                    <tr key={product.product_id}>
                      <td className="mono">{product.product_id}</td>
                      <td className="mono">{product.spot_index_code || "未映射"}</td>
                      <td className="num">{product.contract_count}</td>
                      <td className="num">{product.total_volume == null ? "未取得" : product.total_volume.toLocaleString()}</td>
                      <td className="num">{product.total_open_interest == null ? "未取得" : product.total_open_interest.toLocaleString()}</td>
                      <td className="num">{cffex.basis?.[product.product_id] ?? "未计算"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {cffex.basis_note && <p className="youzi-muted">{cffex.basis_note}</p>}
              {cffex.text && <pre className="youzi-pre">{cffex.text}</pre>}
              {cffex.limitations && cffex.limitations.length > 0 && (
                <ul className="youzi-limits">
                  {cffex.limitations.map((item) => <li key={item}>{item}</li>)}
                </ul>
              )}
            </>
          )}
          {cffex === null && !loading && <p className="youzi-muted">切换本页即取数…</p>}
        </>
      )}

      {/* —— 学习（D04）—— */}
      {tab === "learn" && <LearnPanel client={client} onSaveActivity={saveLearningActivity} onNavigate={setTab} />}

      <footer className="youzi-footer">
        不构成投资建议；本插件用于识别席位事实。席位为营业部通道，非实名账户。
      </footer>
    </section>
  );
}

/**
 * Y3-07 案例复盘：按**证券代码 + 日期区间**查询披露事件时间线。
 *
 * 展示口径（路线图 §6 边界）：
 *   1. 严格**代码输入**：没有推荐列表、没有排行、没有「最活跃席位」。
 *   2. 时间线以**披露事件**为单位（交易日 × 席位 × 方向 × 原始原因），
 *      而不是按日期把同一天的多条原因合并成一行。
 *   3. 六个离散期限原样展示 `label / status / value_pct / target_date`：
 *      `未到期` 与 `已到期但数据源未给值` 必须分开说，不写 0 冒充。
 *   4. 部分覆盖 / 预算耗尽 / 未知口径都给显式提示，**不显示「路径全部走完」**，
 *      也不显示「是否了结」。
 */
function ReplayPanel({
  page,
  events,
  query,
  loading,
  effectiveDay,
  onQuery,
  onReset,
  onOpenReport,
}: {
  page: YouziReplayPage | null;
  /** 累计页（含「加载更多」追加的部分）。 */
  events: YouziReplayEvent[];
  query: YouziReplayQuery | null;
  loading: boolean;
  /** 今日有效日，用作日期区间的默认替代。 */
  effectiveDay: string;
  onQuery: (query: YouziReplayQuery, cursor?: string, append?: boolean) => void;
  onReset: () => void;
  /** U07：复盘行展开后可直达研究工作台。 */
  onOpenReport?: (symbol: string) => void;
}) {
  const [code, setCode] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [openEventId, setOpenEventId] = useState<string | null>(null);
  /** D04（2026-09-19 用户走查）：手填 YYYYMMDD 易错格式，给一组按有效交易日倒推的快捷区间。 */
  function setRange(days: number): void {
    const iso = /^[0-9]{8}$/.test(effectiveDay)
      ? `${effectiveDay.slice(0, 4)}-${effectiveDay.slice(4, 6)}-${effectiveDay.slice(6, 8)}`
      : new Date().toISOString().slice(0, 10);
    const to = new Date(`${iso}T00:00:00`);
    const from = new Date(to);
    from.setDate(to.getDate() - (days - 1));
    const fmt = (d: Date) => `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
    setStart(fmt(from));
    setEnd(fmt(to));
  }

  const submit = () => {
    const trimmedCode = code.trim();
    const trimmedStart = start.trim();
    const trimmedEnd = end.trim();
    if (!trimmedCode || !trimmedStart || !trimmedEnd) return;
    // 换代码/换区间 → 先清空累计页，避免两段结果混在同一条时间线上。
    onReset();
    onQuery({
      securityCode: trimmedCode,
      start: trimmedStart,
      end: trimmedEnd,
      asOf: effectiveDay || trimmedEnd,
      pageSize: 20,
    });
  };

  return (
    <>
      <p className="youzi-note">
        输入<strong>证券代码</strong>与日期区间，查看这段时间内该证券的披露事件时间线。
        <strong>只按代码查，不提供推荐或排行。</strong>
      </p>

      <div className="youzi-form">
        <label>
          <span>证券代码</span>
          <input
            className="mono"
            value={code}
            placeholder="如 600519"
            maxLength={6}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        <label>
          <span>起始日</span>
          <DatePicker compact value={start} onChange={setStart} ariaLabel="起始日" />
        </label>
        <label>
          <span>结束日</span>
          <DatePicker compact value={end} onChange={setEnd} ariaLabel="结束日" />
        </label>
        <span className="youzi-range-chips" role="group" aria-label="快捷时间区间">
          <button className="tag-button" onClick={() => setRange(30)}>近 30 天</button>
          <button className="tag-button" onClick={() => setRange(90)}>近 90 天</button>
          <button className="tag-button" onClick={() => setRange(180)}>近半年</button>
        </span>
        <button className="primary-button" disabled={loading} onClick={submit}>
          {loading ? "取数中…" : "查询"}
        </button>
      </div>

      {page === null && <p className="youzi-muted">填 6 位代码并点「查询」；区间可用右侧快捷按钮直接填好（日期格式 YYYYMMDD，按有效交易日倒推）。</p>}

      {/* 降级：日历缺失 / 区间内无交易日——都不是「没有事件」。 */}
      {page && !page.available && (
        <div className="youzi-alert">
          <strong>未生成复盘结果</strong>
          <span>{REPLAY_REASON_LABELS[page.reason ?? ""] || page.reason || "未取得数据。"}</span>
          <p className="youzi-muted">
            这里是<strong>「我还不能下结论」</strong>，不是「区间内没有披露」。
          </p>
        </div>
      )}

      {page && page.available && (
        <>
          <div className="youzi-summary">
            <span>
              代码 <strong className="mono">{page.security_code}</strong>
            </span>
            <span>
              区间{" "}
              <strong className="mono">
                {dayLabel(page.range_start)} → {dayLabel(page.range_end)}
              </strong>
            </span>
            <span>
              事件 <strong>{page.total_events}</strong> 条
              {events.length < page.total_events && `（已载入 ${events.length}）`}
            </span>
          </div>

          {/* 预算护栏：超上限时保留最近的那些日子，并说清为什么少了。 */}
          {page.truncated && (
            <div className="youzi-alert warn">
              <strong>区间被上限截短</strong>
              <span>{page.truncated_reason || "已按上限保留最近的部分交易日。"}</span>
            </div>
          )}

          {(page.context_days?.length ?? 0) > 0 && (
            <p className="youzi-muted">
              为凑齐首个事件的五交易日窗口，额外前取了{" "}
              <span className="mono">{page.context_days?.map(dayLabel).join("、")}</span>
              ——这些日子<strong>不计入</strong>上面的事件范围。
            </p>
          )}

          {Object.keys(page.coverage).length > 0 && (
            <CoverageTable
              windowDays={Object.keys(page.coverage).sort()}
              coverage={page.coverage}
            />
          )}

          {events.length === 0 ? (
            <div className="youzi-detail">
              <h3>区间内没有披露事件</h3>
              <p className="youzi-muted">
                这是「已取到、且没有命中」——与「没取到」不同。若上方覆盖表里存在
                <strong>无法判断</strong>或<strong>取数失败</strong>的日子，那些天的结论不成立。
              </p>
            </div>
          ) : (
            <div className="youzi-detail">
              <h3>披露事件时间线</h3>
              <table className="youzi-table">
                <thead>
                  <tr>
                    <th>交易日</th>
                    <th>席位</th>
                    <th>方向</th>
                    <th className="num">金额</th>
                    <th>上榜原因</th>
                    <th>事实 / 期限</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <ReplayEventRow
                      key={event.event_id}
                      event={event}
                      open={openEventId === event.event_id}
                      onToggle={() =>
                        setOpenEventId((current) =>
                          current === event.event_id ? null : event.event_id,
                        )
                      }
                      boundary={page.boundary_notice}
                      onOpenReport={onOpenReport}
                    />
                  ))}
                </tbody>
              </table>

              {page.has_more && (
                <button
                  className="ghost-btn"
                  disabled={loading}
                  onClick={() => query && onQuery(query, page.cursor, true)}
                >
                  {loading ? "载入中…" : `加载更多（还有 ${page.total_events - events.length} 条）`}
                </button>
              )}
            </div>
          )}

          {/* 限缩说明固定展示，且原样透出后端文案（前端不改写）。 */}
          {page.limitations.length > 0 && (
            <ul className="youzi-muted youzi-limits">
              {page.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          <p className="youzi-muted">{page.boundary_notice}</p>
        </>
      )}
    </>
  );
}

/** 一条披露事件：折叠时一行，展开后给出六个期限与来源定位。 */
function ReplayEventRow({
  event,
  open,
  onToggle,
  boundary,
  onOpenReport,
}: {
  event: YouziReplayEvent;
  open: boolean;
  onToggle: () => void;
  boundary: string;
  onOpenReport?: (symbol: string) => void;
}) {
  const omitted = event.fact_omitted_reason;
  const facts = event.fact_types;

  return (
    <>
      <tr className={open ? "open" : ""} onClick={onToggle}>
        <td className="mono">{dayLabel(event.trading_day)}</td>
        <td>
          {event.operatedept_name || <span className="youzi-muted">未披露</span>}
          <span className="youzi-code mono">{event.operatedept_code || "—"}</span>
        </td>
        <td>{event.direction === "buy" ? "买入" : "卖出"}</td>
        <td className="num">{money(event.amount)}</td>
        <td className="youzi-reason">{event.explanation || "—"}</td>
        <td>
          {facts.length > 0 ? (
            facts.map((type) => (
              <span key={type} className="youzi-badge hit">
                {FACT_LABELS[type] || type}
              </span>
            ))
          ) : (
            <span className="youzi-badge">{OMITTED_LABELS[omitted] || omitted || "无事实"}</span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="youzi-detail-row">
          <td colSpan={6}>
            <div className="youzi-detail">
              {onOpenReport && (
                <button
                  className="ghost-btn small"
                  title="带这只票去研究工作台看个股研报（U07）"
                  onClick={() => onOpenReport(event.security_code)}
                >
                  去研报
                </button>
              )}
              <h4>期限状态</h4>
              <p className="youzi-muted">
                「未到期」与「已到期但数据源未给值」是两件事，下表分开呈现；
                目标日为空表示按当前时点还推不出那一天（不用未来交易日反推）。
              </p>
              <table className="youzi-table compact">
                <thead>
                  <tr>
                    <th>期限</th>
                    <th className="num">交易日数</th>
                    <th className="num">累计涨跌幅</th>
                    <th>状态</th>
                    <th>目标交易日</th>
                  </tr>
                </thead>
                <tbody>
                  {event.horizons.map((horizon) => (
                    <tr key={horizon.label}>
                      <td className="mono">{horizon.label}</td>
                      <td className="num">{horizon.sessions}</td>
                      <td className="num">{pct(horizon.value_pct)}</td>
                      <td>
                        {HORIZON_STATUS_LABELS[horizon.status] || horizon.status}
                        {horizon.reason && (
                          <span className="youzi-muted mono"> {horizon.reason}</span>
                        )}
                      </td>
                      <td className="mono">
                        {horizon.target_date ? dayLabel(horizon.target_date) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <h4>来源定位</h4>
              <table className="youzi-table compact">
                <tbody>
                  <tr>
                    <td>来源报告</td>
                    <td className="mono">{shortReport(event.source_report)}</td>
                  </tr>
                  <tr>
                    <td>来源记录 ID</td>
                    <td className="mono youzi-break">{event.source_record_id || "—"}</td>
                  </tr>
                  {/* G03：可点回链（构造式 URL：交易日 + 代码；后端拼不出时为空串）。 */}
                  <tr>
                    <td>原始披露页</td>
                    <td className="mono youzi-break">
                      {event.source_url ? (
                        <a href={event.source_url} target="_blank" rel="noreferrer noopener">
                          东财龙虎榜个股页 ↗
                        </a>
                      ) : (
                        <span className="youzi-muted">未取得（交易日或代码不合形，不构造猜测链接）</span>
                      )}
                    </td>
                  </tr>
                  <tr>
                    <td>事件 ID</td>
                    <td className="mono youzi-break">{event.event_id}</td>
                  </tr>
                </tbody>
              </table>
              <p className="youzi-muted">{boundary}</p>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Y2-10 跨日事实面板。
 *
 * 展示原则（与全页一致，且被路线图逐条要求）：
 *   1. 只展示「结束/最近出现日为有效日 D」的标签——五日前就结束的旧事实不进今日视图。
 *   2. 支持按事实类型筛选。
 *   3. 每条事实可展开证据详情（逐日：日期 / 方向 / 金额 / 原文原因 / 可定位 ID）。
 *   4. 固定边界句由**后端下发**（`boundary_notice`）原样渲染，前端不另写一份。
 *   5. 窗口不完整 → `available=false` 但**仍列出已取得的原始披露**：不把
 *      「我没取全」说成「没有事件」。
 *   6. 输出里没有涨跌 / 收益 / 胜率字段——页面也不额外计算。
 */
function TacticsPanel({
  tactics,
  effectiveDay,
  loading,
  onReload,
}: {
  tactics: YouziTactics | null;
  /** 与「今日席位」共用的有效日（YYYYMMDD）；today 未取到时为空串。 */
  effectiveDay: string;
  loading: boolean;
  onReload: (day?: string | null) => void;
}) {
  const [filter, setFilter] = useState<string>("all");
  const [openFactId, setOpenFactId] = useState<string | null>(null);

  if (tactics === null) {
    return <p className="youzi-muted">{loading ? "取数中…" : "切换本页即取数…"}</p>;
  }

  const boundary = tactics.boundary_notice;
  const day = tactics.effective_day || effectiveDay;

  // 降级：日历缺失 / 非交易日 / 窗口不足 / 分页不完整——四者都不生成事实。
  if (!tactics.available) {
    const reasonText: Record<string, string> = {
      calendar_missing: "交易日历不可用，无法确认有效交易日；已停止计算，不做自然日推算。",
      not_a_trading_day: "请求日不是已确认交易日（休市 / 未来日 / 当日尚未披露，三者形状相同）。",
      insufficient_trading_history: "历史交易日不足以构成完整五交易日窗口。",
      incomplete_window: "窗口内存在未完整取得的报告；已停止生成事实，仅展示已取得的原始披露。",
      not_implemented: "该端点尚未接线。",
    };
    return (
      <>
        <div className="youzi-alert">
          <strong>未生成跨日事实</strong>
          <span>{reasonText[tactics.reason ?? ""] || tactics.reason || "未取得数据。"}</span>
          <p className="youzi-muted">
            这里是<strong>「我还不能下结论」</strong>，不是「没有这样的事实」——两者含义不同，故不显示空表。
          </p>
          <button className="ghost-btn" onClick={() => onReload(tactics.request_day || null)}>重新取数</button>
        </div>
        {tactics.window_days.length > 0 && (
          <CoverageTable windowDays={tactics.window_days} coverage={tactics.coverage} />
        )}
        {tactics.raw_billboard.length > 0 && (
          <div className="youzi-detail">
            <h3>已取得的原始披露（未生成事实）</h3>
            <p className="youzi-muted">
              下列行是从已取到的报告中直接读出的上榜记录，未经跨日推断，也不构成任何结论。
            </p>
            <table className="youzi-table">
              <thead>
                <tr><th>交易日</th><th>代码</th><th>名称</th></tr>
              </thead>
              <tbody>
                {tactics.raw_billboard.map((row) => (
                  <tr key={`${row.trading_day}-${row.security_code}`}>
                    <td className="mono">{dayLabel(row.trading_day)}</td>
                    <td className="mono">{row.security_code}</td>
                    <td>{row.security_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="youzi-muted">{boundary}</p>
      </>
    );
  }

  const ending = factsEndingOn(tactics.items, day);
  const types = Array.from(new Set(ending.map((fact) => fact.fact_type))).sort();
  const shown = filter === "all" ? ending : ending.filter((fact) => fact.fact_type === filter);

  return (
    <>
      <p className="youzi-note">
        截至交易日 <strong>{dayLabel(day)}</strong> 的五交易日窗口
        （{tactics.window_days.map(dayLabel).join(" / ")}）内，
        <strong>已披露路径事实</strong>共 {ending.length} 条
        {tactics.items.length !== ending.length
          ? `（窗口内共 ${tactics.items.length} 条，其余 ${tactics.items.length - ending.length} 条的最近出现日早于 ${dayLabel(day)}，不计入今日视图）`
          : ""}
        。<strong>这里只有披露事实，没有涨跌、收益或胜率。</strong>
      </p>

      <CoverageTable windowDays={tactics.window_days} coverage={tactics.coverage} />

      {types.length > 0 && (
        <div className="youzi-chips">
          <button
            className={filter === "all" ? "ghost-btn small active" : "ghost-btn small"}
            onClick={() => setFilter("all")}
          >
            全部 {ending.length}
          </button>
          {types.map((type) => (
            <button
              key={type}
              className={filter === type ? "ghost-btn small active" : "ghost-btn small"}
              onClick={() => setFilter(type)}
              title={FACT_LABELS[type] || type}
            >
              {FACT_LABELS[type] || type} {ending.filter((f) => f.fact_type === type).length}
            </button>
          ))}
        </div>
      )}

      {ending.length === 0 && (
        <p className="youzi-muted">
          完整窗口内<strong>未检出</strong>三类事实。这只是「本窗口的这两个报告中未检出」，
          不等于相关行为没有发生——榜外席位不可见。
        </p>
      )}

      {shown.length > 0 && (
        <table className="youzi-table">
          <thead>
            <tr>
              <th>类型</th><th>代码</th><th>名称</th><th>席位</th>
              <th>参与交易日</th><th className="num">证据</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((fact) => (
              <FactRow
                key={fact.fact_id}
                fact={fact}
                open={openFactId === fact.fact_id}
                onToggle={() => setOpenFactId(openFactId === fact.fact_id ? null : fact.fact_id)}
                boundary={boundary}
              />
            ))}
          </tbody>
        </table>
      )}

      {shown.length === 0 && ending.length > 0 && <p className="youzi-muted">该类型下没有事实。</p>}

      {Object.keys(tactics.excluded).length > 0 && (
        <div className="youzi-detail">
          <h3>被排除的披露行（及原因）</h3>
          <p className="youzi-muted">
            这些行参与了读取但**不能**用于跨日匹配；列出它们是为了让「未检出」可核查，
            而不是让它看起来像「没有记录」。
          </p>
          <ul className="youzi-limits">
            {Object.entries(tactics.excluded)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([key, count]) => (
                <li key={key}>
                  {EXCLUDE_LABELS[key] || key}：{count} 行
                </li>
              ))}
          </ul>
        </div>
      )}

      {tactics.conflicts.length > 0 && (
        <div className="youzi-alert">
          <strong>同日同方向金额冲突 {tactics.conflicts.length} 组</strong>
          <span>同席位同日在同一侧披露了不同金额；本产品**不任取一条**生成对照。</span>
          <ul className="youzi-limits">
            {tactics.conflicts.slice(0, 10).map((item) => (
              <li key={`${item.operatedept_code}-${item.trading_day}-${item.direction}`}>
                {item.operatedept_code} {dayLabel(item.trading_day)} {item.direction}：
                {item.amounts.map(money).join(" / ")}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="youzi-muted">{boundary}</p>
    </>
  );
}

/** 逐日覆盖状态表：让「哪一天没取全」一眼可见。 */
function CoverageTable({
  windowDays,
  coverage,
}: {
  windowDays: string[];
  coverage: Record<string, string>;
}) {
  if (windowDays.length === 0) return null;
  return (
    <div className="youzi-chips">
      {windowDays.map((day) => {
        const state = coverage[day] ?? "unknown";
        return (
          <span
            key={day}
            className={state === "complete" ? "youzi-badge hit" : "youzi-badge bucket"}
            title={COVERAGE_LABELS[state] || state}
          >
            {dayLabel(day)} · {COVERAGE_LABELS[state] || state}
          </span>
        );
      })}
    </div>
  );
}

/** 单条事实行 + 可展开的逐日证据详情（Y2-03 / Y2-06 回链）。 */
function FactRow({
  fact,
  open,
  onToggle,
  boundary,
}: {
  fact: YouziTacticsFact;
  open: boolean;
  onToggle: () => void;
  boundary: string;
}) {
  return (
    <>
      <tr className="hl">
        <td><span className="youzi-badge hit">{FACT_LABELS[fact.fact_type] || fact.fact_type}</span></td>
        <td className="mono">{fact.security_code}</td>
        <td>{fact.security_name || "—"}</td>
        <td>
          {fact.operatedept_name || "未统一命名"}
          <br />
          <span className="mono youzi-muted">{fact.operatedept_code}</span>
        </td>
        <td className="mono">{fact.days.map(dayLabel).join(" / ")}</td>
        <td className="num">{fact.evidence.length}</td>
        <td>
          <button className="ghost-btn small" onClick={onToggle}>
            {open ? "收起证据" : "看证据"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <div className="youzi-detail">
              <h3>逐日证据 · {FACT_LABELS[fact.fact_type] || fact.fact_type}</h3>
              <table className="youzi-table">
                <thead>
                  <tr>
                    <th>交易日</th><th>方向</th><th className="num">金额</th>
                    <th>上榜原因（原文）</th><th>来源报告</th><th>证据定位 ID</th>
                    <th>原始披露页</th>
                  </tr>
                </thead>
                <tbody>
                  {fact.evidence.map((item) => (
                    <tr key={item.evidence_id}>
                      <td className="mono">{dayLabel(item.trading_day)}</td>
                      <td>{item.direction === "buy" ? "买入" : "卖出"}</td>
                      <td className="num">
                        {money(item.amount)}
                        {item.amount_reason ? (
                          <span className="youzi-badge bucket" title={item.amount_reason}>金额不合法</span>
                        ) : null}
                      </td>
                      <td className="youzi-reason">{item.explanation || "—"}</td>
                      <td className="mono youzi-reason">{shortReport(item.source_report)}</td>
                      <td className="mono youzi-reason" title={item.evidence_id}>
                        {item.evidence_id.slice(0, 16)}…
                      </td>
                      {/* G03：每条逐日证据都能点到原始披露页（拼不出时显式说明，不给半截链接）。 */}
                      <td className="mono youzi-reason">
                        {item.source_url ? (
                          <a href={item.source_url} target="_blank" rel="noreferrer noopener">原始页 ↗</a>
                        ) : (
                          <span className="youzi-muted">未取得</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="youzi-muted">
                事实指纹 <span className="mono">{fact.fact_id.slice(0, 16)}…</span>。
                「证据定位 ID」是来源报告 + 证券 + 交易日 + 统计周期 + 原因原文 + 席位 + 方向 + 金额
                的派生哈希；配合「来源报告」即可在原始披露里逐条定位，而不是只给一个日期。
                「原始披露页」是由<strong>交易日 + 证券代码</strong>确定性拼出的东财页面（上游不返回 URL），
                拼不出时显式写「未取得」，不给半截链接。
                金额为<strong>该方向侧的披露金额</strong>，不做相加，也不计算卖出比例或收益。
              </p>
              <p className="youzi-muted">{boundary}</p>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Y4 十一课：与 `plugins/official/youzi-radar/curriculum.json` 同源。
 *
 * ★ 新增的 L6–L10 带 `boundary`（可见性边界）。边界必须在页面上**可见**——
 * 只在 JSON 里写、页面上不显示，等于使用者看不到「这节课不能推出什么」。
 * 改动本数组时请同步课程 JSON，并由 `test_youzi_radar.py` 与
 * `YouziLearn.test.tsx` 两侧守住一致性。
 */
/**
 * Y4-03 / Y4-04：练习面板。
 *
 * 设计口径（路线图 §7 可勾选任务的逐字要求）：
 *   1. **Y4-03 识别练习**用「披露事实 + 证据回链」完成，样例来自**真实取数**；
 *      当日没有匹配就明确说「今天没有可练的样本」并允许换区间查历史，
 *      绝不为了凑题而编造一条事件。
 *   2. **Y4-04 复盘练习**只挑**实际已披露 D1/D2/D5** 的样例；结果**先隐藏**，
 *      使用者先写下「事实 / 假设 / 失效条件」才能揭示；并说明当前快照与样本选择
 *      偏差，**不声称这是严格历史时点回测**。
 *   3. 练习过程中不产生任何绩效统计：不记分、不算胜率、不给排行。
 */
type PracticeMode = "identify" | "review";

/** 复盘练习承诺的三个期限（与 Y0-08 冻结的期限集合一致，此处只取最短三档）。 */
const PRACTICE_HORIZONS = ["D1", "D2", "D5"] as const;

/**
 * 从真实事件里挑出可用的练习样例。
 *
 * - `identify`：只要事件本身（有事实标签或明确省略原因）即可练；
 * - `review`：**必须** D1/D2/D5 三个期限都 `disclosed`——路线图要求「只选择实际已披露」，
 *   否则揭示出来的不是练习对象而是数据缺口。
 */
function pickPracticeSamples(
  events: YouziReplayEvent[] | undefined,
  mode: PracticeMode,
): YouziReplayEvent[] {
  const list = events ?? [];
  if (mode === "identify") return list;
  return list.filter((event) =>
    PRACTICE_HORIZONS.every((label) => {
      const horizon = event.horizons.find((item) => item.label === label);
      return horizon?.status === "disclosed" && horizon.value_pct != null;
    }),
  );
}

function PracticePanel({
  client,
  events,
  loading,
  query,
  page,
  effectiveDay,
  onQuery,
  onReset,
  onSaveActivity,
}: {
  client: CoreClient;
  events: YouziReplayEvent[] | undefined;
  loading: boolean;
  query: YouziReplayQuery | null;
  page: YouziReplayPage | null;
  effectiveDay: string;
  onQuery: (query: YouziReplayQuery, cursor?: string, append?: boolean) => void;
  onReset: () => void;
  onSaveActivity?: (input: SaveActivityInput) => Promise<boolean>;
}) {
  const [mode, setMode] = useState<PracticeMode>("identify");
  const [code, setCode] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  // 复盘练习的三段作答：写下之前不揭示结果。
  const [facts, setFacts] = useState("");
  const [assumptions, setAssumptions] = useState("");
  const [invalidations, setInvalidations] = useState("");
  const [revealed, setRevealed] = useState(false);
  /** U06：落库状态（idle/saving/saved/failed）。 */
  const [savedState, setSavedState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  /** U06：我的复盘记录（产品学习存储里最近的游资练习，objective 以【游资雷达练习】为标记）。 */
  const [records, setRecords] = useState<PracticeRecord[] | null>(null);

  // ★ `events` 在挂载瞬间可能还是 undefined（hook 初值），不能直接 `.length`。
  // 这里统一次兜底，后面所有分支都建立在同一个值上，避免各分支口径不一致。
  const allEvents = events ?? [];
  const samples = pickPracticeSamples(allEvents, mode);
  const reviewCandidates = allEvents.length;
  const filled = facts.trim().length > 0 && assumptions.trim().length > 0 && invalidations.trim().length > 0;

  /** U06：换样例与切模式共用的清理路径——三栏作答与揭示态一并清掉。 */
  const clearAnswers = () => {
    setFacts("");
    setAssumptions("");
    setInvalidations("");
    setRevealed(false);
    setSavedState("idle");
  };

  const submit = () => {
    const trimmedCode = code.trim();
    const trimmedStart = start.trim();
    const trimmedEnd = end.trim();
    if (!trimmedCode || !trimmedStart || !trimmedEnd) return;
    onReset();
    // U06：换样例与切模式走同一条清理路径（旧 switchMode 只清 revealed、不清三栏，
    // 上一题的作答会残留到新题——:1174 注释担心的风险在切模式分支同样成立）。
    clearAnswers();
    onQuery({
      securityCode: trimmedCode,
      start: trimmedStart,
      end: trimmedEnd,
      asOf: effectiveDay || trimmedEnd,
      pageSize: 20,
    });
  };

  const switchMode = (next: PracticeMode) => {
    setMode(next);
    clearAnswers();
  };

  /** U06：揭示的同时把三栏答案落库（append-only 学习记录），切页签/重开不丢。 */
  const reveal = () => {
    setRevealed(true);
    const sample = samples[0];
    if (onSaveActivity && sample) {
      setSavedState("saving");
      void onSaveActivity({
        unitType: "exercise",
        objective: `【游资雷达练习】复盘（${sample.security_code} ${sample.trading_day} ${sample.direction === "buy" ? "买入" : "卖出"}）：先写事实/假设/失效条件，再揭示`,
        userAnswer: `事实：${facts.trim()}\n假设：${assumptions.trim()}\n失效条件：${invalidations.trim()}`,
        boundInstrument: sample.security_code,
      }).then((ok) => setSavedState(ok ? "saved" : "failed"));
    }
  };

  // U06：进入练习页签时读回最近的学习活动（保存成功后随 savedState 变化刷新）。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await client.request<PracticeRecord[]>({ method: "GET", path: "/learning/activities" });
        if (!cancelled && response.status < 400 && Array.isArray(response.data)) {
          setRecords(
            response.data
              .filter((item) => item.objective?.startsWith("【游资雷达练习】"))
              .slice(0, 5),
          );
        } else if (!cancelled) {
          setRecords([]);
        }
      } catch {
        if (!cancelled) setRecords([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, savedState]);

  return (
    <>
      <p className="youzi-note">
        练习用<strong>真实披露数据</strong>完成，样例来自只读取数。
        <strong>不记分、不算胜率、不给排行</strong>——写完对照事实即可。
        答案会存进你自己的学习记录（learning_activities），只在本机。
      </p>

      <div className="youzi-chips">
        <button
          className={mode === "identify" ? "youzi-chip active" : "youzi-chip"}
          onClick={() => switchMode("identify")}
        >
          识别练习（Y4-03）
        </button>
        <button
          className={mode === "review" ? "youzi-chip active" : "youzi-chip"}
          onClick={() => switchMode("review")}
        >
          复盘练习（Y4-04）
        </button>
      </div>

      <p className="youzi-muted">
        {mode === "identify"
          ? "读一条披露事件，写出「已知」「未知」，再从证据回链核对来源。"
          : "只看已披露 D1/D2/D5 的样例；先写下事实 / 假设 / 失效条件，再揭示结果。"}
      </p>

      <div className="youzi-form">
        <label>
          <span>证券代码</span>
          <input
            className="mono"
            value={code}
            placeholder="如 600519"
            maxLength={6}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        <label>
          <span>起始日</span>
          <DatePicker compact value={start} onChange={setStart} ariaLabel="起始日" />
        </label>
        <label>
          <span>结束日</span>
          <DatePicker compact value={end} onChange={setEnd} ariaLabel="结束日" />
        </label>
        <button className="primary-button" disabled={loading} onClick={submit}>
          {loading ? "取数中…" : "取样例"}
        </button>
      </div>

      {page === null && <p className="youzi-muted">输入代码与区间后点「取样例」。</p>}

      {page && !page.available && (
        <div className="youzi-alert">
          <strong>本次练习没有可用样例</strong>
          <span>{REPLAY_REASON_LABELS[page.reason ?? ""] || page.reason || "未取得数据。"}</span>
          <p className="youzi-muted">
            这是<strong>「我还不能下结论」</strong>，不是「区间内没有披露」。换个区间再试。
          </p>
        </div>
      )}

      {page?.available && samples.length === 0 && (
        <div className="youzi-alert warn">
          <strong>这段区间里没有可用的练习样例</strong>
          <span>
            {mode === "review"
              ? `区间内共 ${reviewCandidates} 条披露事件，但没有一条同时已披露 D1/D2/D5——按口径不能拿来复盘练习。`
              : "区间内没有披露事件。"}
          </span>
          <p className="youzi-muted">
            换一段<strong>已走完期限</strong>的历史区间即可；这里不为了凑题而编造样例。
          </p>
        </div>
      )}

      {page?.available && samples.length > 0 && (
        <>
          <div className="youzi-summary">
            <span data-testid="practice-sample-count">
              可练样例 <strong>{samples.length}</strong> 条
              {mode === "review" && reviewCandidates > samples.length && (
                <span className="youzi-muted">
                  （区间内共 {reviewCandidates} 条，按「已披露 D1/D2/D5」筛掉{" "}
                  {reviewCandidates - samples.length} 条）
                </span>
              )}
            </span>
            <span>
              区间{" "}
              <strong className="mono">
                {dayLabel(page.range_start)} → {dayLabel(page.range_end)}
              </strong>
            </span>
          </div>

          <div className="youzi-detail">
            <h3>{mode === "identify" ? "识别练习：写出已知与未知" : "复盘练习：写下再揭示"}</h3>
            <table className="youzi-table">
              <thead>
                <tr>
                  <th>交易日</th>
                  <th>证券</th>
                  <th>席位</th>
                  <th>方向</th>
                  <th className="num">金额</th>
                  <th>上榜原因</th>
                  <th>事实</th>
                </tr>
              </thead>
              <tbody>
                {samples.slice(0, 1).map((event) => (
                  <tr key={event.event_id}>
                    <td className="mono">{dayLabel(event.trading_day)}</td>
                    <td className="mono">{event.security_code}</td>
                    <td>
                      {event.operatedept_name || <span className="youzi-muted">未披露</span>}
                      <span className="youzi-code mono">{event.operatedept_code || "—"}</span>
                    </td>
                    <td>{event.direction === "buy" ? "买入" : "卖出"}</td>
                    <td className="num">{money(event.amount)}</td>
                    <td className="youzi-reason">{event.explanation || "—"}</td>
                    <td>
                      {event.fact_types.length > 0 ? (
                        event.fact_types.map((type) => (
                          <span key={type} className="youzi-badge hit">
                            {FACT_LABELS[type] || type}
                          </span>
                        ))
                      ) : (
                        <span className="youzi-badge">
                          {OMITTED_LABELS[event.fact_omitted_reason] ||
                            event.fact_omitted_reason ||
                            "无事实"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="youzi-muted">
              证据回链：来源报告 <span className="mono">{samples[0]?.source_report || "—"}</span>
              、来源记录 ID <span className="mono">{samples[0]?.source_record_id || "—"}</span>
              、事件 ID <span className="mono">{samples[0]?.event_id || "—"}</span>
              。结论必须能回到这三个标识。
            </p>
          </div>

          {mode === "review" && (
            <div className="youzi-detail">
              <h4>先写下你的判断</h4>
              <p className="youzi-muted">
                顺序是硬要求：<strong>先写完再揭示</strong>。
                先看结果再补理由，练不出「写下失效条件」的习惯。
              </p>
              <div className="youzi-form column">
                <label>
                  <span>事实（已披露、可核对的部分）</span>
                  <textarea
                    className="youzi-textarea"
                    value={facts}
                    placeholder="例：09-15 该席位买入榜披露，金额…，原因是…"
                    onChange={(event) => setFacts(event.target.value)}
                  />
                </label>
                <label>
                  <span>假设（我的读法，可能错）</span>
                  <textarea
                    className="youzi-textarea"
                    value={assumptions}
                    placeholder="例：这可能是短线资金的一次集中买入。"
                    onChange={(event) => setAssumptions(event.target.value)}
                  />
                </label>
                <label>
                  <span>失效条件（什么情况说明我读错了）</span>
                  <textarea
                    className="youzi-textarea"
                    value={invalidations}
                    placeholder="例：若次日同一席位反向披露，则「集中买入」的读法不成立。"
                    onChange={(event) => setInvalidations(event.target.value)}
                  />
                </label>
              </div>
              <button
                className="primary-button"
                disabled={!filled || revealed || savedState === "saving"}
                onClick={reveal}
              >
                {revealed ? "已揭示" : filled ? "揭示结果" : "三栏都写完后可揭示"}
              </button>
              {savedState === "saving" && <span className="youzi-muted">保存学习记录中…</span>}
              {savedState === "saved" && (
                <span className="youzi-muted">✓ 已存入学习记录（learning_activities），切页签 / 重开应用不丢。</span>
              )}
              {savedState === "failed" && (
                <span className="youzi-muted">学习记录保存失败（答案仍在上方，可修正后重试）。</span>
              )}
            </div>
          )}

          {mode === "identify" && (
            <div className="youzi-detail">
              <h4>对照要点</h4>
              <ul className="youzi-muted youzi-limits">
                <li>上榜原因只标统计周期（单日 / 多日），不标资金意图。</li>
                <li>「未检出」不等于「未发生」；同一营业部不等于同一账户。</li>
                <li>事实标签只描述已披露的路径，不构成任何买卖时机判断。</li>
              </ul>
            </div>
          )}

          {/* U06：我的复盘记录——读产品自己的学习存储（GET /learning/activities），
              按 objective 前缀过滤出本页练习的落库条目（其余学习活动不掺进来）。 */}
          {records && records.length > 0 && (
            <div className="youzi-detail">
              <h4>我的复盘记录（最近 {records.length} 条）</h4>
              <ul className="youzi-muted">
                {records.map((record) => (
                  <li key={record.activity_id}>
                    <span className="mono">{record.completed_at.slice(0, 10)}</span>
                    {" — "}
                    {(record.user_answer || record.objective).replace(/\n/g, " / ").slice(0, 90)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {mode === "review" && revealed && (
            <div className="youzi-detail">
              <h4>揭示：已披露的期限</h4>
              <table className="youzi-table compact">
                <thead>
                  <tr>
                    <th>期限</th>
                    <th className="num">累计涨跌幅</th>
                    <th>状态</th>
                    <th>目标交易日</th>
                    <th>来源</th>
                  </tr>
                </thead>
                <tbody>
                  {samples[0]?.horizons
                    .filter((horizon) =>
                      (PRACTICE_HORIZONS as readonly string[]).includes(horizon.label),
                    )
                    .map((horizon) => (
                      <tr key={horizon.label}>
                        <td className="mono">{horizon.label}</td>
                        <td className="num">{pct(horizon.value_pct)}</td>
                        <td>{HORIZON_STATUS_LABELS[horizon.status] || horizon.status}</td>
                        <td className="mono">
                          {horizon.target_date ? dayLabel(horizon.target_date) : "—"}
                        </td>
                        <td className="youzi-muted">{horizon.source || "—"}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
              <p className="youzi-muted">
                <strong>这不是历史时点回测。</strong>
                披露数据是当前快照，样例又是我按「已披露 D1/D2/D5」挑出来的，
                天然偏向那些走完期限的案例；缺失与未到期的样本没被选进来。
                因此上面的涨跌幅<strong>不构成任何绩效统计</strong>，也不能反推策略优劣。
              </p>
            </div>
          )}

          {page.limitations.length > 0 && (
            <ul className="youzi-muted youzi-limits">
              {page.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          <p className="youzi-muted">{page.boundary_notice}</p>
        </>
      )}
    </>
  );
}

const LESSONS: Array<{
  id: string;
  title: string;
  mechanism: string;
  exercise: string;
  boundary?: string;
}> = [
  {
    id: "L0",
    title: "席位是通道，不是神仙",
    mechanism: "证据要有来源与主体；「代客」与营业部必须写在局限里。",
    exercise: "指出今日榜上一条「代客」或「机构专用」，并写出它为什么不能指向某个实名账户。",
  },
  {
    id: "L1",
    title: "怎么读一张龙虎榜",
    mechanism: "上榜原因、买/卖/净额、覆盖范围。",
    exercise: "任选今日一只上榜股，用证据句式复述它的上榜事实，不做任何预测。",
  },
  {
    id: "L2",
    title: "紫阳东路这样的短线席位",
    mechanism: "观察名单；行为模式是描述，不是标签圣经。",
    exercise: "若观察名单今日出现，记录买/卖方向与原因；未出现就写「今日未出现」，不编造。",
  },
  {
    id: "L3",
    title: "游资与情绪，不是基本面替代",
    mechanism: "个股研报 S1–S5 仍是主证据；席位只是条件补充。",
    exercise: "找一只有财报也有上榜的票，写清楚「席位能解释什么、不能解释什么」。",
  },
  {
    id: "L4",
    title: "当你的持仓碰到游资",
    mechanism: "决策日志：事实 / 假设 / 失效条件。",
    exercise: "用自己的一只持仓或自选，看今日是否上榜；没有就结束，不编造。",
  },
  {
    id: "L5",
    title: "跟席位的常见亏法",
    mechanism: "分享池「排序不含收益率」的同一理由。",
    exercise: "列出东财三日上涨概率（含原文里的「成功率」）为什么不能当本产品的排序键。",
  },
  {
    id: "L6",
    title: "上榜原因能说明什么，不能说明什么",
    mechanism: "上榜原因只标统计周期（单日 / 多日），不标资金意图——对应跨日事实页签的原因分类。",
    exercise: "各找一条单日口径与一条多日口径的原因，把触发条件原文抄下来，说明为什么不能用同一套解读。",
    boundary: "本课只讲「原因文本的统计周期可分类」，不讲任何买卖时机。",
  },
  {
    id: "L7",
    title: "相邻交易日的买卖榜记录怎么读",
    mechanism: "相邻两日分别出现在买入榜与卖出榜，是两个独立披露的叠加。",
    exercise: "找一条相邻交易日买卖榜记录，写出「已知」（两天各披露了什么）与「未知」。",
    boundary: "披露只覆盖进入榜单的席位；「未检出」不等于「未发生」，也推不出是否同一笔交易。",
  },
  {
    id: "L8",
    title: "连续与重复出现，不等于接力或返场",
    mechanism: "连续买入 / 重复出现是描述性事实，不带意图归因。",
    exercise: "找一条「连续买入」或「重复出现」标签，说明它字面断言了什么，为什么不能改写成「接力」。",
    boundary: "同一营业部不等于同一账户，无法据此确认持仓、清仓或同一笔交易。",
  },
  {
    id: "L9",
    title: "榜单结构：换手、市值与买卖占比",
    mechanism: "占比由本产品自算，且分子分母必须同口径——跨口径的比值没有意义。",
    exercise: "对今日一只上榜股写出买入占比，并注明分子与分母分别来自哪个口径、是否同一周期。",
    boundary: "占比是结构描述，不含涨跌预测，也不进任何排序键。",
  },
  {
    id: "L10",
    title: "披露之后：逐期限复盘与信息边界",
    mechanism: "六个期限各有四种状态；「尚未到期」与「已到期但数据源未给值」不是一回事。",
    exercise: "用复盘页签跑一段区间，各找一个「尚未到期」与一个「已到期但未给值」的期限，说明成因差别。",
    boundary: "期限状态只描述历史价格与数据可得性，空值不是 0，也不构成任何交易建议。",
  },
];

/** Y4-05：术语表逐词带证据边界；`currentUnobservable` 为真时页面必须显式标「当前不可观测」。 */
const GLOSSARY: Array<{ term: string; meaning: string; boundary: string; currentUnobservable: boolean }> = [
  {
    term: "首板",
    meaning: "某只股票本轮上涨中的第一个涨停板。",
    boundary: "本产品不做涨停板序列识别；页面能看到的是该股当日是否上榜及原因原文。",
    currentUnobservable: true,
  },
  {
    term: "接力",
    meaning: "在他人已拉起的涨停板之后继续买入同一只股票。",
    boundary: "披露数据不含「谁在谁的后面买」这种时序归因；相关事实只有描述性标签。",
    currentUnobservable: true,
  },
  {
    term: "反核",
    meaning: "在连续大跌（核按钮式抛售）之后反向买入。",
    boundary: "本产品不识别连续大跌形态，也不判定「反向」。",
    currentUnobservable: true,
  },
  {
    term: "一日游",
    meaning: "当日大涨、次日即回落，资金未持续停留。",
    boundary: "D1 期限只给出披露日收盘起算的累计涨跌幅，是价格事实，不是资金持续性结论。",
    currentUnobservable: true,
  },
  {
    term: "返场",
    meaning: "同一席位在离开一段时间后再次出现在同一只股票上。",
    boundary: "「返场」隐含同一账户的判断，而同一营业部不等于同一账户。",
    currentUnobservable: true,
  },
  {
    term: "锁仓",
    meaning: "买入后不卖出，长期持有。",
    boundary: "披露只覆盖进入榜单的席位，「未检出卖出」不等于「没有卖出」。",
    currentUnobservable: true,
  },
  {
    term: "翘板",
    meaning: "跌停板上被大量买单撬开。",
    boundary: "本产品不接入逐笔委托或盘口数据。",
    currentUnobservable: true,
  },
  {
    term: "地天板",
    meaning: "同一交易日内从跌停走到涨停。",
    boundary: "本产品不接入分时或逐笔数据，无法判定盘中是否触及涨跌停。",
    currentUnobservable: true,
  },
  {
    term: "核按钮",
    meaning: "迅速、集中地大额卖出，常伴随跌停。",
    boundary: "本产品不识别抛售形态；日频披露只看到某席位当日卖出金额。",
    currentUnobservable: true,
  },
];

/** U05/U06 共用类型与回调形状。 */
interface SaveActivityInput {
  unitType: "lesson" | "exercise";
  objective: string;
  userAnswer: string;
  boundInstrument?: string | null;
}

/** U06：学习活动记录（GET /learning/activities 返回行的子集）。 */
interface PracticeRecord {
  activity_id: string;
  objective: string;
  user_answer: string;
  completed_at: string;
}

/** U05：curriculum.json 的课程形状（plugins/official/youzi-radar/curriculum.json，version 2026-09-18-v2）。 */
interface CurriculumLesson {
  level: string;
  title: string;
  mechanism?: string;
  principle?: string;
  exercise?: string;
  acceptance?: string;
  boundary?: string;
}

interface CurriculumPayload {
  version?: string;
  updated_at?: string;
  purpose?: string;
  lessons: CurriculumLesson[];
}

/** U07：从课文 exercise 文本里已出现的页签名生成跳转（一次点击可达；不新增第二份课文映射，
 * 只有「文案关键词 → 页签」这一张必需的桥接表）。 */
const LESSON_TAB_JUMPS: Array<{ pattern: RegExp; tab: YouziTab; label: string }> = [
  { pattern: /今日|榜单/, tab: "today", label: "今日席位" },
  { pattern: /跨日事实/, tab: "tactics", label: "跨日事实" },
  { pattern: /复盘/, tab: "replay", label: "案例复盘" },
  { pattern: /持仓|自选/, tab: "cross", label: "我的持仓" },
  { pattern: /档案/, tab: "profile", label: "席位档案" },
];

/** U06：单课作答卡片（textarea 状态本地持有；保存走 POST /learning/activities）。 */
function LessonCard({
  lesson,
  onSaveActivity,
  onNavigate,
  onSaved,
}: {
  lesson: CurriculumLesson;
  onSaveActivity?: (input: SaveActivityInput) => Promise<boolean>;
  onNavigate: (tab: YouziTab) => void;
  onSaved?: () => void;
}) {
  const [answer, setAnswer] = useState("");
  const [saved, setSaved] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const jumps = LESSON_TAB_JUMPS.filter((jump) => jump.pattern.test(lesson.exercise || ""));

  async function save(): Promise<void> {
    if (!onSaveActivity || !answer.trim()) return;
    setSaved("saving");
    const ok = await onSaveActivity({
      unitType: "lesson",
      objective: `【游资雷达练习】${lesson.level} ${lesson.title}：${lesson.exercise || ""}`,
      userAnswer: answer.trim(),
    });
    setSaved(ok ? "saved" : "failed");
    if (ok) onSaved?.();
  }

  return (
    <li id={`youzi-lesson-${lesson.level}`}>
      <div className="youzi-lesson-head">
        <span className="youzi-badge">{lesson.level}</span>
        <strong>{lesson.title}</strong>
      </div>
      {/* U05 四段：原理 → 机制 → 练习 → 验收；缺字段就显示缺，不补写。 */}
      <p className="youzi-muted">原理：{lesson.principle || "（课程文件未提供 principle 字段）"}</p>
      <p className="youzi-muted">对应机制：{lesson.mechanism || "（课程文件未提供 mechanism 字段）"}</p>
      <p>练习：{lesson.exercise || "（课程文件未提供 exercise 字段）"}</p>
      {jumps.length > 0 && (
        <div className="youzi-chips">
          {jumps.map((jump) => (
            <button key={jump.tab} className="ghost-btn small" onClick={() => onNavigate(jump.tab)}>
              去「{jump.label}」做这一课
            </button>
          ))}
        </div>
      )}
      <div className="youzi-form column">
        <label>
          <span>我的答案（记下后切页签 / 重开应用不会丢）</span>
          <textarea
            className="youzi-textarea"
            value={answer}
            placeholder="写下你的观察。不写「明天买谁」。"
            onChange={(event) => setAnswer(event.target.value)}
          />
        </label>
        <div className="youzi-chips">
          <button className="ghost-btn small" disabled={!answer.trim() || saved === "saving"} onClick={() => void save()}>
            {saved === "saved" ? "✓ 已记下（learning_activities）" : saved === "saving" ? "保存中…" : "记下我的答案"}
          </button>
          {saved === "failed" && <span className="youzi-muted">保存失败，请重试。</span>}
        </div>
      </div>
      <p className="youzi-muted">学到什么程度算过：{lesson.acceptance || "（课程文件未提供 acceptance 字段）"}</p>
      {lesson.boundary && <p className="youzi-muted">可见性边界：{lesson.boundary}</p>}
    </li>
  );
}

/** U05：学习页改为从 Core 只读端点 /youzi/curriculum 取同一份 curriculum.json（单一数据源），
 * 前端不再手抄副本——旧 LESSONS 副本的 exercise 已有 9/11 条与 JSON 漂移（U05 实测）。
 * 页头显示 version/updated_at，让「课上的是哪一版」可核对。 */
function LearnPanel({
  client,
  onSaveActivity,
  onNavigate,
}: {
  client: CoreClient;
  onSaveActivity?: (input: SaveActivityInput) => Promise<boolean>;
  onNavigate: (tab: YouziTab) => void;
}) {
  const [curriculum, setCurriculum] = useState<CurriculumPayload | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** U06(4)：本周学习目标（GET /learning/goals/current 已有端点）+ 已落库的课文作答标记。 */
  const [goalProgress, setGoalProgress] = useState<string | null>(null);
  const [doneLessonIds, setDoneLessonIds] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const response = await client.request<{ ok: boolean; curriculum: CurriculumPayload; reason?: string }>({
        method: "GET",
        path: "/youzi/curriculum",
      });
      if (response.status < 400 && response.data?.ok) setCurriculum(response.data.curriculum);
      else setLoadError(response.data?.reason || `课程加载失败（HTTP ${response.status}）。`);
    } catch {
      setLoadError("课程加载失败，请重试。");
    } finally {
      setLoading(false);
    }
  }, [client]);

  const loadProgress = useCallback(async () => {
    try {
      const goalRes = await client.request<{ completed_count: number; target_count: number }>({ method: "GET", path: "/learning/goals/current" });
      if (goalRes.status < 400 && goalRes.data) setGoalProgress(`${goalRes.data.completed_count}/${goalRes.data.target_count}`);
      const actsRes = await client.request<Array<{ objective: string }>>({ method: "GET", path: "/learning/activities" });
      if (actsRes.status < 400 && Array.isArray(actsRes.data)) {
        const saved = new Set<string>();
        for (const item of actsRes.data) {
          const match = /^【游资雷达练习】(L\d+)/.exec(item.objective || "");
          if (match) saved.add(match[1]!);
        }
        setDoneLessonIds(saved);
      }
    } catch {
      // 学习进度取不到不阻塞课程渲染（如实缺省）。
    }
  }, [client]);

  useEffect(() => {
    void load();
    void loadProgress();
  }, [load, loadProgress]);

  if (loadError) {
    return (
      <div className="youzi-alert">
        <strong>课程未能加载</strong>
        <span>{loadError}</span>
        <p className="youzi-muted">课程由插件目录的 curriculum.json 提供；不回退到手抄副本，避免再次漂移。</p>
        <button className="ghost-btn" onClick={() => void load()}>重新加载</button>
      </div>
    );
  }
  if (!curriculum) {
    return <p className="youzi-muted">{loading ? "课程加载中…" : "课程加载中…"}</p>;
  }

  // U06(4)：「下一步学哪一课」= 第一个还没有记下答案的课（从已落库作答推导，不猜学习意图）。
  const nextLesson = curriculum.lessons.find((lesson) => !doneLessonIds.has(lesson.level));

  return (
    <>
      <p className="youzi-note">
        {curriculum.purpose || "学会读游资，不是学做游资。"}课文都对应产品里的某条机制，
        练习用当天真实榜单完成；写观察，不写「明天买谁」。
        <br />
        <span className="youzi-muted">
          课程版本 <span className="mono">{curriculum.version || "未知"}</span>
          （更新于 <span className="mono">{curriculum.updated_at || "未知"}</span>，与插件目录 curriculum.json 同源）。
        </span>
        {goalProgress && (
          <>
            <br />
            <span className="youzi-muted">
              本周学习目标完成 {goalProgress} 次；已记下答案的课：{doneLessonIds.size} / {curriculum.lessons.length}。
              {nextLesson && (
                <>
                  {" "}下一步学 <button className="ghost-btn small" onClick={() => document.getElementById(`youzi-lesson-${nextLesson.level}`)?.scrollIntoView({ behavior: "smooth" })}>{nextLesson.level} {nextLesson.title}</button>。
                </>
              )}
            </span>
          </>
        )}
      </p>
      <ol className="youzi-lessons">
        {curriculum.lessons.map((lesson) => (
          <LessonCard key={lesson.level} lesson={lesson} onSaveActivity={onSaveActivity} onNavigate={onNavigate} onSaved={() => void loadProgress()} />
        ))}
      </ol>

      <div className="youzi-detail">
        <h4>术语表（市场语言注释）</h4>
        <p className="youzi-muted">
          下列说法是<strong>市场语言注释</strong>，不是检测器、不是建议。
          <strong>标注「当前不可观测」的词，本产品不产出对应判断</strong>——
          不要把它读成「没发生」。
        </p>
        <table className="youzi-table compact">
          <thead>
            <tr>
              <th>说法</th>
              <th>通常含义</th>
              <th>证据边界</th>
            </tr>
          </thead>
          <tbody>
            {GLOSSARY.map((item) => (
              <tr key={item.term}>
                <td>
                  <strong>{item.term}</strong>
                </td>
                <td>{item.meaning}</td>
                <td>
                  {item.currentUnobservable && (
                    <span className="youzi-badge bucket">当前不可观测</span>
                  )}
                  <span className="youzi-muted"> {item.boundary}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="youzi-muted">
        「首板」「接力」「反核」「一日游」这类说法是<strong>市场语言注释</strong>，
        不是检测器、不是建议。本插件一期不做打板识别引擎。
      </p>
    </>
  );
}
