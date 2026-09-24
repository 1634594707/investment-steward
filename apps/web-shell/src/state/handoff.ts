/**
 * v33（2026-09-14 路线图 §6 D2/D03/D06/D09）：跨页导航交接的结构化契约。
 *
 * 此前交接只传 `string[]` / `symbol` 字符串，导致：
 * - 同一代码重复跳转不触发预填（依赖字符串变化）；
 * - 来源报告、研究问题、核验状态全部丢失；
 * - 接收方无法回执，发送方不知道是否送达。
 *
 * 现在每次导航都带唯一 `requestId`（接收方按 id 消费并回执，迟到/旧请求不覆盖新请求），
 * 并携带来源上下文（报告 id/版本、主题、研究问题），支撑「返回来源」链（D09）。
 */

/** 唯一请求 id：每次导航新生成（同股重复跳转也有效，D06）。 */
export function newRequestId(): string {
  const cryptoRef = globalThis.crypto as Crypto | undefined;
  if (cryptoRef?.randomUUID) return cryptoRef.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 工作台 → 战法雷达的候选池交接（D02/D03：只含可扫描的标准代码 + 来源上下文）。 */
export interface TacticsHandoff {
  requestId: string;
  /** 来源标签（接收面板展示）：如「方向研判 · 固态电池」。 */
  sourceLabel: string;
  /** 来源方向报告 id（返回来源链，D09；无来源时为空）。 */
  sourceReportId: string;
  /** 主题与研究问题（雷达扫描后「研究该股」回带，D08）。 */
  topic: string;
  question: string;
  /** 交接的标准代码（去重保序；不可扫描代码已由发送方排除并另行提示）。 */
  pool: string[];
  /** 发送方附注：被排除的不可扫描候选及原因（接收面板如实展示）。 */
  excluded: { symbol: string; reason: string }[];
  createdAt: string;
}

/** 战法雷达 → 研究工作台的单股研究交接（D06/D08：带信号与研究依据）。 */
export interface ReportHandoff {
  requestId: string;
  symbol: string;
  /** 预填研究问题（雷达信号上下文 / 方向候选的业务关联）。 */
  question: string;
  /** 来源标签（工作台显示交接来源，可返回）。 */
  sourceLabel: string;
  /**
   * 来源扫描 id（D09 返回链预留）。
   * D02 核对（2026-09-19）：`POST /tactics/scan` 的 `ScanResult` 与 `GET /tactics/signals/{symbol}`
   * 的响应都没有单条扫描 id；只有全市场扫描任务有 `job_id`（`useTactics` 的 `marketScanJobRef`），
   * 且它不下发到结果行，行内「AI 报告」按钮取不到。故此处保持缺省，返回落点由 `sourceView` 承担；
   * 待后端把扫描标识随行下发后再接入，不推测字段名。
   */
  scanId?: string;
  /**
   * D02（前端设计与架构优化任务路线图 2026-09-19）：发起交接时所在的视图——工作台据此提供
   * 「返回来源」操作；返回落点靠视图本身（KeepAlive 驻留），筛选/选中/滚动随页面状态恢复。
   */
  sourceView?: HandoffSourceView;
  createdAt: string;
}

/** 可发起单股研究交接、且返回需要跨视图导航的来源（D02；工作台内的方向跳单股不属跨页往返）。 */
export type HandoffSourceView = "tactics" | "youzi";
