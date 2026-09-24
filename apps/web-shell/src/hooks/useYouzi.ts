import { useCallback, useEffect, useRef, useState } from "react";
import type { CoreClient } from "../state/coreClient";
import { classifyCoreError, detailOf } from "../state/coreClient";

/**
 * P3-D03（席位证据与游资学习插件路线图）：游资雷达领域数据。
 *
 * 形态对齐 `useTactics.ts` / `useMacro.ts`：领域数据下沉到 hook，页面只消费；
 * 取数失败**如实回报**（`available=false` + `reason`），不用空数组冒充「今天没上榜」——
 * 「我没拿到」和「市场没有」对使用者是两件完全不同的事，页面必须能分辨。
 */

/** 观察名单命中（某席位在某只票上）。 */
export interface YouziWatchHit {
  operatedept_code: string;
  name: string;
  direction: string;
  net: number | null;
}

/** 今日席位：一只上榜股票。 */
export interface YouziBillboardRow {
  security_code: string;
  security_name: string;
  explanation: string;
  change_rate: number | null;
  close_price: number | null;
  billboard_buy_amt: number | null;
  billboard_sell_amt: number | null;
  billboard_net_amt: number | null;
  trade_market: string;
  watchlist_hits: YouziWatchHit[];
  /* —— G03（桌面端升级路线图 2026-09-18）：榜单扩展字段与可点回链 —— */
  /** 换手率（原口径百分比；缺失为 null，不显示 0）。 */
  turnover_rate?: number | null;
  turnover_rate_unit?: string;
  /** 自由流通市值（原口径元；缺失为 null）。 */
  free_market_cap?: number | null;
  free_market_cap_unit?: string;
  /** 原始披露页 URL（构造式，见后端 `lhb_feed.provider_page_url`；取不到为空串）。 */
  source_url?: string;
}

export interface YouziToday {
  available: boolean;
  trading_day: string;
  reason?: string;
  watchlist_version?: string;
  rows: YouziBillboardRow[];
}

/** 单票一侧的席位行。 */
export interface YouziSeatRow {
  operatedept_code: string;
  operatedept_name: string;
  buy: number | null;
  sell: number | null;
  net: number | null;
  /** 匿名汇总桶（机构专用/沪深股通/券商总部）：可展示，但不是游资主体。 */
  is_bucket: boolean;
  watchlist_hit: boolean;
  /** G03：该席位行所属的原始披露页（构造式回链；取不到为空串）。 */
  source_url?: string;
}

export interface YouziSeats {
  available: boolean;
  trading_day: string;
  security_code?: string;
  reason?: string;
  buy: YouziSeatRow[];
  sell: YouziSeatRow[];
}

/** 席位档案：某营业部近 N 日的上榜记录。 */
export interface YouziProfileRecord {
  trading_day: string;
  security_code: string;
  security_name: string;
  buy: number | null;
  sell: number | null;
  net: number | null;
  explanation: string;
}

export interface YouziProfile {
  available: boolean;
  operatedept_code?: string;
  is_watchlist?: boolean;
  watchlist_name?: string;
  reason?: string;
  records: YouziProfileRecord[];
  note?: string;
  /* —— U03（桌面端升级路线图 2026-09-18）：静默截断必须说出来 —— */
  /** 本次实际返回条数。 */
  returned?: number;
  /** 数据源单页上限（50）。 */
  page_size?: number;
  /** 取满一页 → 极可能有更早记录被截断（数据源无 total，只能如实声明「可能」）。 */
  possibly_truncated?: boolean;
}

export interface YouziWatchSeat {
  operatedept_code: string;
  name: string;
  last_seen_trade_date: string;
  record_count: number;
  note: string;
}

export interface YouziWatchlist {
  version: string;
  disclaimer: string;
  description: string;
  seats: YouziWatchSeat[];
  buckets: Array<{ name: string; note: string }>;
}

