/**
 * B02/B03（个股研报生成修复路线图 2026-09-15）：个股研报生成失败的结构化呈现。
 *
 * 此前 `useResearch.generateStockReport` 把 HTTP≥400 / ok=false 统一转 null，
 * 调用方只能显示「模型调用或证据解析未通过」固定文案——后端已给出的
 * stage / detail / source_errors 全部被吞（A02 复现确认）。
 * 本模块定义失败负载类型与「原因 + 恢复动作」的统一映射，单股与批量重试共用。
 */

/** 单股研报生成失败负载（保留后端可定位信息；HTTP≥400 时 stage 为 null）。 */
export interface StockReportFailure {
  /** HTTP 状态码；Core 不可达等网络层失败由 coreClient 统一映射为 502。 */
  status: number;
  /** 后端业务失败阶段（model_call / parse / citation / evidence_unavailable / model_access_disabled…）。 */
  stage: string | null;
  /** 后端 detail 或 HTTP 错误体 detail，无则给兜底文案。 */
  detail: string;
  /** 证据取数失败的来源清单（evidence_unavailable 时列出，让用户知道缺了什么）。 */
  sourceErrors: string[];
}

/** 单股研报生成结果：成功带报告，失败带可定位负载（不再返回 null）。 */
export type StockReportResult =
  | { ok: true; report: import("./researchTypes").StockReport }
  | { ok: false; failure: StockReportFailure };

/** 失败的用户可读呈现：主文案 + 恢复动作提示 + 是否值得原样重试。 */
export interface FailurePresentation {
  message: string;
  hint: string | null;
  /** 暂时性故障（限流/超时/解析抖动）可重试；配置类错误重试无意义。 */
  canRetry: boolean;
}

/** 把失败负载映射为「用户知道发生了什么 + 下一步怎么办」。规则由代码决定，不无限自动重试。 */
export function presentStockReportFailure(failure: StockReportFailure): FailurePresentation {
  const sourceSuffix = failure.sourceErrors.length > 0
    ? `（失败来源：${failure.sourceErrors.join("；")}）`
    : "";
  switch (failure.stage) {
    case "model_access_disabled":
      return { message: failure.detail, hint: "在设置页开启模型出网（STEWARD_MODEL_ACCESS）后重试", canRetry: false };
    case "evidence_unavailable":
      return {
        message: `${failure.detail}${sourceSuffix}`,
        hint: "检查行情/新闻数据源连接后重试；若仍失败可能是数据源临时不可用",
        canRetry: true,
      };
    case "model_call":
      return {
        message: failure.detail,
        hint: "暂时性故障可重试；反复失败请检查所选模型方案的配置与额度",
        canRetry: true,
      };
    case "parse":
      return {
        message: failure.detail,
        hint: "多为模型输出格式波动，重试一次通常可恢复",
        canRetry: true,
      };
    case "citation":
      return {
        message: failure.detail,
        hint: "模型引用了本次不存在的来源；可重试或改用其他模型方案",
        canRetry: true,
      };
    default:
      break;
  }
  if (failure.status === 409) {
    return { message: failure.detail, hint: "先在设置页完成对应配置（如置一个方案为使用中）再重试", canRetry: false };
  }
  if (failure.status === 404) {
    return { message: failure.detail, hint: "所选模型方案不存在：回到「本次模型」重新选择", canRetry: false };
  }
  if (failure.status === 408 || failure.status === 504) {
    return { message: failure.detail || "Core 请求超时", hint: "生成耗时过长被中断，可重试；深研档请耐心等待", canRetry: true };
  }
  if (failure.status === 502 || failure.status === 0) {
    return {
      message: failure.detail || "无法连接本地 Core（进程未启动或端口不可达）",
      hint: "确认桌面端已启动且 Core 就绪后重试",
      canRetry: true,
    };
  }
  if (failure.status >= 400) {
    return { message: failure.detail || `Core 返回错误（HTTP ${failure.status}）`, hint: null, canRetry: false };
  }
  return { message: failure.detail || "生成失败，原因未明", hint: "可重试一次；反复失败请记录 detail 反馈", canRetry: true };
}

/** 单行错误文案（CompareEntry.error 为 string，历史结构保持不变）。 */
export function formatStockReportFailure(failure: StockReportFailure): string {
  const view = presentStockReportFailure(failure);
  return view.hint ? `${view.message}（${view.hint}）` : view.message;
}