/** 指数席位（中金所会员排名，对照页）。 */
export interface YouziCffexProduct {
  product_id: string;
  spot_index_code: string;
  contract_count: number;
  total_volume: number | null;
  total_open_interest: number | null;
}

export interface YouziCffex {
  available: boolean;
  trading_day: string;
  reason?: string;
  text?: string;
  products: YouziCffexProduct[];
  basis?: Record<string, number>;
  basis_note?: string;
  limitations?: string[];
}

/** 跨日事实标签（Y2）：三类，均为「已披露路径事实」。 */
export type YouziFactType =
  | "adjacent_buy_sell"
  | "consecutive_buy"
  | "repeated_seat_presence";

/** 单条逐日证据（可回链到原始披露）。 */
export interface YouziTacticsEvidence {
  trading_day: string;
  direction: string;
  amount: number | null;
  evidence_id: string;
  explanation: string;
  /** 来源报告名（`RPT_BILLBOARD_DAILYDETAILSBUY/SELL`），回到原文的第一把钥匙。 */
  source_report: string;
  /** 来源记录 ID：内容派生哈希；与来源报告名合用才能唯一定位一行。 */
  source_record_id: string;
  /** G03：回到东财原始披露页的可点回链（构造式；取不到为空串）。 */
  source_url?: string;
  /** 金额缺失/非法时的显式原因；有效金额为 null。 */
  amount_reason: string | null;
}

/** 一条跨日事实。 */
export interface YouziTacticsFact {
  fact_type: YouziFactType | string;
  security_code: string;
  security_name: string;
  operatedept_code: string;
  operatedept_name: string;
  /** 参与交易日（升序）。UI 只展示「结束/最近出现日为 D」的标签。 */
  days: string[];
  fact_id: string;
  evidence: YouziTacticsEvidence[];
}

/** 同日同方向金额冲突（不任取一条生成对照）。 */
export interface YouziTacticsConflict {
  operatedept_code: string;
  trading_day: string;
  direction: string;
  amounts: number[];
  evidence_ids: string[];
  note: string;
}

/** 原始榜单行（降级时展示：我只回我能证实的部分）。 */
export interface YouziTacticsRawRow {
  trading_day: string;
  security_code: string;
  security_name: string;
}

export interface YouziTactics {
  request_day: string;
  /** 有效日：由可信日历判定后与 request_day 一致；不可信时为 null。 */
  effective_day: string | null;
  window_days: string[];
  /** 逐日覆盖状态：complete / truncated / fetch_failed / not_published / unknown。 */
  coverage: Record<string, string>;
  available: boolean;
  items: YouziTacticsFact[];
  /** 窗口不完整时仍回已取得的原始披露，绝不用空表冒充「没有事件」。 */
  raw_billboard: YouziTacticsRawRow[];
  reason?: string;
  conflicts: YouziTacticsConflict[];
  /** 排除计数（按原因分类）；键名见 `EXCLUDE_LABELS`。 */
  excluded: Record<string, number>;
  /** 固定边界文案，由后端下发；前端不自行改写。 */
  boundary_notice: string;
}

/** 三类事实的中文名（与后端 `tactic_annotator.FACT_*` 常量一一对应）。 */
export const FACT_LABELS: Record<string, string> = {
  adjacent_buy_sell: "相邻买卖：D 日买榜、下一交易日卖榜",
  consecutive_buy: "连续买榜",
  repeated_seat_presence: "窗口内重复出现",
};

/** 排除原因的中文名（后端 `annotate()` 的 `excluded` 键）。 */
export const EXCLUDE_LABELS: Record<string, string> = {
  identity_bucket: "匿名汇总桶（机构专用/沪深股通/券商总部）",
  identity_unknown: "席位身份不可靠或无代码",
  period_multi_day: "多日累计口径（不拆成日流水）",
  period_unknown: "统计周期无法判定",
  direction_invalid: "方向字段非法",
  amount_invalid: "金额缺失/非正/非有限",
  amount_conflict_groups: "同日同方向金额冲突",
};

/** 逐日覆盖状态的中文名。unknown 必须与 not_published 分开说。 */
export const COVERAGE_LABELS: Record<string, string> = {
  complete: "已完整取得",
  not_published: "尚未披露",
  fetch_failed: "取数失败",
  truncated: "分页截断",
  unknown: "无法判断",
};

/**
 * 只保留「结束/最近出现日为 D」的事实（Y2-10 硬要求）。
 *
 * 含义：该事实的**最后参与日**必须等于有效日 D。这样「五日前结束」的旧标签
 * 不会混进今日视图，也不会把跨窗口的陈旧事实当成当天的。
 */
export function factsEndingOn(facts: YouziTacticsFact[], day: string): YouziTacticsFact[] {
  if (!day) return facts;
  return facts.filter((fact) => fact.days.length > 0 && fact.days[fact.days.length - 1] === day);
}

/** 六个离散期限的中文名（后端 `youzi_horizon.HORIZON_DAYS`）。 */
export const HORIZON_STATUS_LABELS: Record<string, string> = {
  disclosed: "已披露",
  not_due: "尚未到期",
  missing: "已到期但数据源未给值",
  unknown: "无法判断",
};

/** 复盘事件里的一个期限格（Y3-05/06）。 */
export interface YouziHorizon {
  label: string;
  sessions: number;
  value_pct: number | null;
  status: string;
  /** 目标交易日；为空串表示按当前 as_of 还推不出（不拿未来交易日反推）。 */
  target_date: string;
  fetched_at: number;
  source: string;
  reason: string;
}

/** 复盘里的一条披露事件（= 一个交易日 × 一个席位 × 一个方向 × 一条原始原因）。 */
export interface YouziReplayEvent {
  event_id: string;
  trading_day: string;
  security_code: string;
  security_name: string;
  operatedept_code: string;
  operatedept_name: string;
  direction: string;
  amount: number | null;
  explanation: string;
  source_report: string;
  source_record_id: string;
  /** G03：回到东财原始披露页的可点回链（构造式；取不到为空串）。 */
  source_url?: string;
  fact_types: string[];
  fact_ids: string[];
  horizons: YouziHorizon[];
  /**
   * 未生成事实的显式原因：
   * - `""` → 有事实；
   * - `no_fact_for_window` → 窗口完整、检查过了没有；
   * - `insufficient_context_days` / `context_not_complete` / `window_incomplete`
   *   → **没法检查**（不是「没有」）。
   */
  fact_omitted_reason: string;
}

/** 复盘的降级原因（后端 `degraded(reason)`）。 */
export const REPLAY_REASON_LABELS: Record<string, string> = {
  calendar_missing: "可信交易日历不可用，无法确定区间内的交易日。",
  no_trading_day_in_range: "该日期区间内没有交易日（可能是长假）。",
};

/** 复盘的事实省略原因（前端只做文案映射，不改语义）。 */
export const OMITTED_LABELS: Record<string, string> = {
  no_fact_for_window: "窗口完整，未命中三类路径事实",
  insufficient_context_days: "五交易日上下文不足，未做判断",
  context_not_complete: "上下文覆盖不完整，未做判断",
  window_incomplete: "窗口不完整，未做判断",
};

export interface YouziReplayPage {
  security_code: string;
  range_start: string;
  range_end: string;
  events: YouziReplayEvent[];
  cursor: string;
  has_more: boolean;
  total_events: number;
  coverage: Record<string, string>;
  truncated: boolean;
  truncated_reason: string;
  fetched_at: number;
  limitations: string[];
  available: boolean;
  reason?: string;
  boundary_notice: string;
  event_day_count?: number;
  context_days?: string[];
}

/** 复盘查询条件（Y3-08：代码/日期切换要能取消或忽略在途响应）。 */
export interface YouziReplayQuery {
  securityCode: string;
  start: string;
  end: string;
  asOf: string;
  pageSize: number;
}

export type YouziTab =
  | "today"
  | "tactics"
  | "replay"
  | "practice"
  | "profile"
  | "cross"
  | "cffex"
  | "learn";

/** P4-E01：持仓/自选与今日上榜的交叉提示。 */
export interface YouziCrossHit {
  security_code: string;
  security_name: string;
  labels: string[];
  statuses: string[];
  explanation: string;
  billboard_net_amt: number | null;
  watchlist_hit: boolean;
  /** G03：该上榜事实的原始披露页（构造式回链；取不到为空串）。 */
  source_url?: string;
}

export interface YouziCrossCheck {
  available: boolean;
  trading_day: string;
  checked: number;
  reason?: string;
  hits: YouziCrossHit[];
  note?: string;
}

export function useYouzi(client: CoreClient, active: boolean) {
  const [tab, setTab] = useState<YouziTab>("today");
  const [today, setToday] = useState<YouziToday | null>(null);
  const [seats, setSeats] = useState<YouziSeats | null>(null);
  const [profile, setProfile] = useState<YouziProfile | null>(null);
  const [watchlist, setWatchlist] = useState<YouziWatchlist | null>(null);
  const [cffex, setCffex] = useState<YouziCffex | null>(null);
  const [cross, setCross] = useState<YouziCrossCheck | null>(null);
  const [tactics, setTactics] = useState<YouziTactics | null>(null);
  /** 事实页签选中的交易日（YYYYMMDD）。null = 跟随后端 today 的有效日。 */
  const [tacticsDay, setTacticsDay] = useState<string | null>(null);
  /** Y3-07：复盘页签的一页结果（当前游标位置）。 */
  const [replay, setReplay] = useState<YouziReplayPage | null>(null);
  /** Y3-07：累计页（「加载更多」把后续页追加进来，保持时间线连续）。 */
  const [replayEvents, setReplayEvents] = useState<YouziReplayEvent[]>([]);
  const [replayQuery, setReplayQuery] = useState<YouziReplayQuery | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 当前选中的上榜股票（点进「今日席位」某一行后看买卖席位）。 */
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  /** U04：今日席位页签选中的交易日（YYYYMMDD）；null = 最新有效日。 */
  const [todayDay, setTodayDayState] = useState<string | null>(null);

  /** U04：day 显式传参优先于 todayDay 状态（点「查看该日榜单」时 setState 与请求同帧发起，
   * 依赖状态会拿到旧值）。undefined = 跟随当前选择；null = 最新有效日。 */
  const loadToday = useCallback(async (day?: string | null) => {
    const target = day === undefined ? todayDay : day;
    setLoading(true);
    setError(null);
    try {
      // 后端按可信交易日历校验 trading_day（不传 = 最新有效日），前端不拿自然日当交易日。
      const path = (target ? `/youzi/today?trading_day=${encodeURIComponent(target)}` : "/youzi/today") as `/${string}`;
      const response = await client.request<YouziToday>({ method: "GET", path });
      if (response.status < 400 && response.data) setToday(response.data);
      else setError(classifyCoreError(response.status, detailOf(response.data)).message);
    } catch {
      setError("今日席位请求失败，请重试。");
    } finally {
      setLoading(false);
    }
  }, [client, todayDay]);

  /** U04：切到指定交易日（YYYYMMDD 或 null=最新）。切换时作废既有席位明细（属于旧的一天）。 */
  const setTodayDay = useCallback((day: string | null) => {
    setTodayDayState(day);
    setSeats(null);
    setSelectedCode(null);
    setToday(null); // 立即清空旧榜单，避免旧日数据在新标题下短暂可见。
  }, []);

  const loadWatchlist = useCallback(async () => {
    try {
      const response = await client.request<YouziWatchlist>({ method: "GET", path: "/youzi/watchlist" });
      if (response.status < 400 && response.data) setWatchlist(response.data);
    } catch {
      // 名单取不到不阻塞主流程：页面会在相关位置显示「名单未加载」。
    }
  }, [client]);

  const loadSeats = useCallback(
    async (securityCode: string) => {
      // U01（桌面端升级路线图 2026-09-18）：不发空代码请求（旧「刷新」在 selectedCode 为空时会打一次空请求）。
      if (!securityCode) return;
      setSelectedCode(securityCode);
      setLoading(true);
      try {
        const response = await client.request<YouziSeats>({
          method: "GET",
          path: `/youzi/seats/${encodeURIComponent(securityCode)}`,
        });
        if (response.status < 400 && response.data) setSeats(response.data);
        else setError(classifyCoreError(response.status, detailOf(response.data)).message);
      } catch {
        setError("席位明细请求失败，请重试。");
      } finally {
        setLoading(false);
      }
    },
    [client],
  );

  /** U01：收起席位明细（旧实现 setSeats(null) 全仓 0 命中，明细出来后关不掉）。 */
  const closeSeats = useCallback(() => {
    setSeats(null);
    setSelectedCode(null);
  }, []);

  const loadProfile = useCallback(
    async (operatedeptCode: string) => {
      setLoading(true);
      try {
        const response = await client.request<YouziProfile>({
          method: "GET",
          path: `/youzi/profile/${encodeURIComponent(operatedeptCode)}`,
        });
        if (response.status < 400 && response.data) setProfile(response.data);
        else setError(classifyCoreError(response.status, detailOf(response.data)).message);
      } catch {
        setError("席位档案请求失败，请重试。");
      } finally {
        setLoading(false);
      }
    },
    [client],
  );

  const loadCffex = useCallback(async () => {
    setLoading(true);
    try {
      const response = await client.request<YouziCffex>({ method: "GET", path: "/youzi/cffex" });
      if (response.status < 400 && response.data) setCffex(response.data);
      else setError(classifyCoreError(response.status, detailOf(response.data)).message);
    } catch {
      setError("指数席位请求失败，请重试。");
    } finally {
      setLoading(false);
    }
  }, [client]);

  const loadCrossCheck = useCallback(async () => {
    setLoading(true);
    try {
      const response = await client.request<YouziCrossCheck>({ method: "GET", path: "/youzi/cross-check" });
      if (response.status < 400 && response.data) setCross(response.data);
      else setError(classifyCoreError(response.status, detailOf(response.data)).message);
    } catch {
      setError("交叉提示请求失败，请重试。");
    } finally {
      setLoading(false);
    }
  }, [client]);

  /**
   * Y2-10：跨日事实（独立于 `/youzi/today` 请求，页面上再合并展示）。
   *
   * `day` 缺省时由后端按可信日历定有效日（不传自然日、不猜）。
   *
   * ★ 日期切换忽略旧响应：用户连点两天时，先发出的请求可能后返回。这里用单调
   * 递增的序号守卫——只有「最后一次发起」的响应允许写入 state，避免 A 日结果
   * 覆盖 B 日（路线图 Y2-10 / Y3-08 的同一要求）。
   */
  const tacticsSeq = useRef(0);
  const loadTactics = useCallback(
    async (day?: string | null) => {
      const target = day ?? null;
      const seq = (tacticsSeq.current += 1);
      setTacticsDay(target);
      setLoading(true);
      setError(null);
      try {
        const path = (target ? `/youzi/tactics/${encodeURIComponent(target)}` : "/youzi/tactics") as `/${string}`;
        const response = await client.request<YouziTactics>({ method: "GET", path });
        if (seq !== tacticsSeq.current) return; // 已被更新的请求取代，丢弃。
        if (response.status < 400 && response.data) setTactics(response.data);
        else setError(classifyCoreError(response.status, detailOf(response.data)).message);
      } catch {
        if (seq !== tacticsSeq.current) return;
        setError("跨日事实请求失败，请重试。");
      } finally {
        if (seq === tacticsSeq.current) setLoading(false);
      }
    },
    [client],
  );

  /**
   * Y3-07 复盘：按代码 + 区间查询，**代码输入**而非推荐列表。
   *
   * ★ Y3-08 请求生命周期（三条硬要求）：
   *  1. **序号守卫**：换代码/换区间/翻页时先发出的请求可能后返回。只有「最后一次
   *     发起」的响应允许写入 state——否则 A 股票的结果会覆盖 B 股票。
   *  2. **游标只接受当前查询的**：后端游标绑定 `(证券, 区间, 快照)`，换任一项后旧
   *     游标会被拒；前端因此把游标与它所属的查询一起替换，不复用旧游标。
   *  3. **重试是手动的**：失败只提示，不自动重试——避免在坏参数下无限循环。
   */
  const replaySeq = useRef(0);
  const loadReplay = useCallback(
    async (query: YouziReplayQuery, cursor = "", append = false) => {
      const seq = (replaySeq.current += 1);
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          start: query.start,
          end: query.end,
          as_of: query.asOf,
          page_size: String(query.pageSize),
        });
        if (cursor) params.set("cursor", cursor);
        const path =
          `/youzi/replay/${encodeURIComponent(query.securityCode)}?${params.toString()}` as `/${string}`;
        const response = await client.request<YouziReplayPage>({ method: "GET", path });
        if (seq !== replaySeq.current) return; // 已被更新的请求取代，丢弃。
        if (response.status < 400 && response.data) {
          const page = response.data;
          setReplay(page);
          setReplayQuery(query);
          setReplayEvents((previous) => (append ? [...previous, ...page.events] : page.events));
        } else if (response.status === 422) {
          // 参数问题要说清是哪一项，否则用户只会看到「失败了」。
          setError(detailOf(response.data) || "复盘参数不合法，请检查代码与日期区间。");
        } else {
          setError(classifyCoreError(response.status, detailOf(response.data)).message);
        }
      } catch {
        if (seq !== replaySeq.current) return;
        setError("复盘请求失败，请重试。");
      } finally {
        if (seq === replaySeq.current) setLoading(false);
      }
    },
    [client],
  );

  /** 切换到另一个代码/区间时**清空**累计页，避免两段结果混在一起。 */
  const resetReplay = useCallback(() => {
    replaySeq.current += 1; // 作废所有在途响应。
    setReplay(null);
    setReplayEvents([]);
    setReplayQuery(null);
  }, []);

  // 页面激活时才取数（KeepAlive 常驻，避免切页重复请求）。
  useEffect(() => {
    if (!active) return;
    if (today === null) void loadToday();
    if (watchlist === null) void loadWatchlist();
  }, [active, today, watchlist, loadToday, loadWatchlist]);
  useEffect(() => {
    if (!active) return;
    if (tab === "cffex" && cffex === null) void loadCffex();
    if (tab === "cross" && cross === null) void loadCrossCheck();
  }, [active, tab, cffex, cross, loadCffex, loadCrossCheck]);

  // Y2-10：事实页签首次进入才请求；有效日未显式指定时后端自行判定。
  useEffect(() => {
    if (!active) return;
    if (tab === "tactics" && tactics === null && !loading) void loadTactics(tacticsDay);
  }, [active, tab, tactics, tacticsDay, loading, loadTactics]);

  return {
    tab,
    setTab,
    today,
    todayDay,
    setTodayDay,
    seats,
    profile,
    watchlist,
    cffex,
    cross,
    tactics,
    tacticsDay,
    replay,
    replayEvents,
    replayQuery,
    loading,
    error,
    selectedCode,
    setSelectedCode,
    loadToday,
    loadSeats,
    closeSeats,
    loadProfile,
    loadCffex,
    loadCrossCheck,
    loadTactics,
    loadReplay,
    resetReplay,
    setProfile,
  };
}
