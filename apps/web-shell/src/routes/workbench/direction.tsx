/**
 * v33（2026-09-14 路线图 A06/A17/B12/D02）：方向研判阅读区与候选股表。
 * - 报告页签：概览 / 产业分析 / 候选股 / 证据与风险（导航先于长内容，吸顶）；
 * - 候选股表格（A17）：选择框、名称/代码、产业链环节、入选依据、核验状态、操作——
 *   替代只有 tooltip 的标签；主要操作「送已选到雷达 / 送筛选结果到雷达 / 研究该股」，
 *   选择与发送范围分离（D02），数量常驻可见；
 * - 证券身份（B09）与三种资格（B10）按服务端核验结果如实展示，不猜测。
 */
import { useMemo, useState, type ReactNode } from "react";
import type { DirectionReport, DirectionPoolCandidate } from "./researchTypes";
import { MarkdownishText, QUALITY_STATUS_TONE } from "./panels";
// M1-B04：候选身份判定与代码列口径收敛到 format.ts，与 Markdown 导出共用。
import { legacyIdentityStatus, poolCodeText, poolCodeHint } from "./format";

/** 方向篇幅模式 → 中文标签（与后端 DIRECTION_MODES 同源）。 */
export const DIRECTION_MODE_LABEL: Record<string, string> = {
  quick: "快速概览",
  standard: "标准研判",
  deep: "深研",
};

/** B01（2026-09-15 方向研判路线图）两种诚实输出状态 → 展示元数据。 */
export const RESEARCH_MODE_META: Record<string, { label: string; tone: string; hint: string }> = {
  evidence: { label: "证据研判", tone: "mint", hint: "基于本次证据快照生成（证据编号 [E#] 可回链）" },
  knowledge: { label: "知识概览", tone: "warn", hint: "无可用证据，基于模型既有知识——时效性有限，见数据缺口" },
};

/** M3-D01：报告形态 → 展示元数据（与后端 DIRECTION_FORM_LABELS 同源）。 */
export const DIRECTION_FORM_META: Record<string, { label: string; tone: string; hint: string }> = {
  full: { label: "完整研判", tone: "mint", hint: "证据充分：十节齐备、结论覆盖证据命中的行业" },
  partial: { label: "局部研判", tone: "blue", hint: "部分证据可用：只对有证据的行业下结论，未覆盖部分在缺口集中说明" },
  stage: { label: "阶段性研究", tone: "warn", hint: "关键数据缺失：短篇阶段性研究 + 补证动作，补齐后可升级为正式研判" },
};

/** M3-D02：研究优先级 → 色调（高/中/低）。 */
export const COMPARISON_PRIORITY_TONE: Record<string, string> = {
  高: "mint",
  中: "blue",
  低: "gray",
};

/**
 * v35 A01：主题类型 → 徽章文案与色调（与后端 THEME_KIND_LABELS 同源）。
 * 产业主题走 v34 板块行情层；风格/复合主题接入 v35 板块估值横截面层。
 */
export const THEME_KIND_META: Record<string, { label: string; tone: string; hint: string }> = {
  industry: { label: "产业主题", tone: "blue", hint: "命中行业板块：证据来自当日板块行情快照（v34 板块行情层）" },
  style: { label: "风格主题", tone: "mint", hint: "筛选条件类主题：证据来自全市场估值横截面与低估度榜单（v35 估值证据层）" },
  composite: { label: "复合主题", tone: "mint", hint: "产业限定 + 风格筛选：估值横截面只在限定板块内聚合，与板块行情快照并存" },
  generic: { label: "通用主题", tone: "gray", hint: "未命中产业板块与风格词：沿用缺口声明，不硬凑证据" },
};

/** v35 A02：风格词口径提示（输入框下方按主题类型展示可用证据层）。 */
export const THEME_INPUT_HINT: Record<string, string> = {
  industry: "产业/主题关键词：命中行业板块时注入当日板块行情快照；有可用证据则生成「证据研判」。",
  style: "风格/筛选类主题（如「低估板块」）：接入全市场估值横截面与低估度榜单，报告须引用 [E#] 证据编号。",
  composite: "复合主题（如「低估的银行」）：产业限定 + 风格筛选，估值横截面只在限定板块内聚合。",
  generic: "未命中产业板块与风格词：将如实声明数据缺口，不硬凑证据。",
};

/** 输入框占位符：按主题类型给出可用的证据层提示。 */
export const THEME_TOPIC_PLACEHOLDER = "如「AI 芯片」「低估板块」「低估的银行」";

/** 判断分层 → 展示元数据（B05：产业景气/竞争力/资金情绪/技术信号分开陈述）。 */
export const JUDGMENT_KIND_TONE: Record<string, string> = {
  fact: "blue",
  industry: "gray",
  competitiveness: "blue",
  sentiment: "warn",
  technical: "gray",
};

/**
 * v36 A02：四维低估判定 → 五类分类标签的展示元数据（与后端 `VALUATION_JUDGMENT_LABELS` 同源）。
 * 色调选择依据：低估候选 = 唯一的「允许提名」象限 → mint（与「有支撑」同色，表示可推进）；
 * 深跌未反转 = 便宜但基本面塌陷，需警示 → coral；高位 = 价格已在高处，同需警示 → coral；
 * 盈利周期顶 = 盈利端见顶、价格未跌透 → warn；待定 = 缺维兜底、无结论 → gray。
 * 标签是**研究分类**，不是买卖指令；模型可推翻，但须在正文给出数值论证。
 */
export const VALUATION_JUDGMENT_META: Record<string, { label: string; tone: string; hint: string }> = {
  "低估候选": { label: "低估候选", tone: "mint", hint: "PB 分位 ≤ 40% 且亏损面 ≤ 30% 且无 ROE 塌陷背离且非高位——候选池允许提名的唯一象限" },
  "深跌未反转": { label: "深跌未反转", tone: "coral", hint: "PB 分位 ≤ 40%，但亏损面 ≥ 40% 或 ROE 塌陷背离 ≥ 40pp——便宜却尚未反转，不入候选池" },
  "高位": { label: "高位", tone: "coral", hint: "代表股 PB 分位中位数 ≥ 70%——相对自身历史已处高位，不入候选池" },
  "盈利周期顶": { label: "盈利周期顶", tone: "warn", hint: "PE 分位 ≤ 10% 且 PB 分位 ≥ 30%——盈利端见顶而价格未同步跌透，不入候选池" },
  "待定": { label: "待定", tone: "gray", hint: "四维数据不全或落入空档，不下结论（缺哪维见证据条目依据文本）" },
};

/** v36 A02：判定顺序常量（与后端 `VALUATION_JUDGMENT_ORDER` 同源，用于分类总表排序）。 */
export const VALUATION_JUDGMENT_ORDER: readonly string[] = [
  "高位",
  "盈利周期顶",
  "深跌未反转",
  "低估候选",
  "待定",
];

/** v36 A02：非「低估候选」象限（提名需正文推翻论证）的分类集合。 */
export const NON_LOW_JUDGMENT_LABELS: readonly string[] = ["深跌未反转", "高位", "盈利周期顶"];

/**
 * v37：分类标签 → 表格行底色（颜色标注）。用户反馈「容易抓不住重点」——
 * 五个分类在总表与候选池里用左侧色条 + 行底色区分：
 * 低估候选（唯一可提名象限）= mint；深跌未反转 / 高位 = coral；盈利周期顶 = warn；待定 = gray。
 * 色调与 `VALUATION_JUDGMENT_META` 同源，不在此另立一套。
 */
export const JUDGMENT_ROW_CLASS: Record<string, string> = {
  "低估候选": "is-low-candidate",
  "深跌未反转": "is-deep-fall",
  "高位": "is-high",
  "盈利周期顶": "is-cycle-top",
  "待定": "is-pending",
};

/**
 * v37：三问速览（概览页首屏「结论优先」卡片）。
 *
 * 用户反馈原报告「全篇约 4000 字连续长文，分类总表埋在第 5 节，核心判断在第 10 节之后」。
 * 本卡片把**服务端已算好的分类归属**按三问直接铺开，首屏即可看到
 * 「哪些满足低估 / 哪些深跌未反转 / 哪些已处高位」，不必先读正文。
 *
 * 数据来源全部是既有字段（`board_judgments` + 证据快照的 `valuation_judgment`），
 * **不在前端重算分类**（避免两端口径漂移）；旧报告无字段 → 整块不渲染。
 */
export function DirectionAnswerFirst({ report }: { report: DirectionReport }) {
  const judgments = report.board_judgments ?? {};
  const names = Object.keys(judgments);
  if (names.length === 0) return null;
  const buckets: { key: string; label: string; hint: string; labels: string[] }[] = [
    {
      key: "low",
      label: "① 满足低估判定（可提名）",
      hint: "PB 分位 ≤ 40% 且亏损面 ≤ 30% 且无 ROE 塌陷背离且非高位——候选池只应从这一象限提名",
      labels: ["低估候选"],
    },
    {
      key: "deep",
      label: "② 只是跌得久、尚未反转",
      hint: "便宜（低分位）但基本面未反转：亏损面 ≥ 40% 或 ROE 塌陷背离 ≥ 40pp",
      labels: ["深跌未反转"],
    },
    {
      key: "high",
      label: "③ 已处高位",
      hint: "代表股 PB 分位中位数 ≥ 70%（高位）或 PE 分位 ≤ 10% 且 PB 分位 ≥ 30%（盈利周期顶）",
      labels: ["高位", "盈利周期顶"],
    },
  ];
  const grouped = buckets.map((bucket) => ({
    ...bucket,
    members: names
      .filter((name) => bucket.labels.includes(judgments[name] ?? ""))
      .sort((a, b) => a.localeCompare(b)),
  }));
  const pending = names.filter((name) => (judgments[name] ?? "") === "待定").sort((a, b) => a.localeCompare(b));
  const lowEmpty = grouped[0]!.members.length === 0;
  return (
    <div className="wb-direction-answer-first">
      {grouped.map((bucket) => (
        <div key={bucket.key} className={`wb-answer-first-row ${bucket.key}`} title={bucket.hint}>
          <span className="wb-answer-first-q">{bucket.label}</span>
          <span className="wb-answer-first-a">
            {bucket.members.length === 0 ? (
              <span className="wb-answer-first-none">
                {bucket.key === "low" ? "本期无——原报告须如实说明并给出最接近的板块与差距" : "无"}
              </span>
            ) : (
              bucket.members.map((name) => (
                <span key={name} className={`soft-tag ${VALUATION_JUDGMENT_META[judgments[name] ?? ""]?.tone ?? "gray"}`}>
                  {name}
                  <small> · {judgments[name]}</small>
                </span>
              ))
            )}
          </span>
        </div>
      ))}
      {pending.length > 0 && (
        <div className="wb-answer-first-row pending" title="四维数据不全或落入空档，不下结论">
          <span className="wb-answer-first-q">④ 待定（数据不全/空档）</span>
          <span className="wb-answer-first-a">
            {pending.map((name) => <span key={name} className="soft-tag gray">{name}</span>)}
          </span>
        </div>
      )}
      {lowEmpty && (
        <p className="report-meta">
          「本期无低估候选」是合法结论；按 C01 口径，正文须给出最接近低估候选的板块及其逐维差距，
          禁止把「深跌未反转」或「高位」板块包装成低估候选。
        </p>
      )}
    </div>
  );
}

/** v37：报告正文分节折叠（每节默认折叠，标题行常驻可扫读）。
 *  T03：折叠行只在 1–2 级标题处断开，`###` 及更深留给 MarkdownishText 渲染，保住子层级。 */
const FOLD_HEADING = /^#{1,2}\s+(.*)$/;

interface FoldSection {
  name: string;
  body: string;
  chars: number;
  preview: string;
  /** 该小节里「表述了判断但正文找不到引用」的条数；>0 时在行上标出。 */
  unsupported: number;
  missing: boolean;
}

function firstPreview(body: string): string {
  for (const raw of body.split("\n")) {
    const line = raw.replace(/^[-*]\s+/, "").replace(/^#{1,6}\s+/, "").trim();
    if (line.length > 0) return line.length > 46 ? `${line.slice(0, 46)}…` : line;
  }
  return "";
}

export function DirectionFoldedReport({ report }: { report: DirectionReport }) {
  const content = report.report || report.direction_summary || "";
  const sections = useMemo<FoldSection[]>(() => {
    const states = report.section_states ?? [];
    const claims = report.unsupported_judgment_claims ?? [];
    const charsOf = (name: string, body: string) => states.find((s) => s.section === name)?.chars ?? body.replace(/\s/g, "").length;
    const unsupportedOf = (name: string) => claims.filter((c) => c.section === name).length;

    const out: FoldSection[] = [];
    const leading: string[] = [];
    let current: { name: string; body: string[] } | null = null;
    for (const line of content.split("\n")) {
      const heading = line.match(FOLD_HEADING);
      if (heading) {
        if (current) out.push(toSection(current.name, current.body.join("\n").trim()));
        current = { name: (heading[1] ?? "").trim(), body: [] };
        continue;
      }
      if (current) current.body.push(line);
      else leading.push(line);
    }
    if (current) out.push(toSection(current.name, current.body.join("\n").trim()));

    function toSection(name: string, body: string): FoldSection {
      return { name, body, chars: charsOf(name, body), preview: firstPreview(body), unsupported: unsupportedOf(name), missing: false };
    }

    // 首个标题之前的内容（反方审查撤下块等）过去被逐行切成多个「正文」，这里并成一节、沿用其自带的小标题。
    const leadText = leading.join("\n").trim();
    if (leadText) {
      const head = (leadText.split("\n")[0] ?? "").replace(/^【\s*/, "").replace(/\s*】$/, "").trim();
      const name = head.slice(0, 24) || "补充材料";
      out.unshift({ name, body: leadText, chars: charsOf(name, leadText), preview: firstPreview(leadText), unsupported: unsupportedOf(name), missing: false });
    }

    // 必需小节没写时，报告里根本没有对应标题 → 列表看不出「少了一节」。补成显式缺失行。
    const present = new Set(out.map((s) => s.name));
    for (const state of states) {
      if (state.present || present.has(state.section)) continue;
      out.push({ name: state.section, body: "", chars: 0, preview: "", unsupported: 0, missing: true });
    }
    return out;
  }, [content, report.section_states, report.unsupported_judgment_claims]);

  if (sections.length === 0) {
    return <MarkdownishText content={content} />;
  }
  return (
    <div className="wb-direction-folded-report">
      {sections.map((section, index) => (
        <details key={`${section.name}-${index}`} className={`wb-fold-section ${section.missing ? "is-missing" : ""}`} open={index === 0 && !section.missing}>
          <summary>
            <span className="wb-fold-index">{String(index + 1).padStart(2, "0")}</span>
            <span className="wb-fold-name">{section.name}</span>
            {section.missing ? (
              <span className="wb-fold-flag warn">未写</span>
            ) : section.unsupported > 0 ? (
              <span className="wb-fold-flag">{section.unsupported} 处判断无引用</span>
            ) : null}
            <span className="wb-fold-hint">{section.missing ? "—" : `${section.chars} 字`}</span>
            {!section.missing && section.preview && <span className="wb-fold-preview">{section.preview}</span>}
          </summary>
          {!section.missing && (
            <div className="wb-fold-body">
              <MarkdownishText content={section.body} />
            </div>
          )}
        </details>
      ))}
    </div>
  );
}

/**
 * v36 A02/D02：板块四维分类徽章。
 * 旧报告（v36 之前生成）无分类字段 → 返回 null，不猜测分类。
 */
export function BoardValuationJudgmentTag({
  judgment,
  rationale,
}: {
  judgment?: string;
  rationale?: string;
}) {
  if (!judgment) return null;
  const meta = VALUATION_JUDGMENT_META[judgment];
  const tone = meta?.tone ?? "gray";
  const hint = rationale ? `${meta?.hint ?? ""}\n判定依据：${rationale}` : meta?.hint ?? "";
  return (
    <span className={`soft-tag ${tone}`} title={hint} data-judgment={judgment}>
      {meta?.label ?? judgment}
    </span>
  );
}

/** v36 C03：分类判定引用闸门三类 reason → 中文说明（与后端 `judgment_refs_cover_boards` 同源）。 */
const JUDGMENT_GATE_REASON: Record<string, string> = {
  no_refs: "该小节出现分类标签但没有任何 [E#] 引用",
  no_board_ref: "该小节的 [E#] 引用都不属于板块估值证据（引用了非估值证据）",
  wrong_ref: "该小节点名了板块，但未引用该板块的估值证据条目",
};

/** 反方审查影响 → 文案与色调。 */
const CC_EFFECT_META: Record<string, { label: string; tone: string }> = {
  weakens: { label: "削弱", tone: "coral" },
  supports: { label: "支持", tone: "mint" },
  cannot_judge: { label: "无法判断", tone: "warn" },
};

export type DirectionTabKey = "overview" | "analysis" | "candidates" | "evidence" | "followup";

export const DIRECTION_TAB_META: Record<DirectionTabKey, { label: string; hint: string }> = {
  overview: { label: "概览", hint: "结论摘要 / 研究模式 / 下一验证动作 / 质量状态" },
  analysis: { label: "产业分析", hint: "十小节研判正文（研究问题 → 下一验证动作）" },
  candidates: { label: "候选股", hint: "可验证的候选公司表：身份核验 / 入选解释 / 送雷达 / 研究该股" },
  evidence: { label: "证据与风险", hint: "证据快照 / 反方审查 / 核心判断分层 / 催化与风险 / 数据缺口" },
  followup: { label: "追问复盘", hint: "围绕本方向研判追问：原报告不可变，结果保存为分析附录" },
};

/** v33 D09：旧记录的证券身份判定已收敛到 format.ts（`legacyIdentityStatus`），
 * 与 Markdown 导出共用同一套前缀规则，避免两处口径漂移。 */

/** 证券身份标签（B09：新记录用服务端核验结果；旧记录用前端重新校验）。 */
export function IdentityTag({ candidate }: { candidate: DirectionPoolCandidate }) {
  const status = legacyIdentityStatus(candidate);
  const label = candidate.identity_status_label || (status === "verified" ? "身份已核验" : status === "invalid" ? "无效代码" : "待核验");
  const tone = status === "verified" ? "mint" : status === "invalid" ? "coral" : "warn";
  const identity = candidate.identity;
  const hint = status === "verified"
    ? `标准代码 ${identity?.key || candidate.symbol}（${identity?.market === "sh" ? "上交所" : identity?.market === "sz" ? "深交所" : identity?.market === "bj" ? "北交所" : "市场未知"}）`
    : status === "invalid"
      ? "未能通过证券主数据校验（非 A 股股票代码或无法解析）——不可送雷达扫描"
      : "格式合法但主数据未接入，需人工核对";
  return <span className={`soft-tag ${tone}`} title={hint}>{label}</span>;
}

/**
 * v35 A01：主题类型徽章（产业/风格/复合/通用）。
 * 旧报告（v33/v34 生成）无 `theme_kind` 字段 → 整块不渲染，不猜测类型。
 */
export function ThemeKindTag({ report }: { report: DirectionReport }) {
  const kind = report.theme_kind;
  if (!kind) return null;
  const meta = THEME_KIND_META[kind] ?? THEME_KIND_META.generic!;
  const terms = report.style_terms ?? [];
  const hint = terms.length > 0 ? `${meta.hint}（命中风格词：${terms.join("、")}）` : meta.hint;
  return <span className={`soft-tag ${meta.tone}`} title={hint}>{report.theme_kind_label || meta.label}</span>;
}

/** v35 B05：候选池估值回链状态 → 展示元数据。 */
const POOL_VALUATION_TONE: Record<string, string> = {
  attached: "mint",
  failed: "warn",
  cooldown: "warn",
  unavailable: "warn",
  not_applicable: "gray",
};

/**
 * v35 B05：候选池估值列——展示回链的当前值 + 历史分位 + 同业位次。
 * 取数失败/冷却的候选保留并如实标注「估值证据缺失」（不静默丢字段）。
 */
export function PoolValuationCell({ candidate }: { candidate: DirectionPoolCandidate }) {
  const status = candidate.valuation_status ?? "";
  const valuation = candidate.valuation;
  if (!valuation) {
    if (!status) return <span className="wb-pool-valuation-empty">—</span>;
    const tone = POOL_VALUATION_TONE[status] ?? "gray";
    return (
      <span className={`soft-tag ${tone}`} title={candidate.valuation_error || candidate.valuation_status_label || ""}>
        {candidate.valuation_status_label || "估值证据缺失"}
      </span>
    );
  }
  const percentile = valuation.pb_mrq_percentile ?? valuation.pe_ttm_percentile;
  const rank = valuation.peer_rank?.pb_mrq ?? valuation.peer_rank?.pe_ttm;
  const rankBasis = valuation.peer_rank_basis?.pb_mrq ?? valuation.peer_rank_basis?.pe_ttm;
  return (
    <div className="wb-pool-valuation" title={valuation.summary || ""}>
      <span className="wb-pool-valuation-values">
        {valuation.pb_mrq != null && <span>PB {valuation.pb_mrq.toFixed(2)}</span>}
        {valuation.pe_ttm != null && <span>PE {valuation.pe_ttm.toFixed(1)}</span>}
      </span>
      {percentile != null && (
        <small className="wb-pool-valuation-meta">
          分位 {percentile.toFixed(1)}%{valuation.percentile_window_bars ? `（${valuation.percentile_window_bars} 日）` : ""}
        </small>
      )}
      {rank != null && (
        <small className="wb-pool-valuation-meta">
          同业位次 {rank}{rankBasis ? ` / ${rankBasis}` : ""}
        </small>
      )}
      {candidate.valuation_ref && (
        <small className="wb-pool-valuation-ref" title="估值证据来源定位（可回链）">{candidate.valuation_ref}</small>
      )}
    </div>
  );
}

/**
 * M3-D02：前置行业比较表——「先研究谁、为什么、等待什么变化」。
 * 旧报告（无 `industry_comparison`）整块不渲染，不猜测内容。
 */
export function DirectionIndustryComparison({ report }: { report: DirectionReport }) {
  const rows = report.industry_comparison ?? [];
  if (rows.length === 0) return null;
  return (
    <div className="wb-direction-comparison" data-testid="direction-industry-comparison">
      <span className="answer-block-title">行业比较表（先研究谁 · 依据 · 等待什么变化）</span>
      <div className="wb-pool-scroll">
        <table className="wb-judgement-table">
          <thead>
            <tr>
              <th>细分行业</th>
              <th>估值证据</th>
              <th>盈利变化</th>
              <th>催化与期限</th>
              <th>反证</th>
              <th>研究优先级</th>
              <th>优先级依据</th>
              <th>证据完整度</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={`${row.industry}-${i}`}>
                <td><b>{row.industry}</b></td>
                <td>{row.valuation_evidence || "不可得"}</td>
                <td>{row.profit_change || "不可得"}</td>
                <td>{[row.catalyst, row.horizon].filter(Boolean).join(" · ") || "不可得"}</td>
                <td>{row.counter_evidence || "不可得"}</td>
                <td>
                  {row.research_priority
                    ? <span className={`soft-tag ${COMPARISON_PRIORITY_TONE[row.research_priority] ?? "gray"}`}>{row.research_priority}</span>
                    : <span className="wb-pool-valuation-empty" title="模型未给出优先级">未给</span>}
                </td>
                <td>{row.priority_basis || <span className="coral">缺依据（须补，不得编造评分）</span>}</td>
                <td>{row.evidence_completeness || "不可得"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** M3-D03/D04/D07/D08：确定性检查的复核块（跨层因果 / 周期正常化 / 引用对应 / 缺口重复）。 */
export function DirectionQualityReview({ report }: { report: DirectionReport }) {
  const crossLayer = report.cross_layer_claims ?? [];
  const cycleGaps = report.cycle_normalization_gaps ?? [];
  const refProblems = report.reference_support_problems ?? [];
  const repeatedGaps = report.repeated_gaps ?? [];
  const mislabeled = report.research_pool_mislabeled ?? [];
  const total = crossLayer.length + cycleGaps.length + refProblems.length + repeatedGaps.length + mislabeled.length;
  if (total === 0) return null;
  return (
    <details className="wb-quality-warn" open data-testid="direction-quality-review">
      <summary>复核项 {total} 条（确定性检查，未通过者不影响草稿保存）</summary>
      <ul className="answer-list">
        {crossLayer.map((item, i) => (
          <li key={`cl-${i}`}>
            <b>跨层推断</b>（{item.section}）：{item.sentence}
            <small>　宏观词 {item.macro_terms.join("、")} → 盈利词 {item.profit_terms.join("、")}，同句无中间环节证据（D03）</small>
          </li>
        ))}
        {cycleGaps.map((item, i) => (
          <li key={`cy-${i}`}><b>周期正常化</b>（{item.board}）：{item.reason}（D04）</li>
        ))}
        {refProblems.map((item, i) => (
          <li key={`rf-${i}`}>
            <b>引用不支持</b>（{item.section}）：引用 {item.refs.join("、")}，句内数值 {item.numbers.join("、")} 不在该条目中（D07）
          </li>
        ))}
        {repeatedGaps.map((item, i) => (
          <li key={`rg-${i}`}><b>缺口重复</b>：{item.gap}（出现于 {item.sections.join("、")}，D08）</li>
        ))}
        {mislabeled.map((item, i) => (
          <li key={`ml-${i}`}><b>待验证对象被表述为低估</b>：{item}（D06）</li>
        ))}
      </ul>
    </details>
  );
}

/**
 * 候选股表格（A17/D02）：选择与发送分离。
 * 「送已选」只发勾选项；「送筛选结果」发当前过滤集合；无效代码与重复项归入
 * excluded 并说明原因，不混入发送集合（D05/E05）。
 */
export function DirectionPoolTable({
  report,
  onSendToTactics,
  onStudySingle,
  rows,
  title,
  note,
}: {
  report: DirectionReport;
  onSendToTactics: (pool: string[], context: { sourceLabel?: string; sourceReportId?: string; topic?: string; question?: string; excluded?: { symbol: string; reason: string }[] }) => void;
  onStudySingle: (symbol: string, question?: string) => void;
  /* M3-D06：可选行子集 + 自定义标题/说明——同一组件渲染「低估候选池」与「待验证研究池」两个视图；
   * 不传时行为与历史一致（渲染完整候选池）。 */
  rows?: DirectionPoolCandidate[];
  title?: string;
  note?: string;
}) {
  const pool = rows ?? report.stock_pool ?? [];
  const [selected, setSelected] = useState<string[]>([]);
  const [sector, setSector] = useState("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  /* v36 C02：候选池象限约束留痕 → 按候选代码建索引，用于「所属板块分类」列与告警标记。
   * 违规候选（来自非低估象限且正文无论证推翻）单独成集合；旧报告无该字段 → 空集合，整列不渲染。 */
  const quadrantViolations = report.pool_quadrant_violations ?? [];
  const violationBySymbol = useMemo(() => {
    const map = new Map<string, { board: string; label: string }>();
    for (const item of quadrantViolations) {
      const key = (item.symbol || "").replace(/\D/g, "") || item.symbol;
      if (key) map.set(key, { board: item.board, label: item.label });
    }
    return map;
  }, [quadrantViolations]);
  const boardJudgments = report.board_judgments ?? {};
  const hasQuadrantData = Object.keys(boardJudgments).length > 0 || quadrantViolations.length > 0;
  /** 候选 → 所属板块名：优先服务端写入，其次按板块名双向子串匹配本地 board_judgments。 */
  const boardOf = (item: DirectionPoolCandidate): string => {
    if (item.pool_board_name) return item.pool_board_name;
    const raw = (item.sector || "").trim();
    if (!raw) return "";
    if (boardJudgments[raw] != null) return raw;
    const names = Object.keys(boardJudgments);
    return names.find((name) => name && (raw.includes(name) || name.includes(raw))) ?? "";
  };
  const isViolation = (item: DirectionPoolCandidate): boolean => {
    if (item.pool_quadrant_violation != null) return item.pool_quadrant_violation;
    const key = (item.symbol || "").replace(/\D/g, "") || item.symbol;
    return violationBySymbol.has(key);
  };
  const sectors = useMemo(
    () => Array.from(new Set(pool.map((item) => item.sector || "").filter(Boolean))),
    [pool],
  );
  const filtered = useMemo(
    () => pool.filter((item) => sector === "all" || (item.sector || "") === sector),
    [pool, sector],
  );
  const scannable = (item: DirectionPoolCandidate): boolean =>
    legacyIdentityStatus(item) === "verified" && !item.duplicate_of_pool;
  const filteredSymbols = useMemo(
    () => filtered.filter(scannable).map((item) => item.symbol).filter((value, index, arr) => arr.indexOf(value) === index),
    [filtered],
  );
  const selectedSymbols = useMemo(
    () => selected.filter((symbol) => filteredSymbols.includes(symbol)),
    [selected, filteredSymbols],
  );
  const excludedOf = (candidates: DirectionPoolCandidate[]) =>
    candidates
      .filter((item) => !scannable(item))
      .map((item) => ({
        symbol: item.symbol || item.symbol_raw || "（无代码）",
        reason: item.duplicate_of_pool ? "重复候选" : legacyIdentityStatus(item) === "invalid" ? "证券身份无效" : "待核验",
      }));
  const invalidCount = pool.filter((item) => legacyIdentityStatus(item) === "invalid").length;
  const toggle = (symbol: string): void => {
    setSelected((current) => (current.includes(symbol) ? current.filter((item) => item !== symbol) : [...current, symbol]));
  };
  const sendSelected = (): void => {
    // D02：发送范围 = 当前可见（未被筛选隐藏）且已勾选的可扫描项；被隐藏的勾选默认移除，不随发送。
    // 去重保序：重复候选（duplicate_of_pool）与原候选同码，只发一次。
    const chosen = Array.from(new Set(pool.filter((item) => selectedSymbols.includes(item.symbol)).map((item) => item.symbol)));
    onSendToTactics(
      chosen,
      {
        sourceLabel: `方向研判 · ${report.topic}`,
        sourceReportId: report.report_id || "",
        topic: report.topic,
        question: "",
        excluded: [],
      },
    );
  };
  const sendFiltered = (): void => {
    onSendToTactics(
      filteredSymbols,
      {
        sourceLabel: `方向研判 · ${report.topic}（筛选结果）`,
        sourceReportId: report.report_id || "",
        topic: report.topic,
        question: "",
        excluded: excludedOf(filtered),
      },
    );
  };
  return (
    <div className="wb-pool-table" data-testid="direction-pool-table">
      <div className="wb-pool-toolbar">
        <span className="answer-block-title">
          {title ?? `候选公司（${pool.length} 家 · 有效 ${pool.filter(scannable).length} 家）`}
        </span>
        {note && <span className="wb-pool-note" title={note}>{note}</span>}
        {sectors.length > 1 && (
          <div className="pref-row">
            <button className={`tag-button ${sector === "all" ? "active" : ""}`} onClick={() => setSector("all")}>全部环节</button>
            {sectors.map((item) => (
              <button key={item} className={`tag-button ${sector === item ? "active" : ""}`} onClick={() => setSector(item)}>{item}</button>
            ))}
          </div>
        )}
      </div>
      <div className="wb-pool-counts" role="status">
        <span>已选 <b>{selectedSymbols.length}</b></span>
        <span>筛选结果可发送 <b>{filteredSymbols.length}</b></span>
        {invalidCount > 0 && <span className="coral">无效代码 {invalidCount}（不随发送）</span>}
        {quadrantViolations.length > 0 && (
          <span className="coral" title="这些候选来自「深跌未反转 / 高位 / 盈利周期顶」象限的板块，而正文没有推翻该板块分类的数值论证（C02 候选池象限约束）">
            非低估象限候选 {quadrantViolations.length}（正文无论证推翻）
          </span>
        )}
        <div className="wb-pool-actions">
          <button className="ghost-btn" disabled={selectedSymbols.length === 0} onClick={sendSelected}>
            送已选到雷达（{selectedSymbols.length}）
          </button>
          <button className="ghost-btn" disabled={filteredSymbols.length === 0} onClick={sendFiltered}>
            送筛选结果到雷达（{filteredSymbols.length}）
          </button>
        </div>
      </div>
      <div className="wb-pool-scroll">
        <table className="wb-judgement-table wb-pool">
          <thead>
            <tr>
              <th className="wb-pool-col-check">选择</th>
              <th>名称 / 代码</th>
              <th>产业链环节</th>
              <th>所属板块分类</th>
              <th>入选依据（关联 · 兑现路径）</th>
              <th>核验状态</th>
              <th>估值回链</th>
              <th>行情日期</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((item) => {
              const rowKey = item.symbol || item.symbol_raw || "";
              const isOpen = expanded === rowKey;
              const board = boardOf(item);
              const violation = isViolation(item);
              return (
                <tr
                  key={rowKey}
                  className={[
                    legacyIdentityStatus(item) === "invalid" ? "is-weak" : "",
                    violation ? "is-quadrant-warn" : "",
                    // v37 颜色标注：按所属板块分类给候选行上色（无法归属则不上色）。
                    JUDGMENT_ROW_CLASS[boardJudgments[board] ?? ""] ?? "",
                  ].filter(Boolean).join(" ")}
                >
                  <td className="wb-pool-col-check">
                    {scannable(item) ? (
                      <input
                        type="checkbox"
                        aria-label={`选择 ${item.name || item.symbol}`}
                        checked={selected.includes(item.symbol)}
                        onChange={() => toggle(item.symbol)}
                      />
                    ) : <span title="身份无效或重复的候选不可发送">—</span>}
                  </td>
                  <td><b>{item.name || "—"}</b><small className="wb-pool-code" title={poolCodeHint(item)}>{poolCodeText(item)}</small></td>
                  <td>{item.sector || item.value_chain_position || "—"}</td>
                  <td className="wb-pool-board-judgment">
                    {violation && (
                      <span
                        className="wb-pool-quadrant-flag"
                        title={`该候选所属板块「${board || item.sector || "未知"}」的系统参考分类为「${item.pool_board_judgment || violationBySymbol.get((item.symbol || "").replace(/\D/g, ""))?.label || "非低估象限"}」，而正文没有推翻该分类的数值论证——候选池代表性存疑（C02）`}
                        role="img"
                        aria-label="非低估象限候选告警"
                      >⚠</span>
                    )}
                    {hasQuadrantData ? (
                      board || item.pool_board_judgment ? (
                        <>
                          {board && <small className="wb-pool-board-name">{board}</small>}
                          <BoardValuationJudgmentTag judgment={item.pool_board_judgment || boardJudgments[board]} />
                        </>
                      ) : (
                        <span className="wb-pool-valuation-empty" title="候选未能归属到任何已分类板块（服务端按板块名双向子串匹配），不猜测分类">未归属</span>
                      )
                    ) : (
                      <span className="wb-pool-valuation-empty" title="该报告未包含板块四维分类（v36 之前生成的报告），本列不猜测分类">—</span>
                    )}
                  </td>
                  <td className="wb-pool-reason">
                    {item.business_link || item.reason || "—"}
                    {(item.profit_path || item.counter_evidence || (item.gaps?.length ?? 0) > 0) && (
                      <button className="wb-pool-expand" onClick={() => setExpanded(isOpen ? null : rowKey)}>
                        {isOpen ? "收起依据" : "查看依据"}
                      </button>
                    )}
                    {isOpen && (
                      <ul className="wb-pool-reason-detail">
                        {item.value_chain_position && <li><b>产业链位置：</b>{item.value_chain_position}</li>}
                        {item.profit_path && <li><b>业绩兑现路径：</b>{item.profit_path}</li>}
                        {item.counter_evidence && <li className="coral"><b>主要反证：</b>{item.counter_evidence}</li>}
                        {(item.gaps ?? []).map((gap) => <li key={gap} className="wb-pool-gap"><b>证据缺口：</b>{gap}</li>)}
                      </ul>
                    )}
                  </td>
                  <td><IdentityTag candidate={item} /></td>
                  <td><PoolValuationCell candidate={item} /></td>
                  <td title="行情可扫描性由战法雷达实际扫描确认（B10：不假装已核验）">{item.reference_price ? `≈${item.reference_price}（旧记录）` : "—"}</td>
                  <td>
                    <div className="wb-pool-row-actions">
                      <button
                        className="ghost-btn"
                        disabled={!scannable(item)}
                        title={scannable(item) ? "把这一只送战法雷达扫描 K 线" : "身份无效的候选不可扫描"}
                        onClick={() => onSendToTactics([item.symbol], {
                          sourceLabel: `方向研判 · ${report.topic}`,
                          sourceReportId: report.report_id || "",
                          topic: report.topic,
                          question: "",
                          excluded: [],
                        })}
                      >送雷达</button>
                      <button
                        className="ghost-btn"
                        title="进入个股研报并预填该标的与业务关联（D07：单股上下文）"
                        onClick={() => onStudySingle(item.symbol, item.business_link ? `${report.topic}：验证「${item.business_link}」` : report.topic)}
                      >研究该股</button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr><td colSpan={9} className="wb-pool-empty">没有符合筛选条件的候选。</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {(report.data_gaps?.length ?? 0) > 0 && (
        <p className="report-meta amber">数据缺口：{report.data_gaps!.join("；")}</p>
      )}
    </div>
  );
}

/**
 * v36 A02/D02：板块四维分类总表（系统参考分类汇总）。
 * 数据来源：`board_judgments`（板块名 → 分类）+ 证据快照里对应条目的依据文本与分位附录。
 * 旧报告无 `board_judgments` → 整块不渲染。系统给的是**研究分类**，模型可推翻。
 */
export function BoardJudgmentSummary({ report }: { report: DirectionReport }) {
  const judgments = report.board_judgments ?? {};
  const names = Object.keys(judgments);
  if (names.length === 0) return null;
  const evidence = report.evidence_snapshot ?? [];
  const byName = new Map(
    evidence
      .filter((item) => item.sector_name && item.valuation_judgment)
      .map((item) => [item.sector_name as string, item]),
  );
  const orderOf = (label: string) => VALUATION_JUDGMENT_ORDER.indexOf(label);
  const rows = names
    .map((name) => ({ name, label: judgments[name] ?? "", item: byName.get(name) }))
    .sort((a, b) => {
      const delta = orderOf(a.label) - orderOf(b.label);
      return delta !== 0 ? delta : a.name.localeCompare(b.name);
    });
  return (
    <div className="wb-direction-board-judgments">
      <span className="answer-block-title">
        板块四维分类总表（系统参考分类 · 判定顺序 {VALUATION_JUDGMENT_ORDER.join(" → ")}）
      </span>
      <div className="wb-pool-scroll">
        <table className="wb-judgement-table wb-board-judgment">
          <thead>
            <tr>
              <th>板块</th>
              <th>分类</th>
              <th>PB 分位中位数</th>
              <th>PE 分位中位数</th>
              <th>判定依据（逐维数值）</th>
              <th>引用</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.name}
                className={[
                  JUDGMENT_ROW_CLASS[row.label] ?? "",
                  NON_LOW_JUDGMENT_LABELS.includes(row.label) ? "is-quadrant-warn" : "",
                ].filter(Boolean).join(" ")}
              >
                <td><b>{row.name}</b></td>
                <td><BoardValuationJudgmentTag judgment={row.label} rationale={row.item?.valuation_judgment_rationale} /></td>
                <td>{row.item?.percentile_appendix?.pb_percentile_median != null
                  ? `${row.item.percentile_appendix.pb_percentile_median.toFixed(1)}%`
                  : (row.label === "待定" ? "不可得" : "—")}</td>
                <td>{row.item?.percentile_appendix?.pe_percentile_median != null
                  ? `${row.item.percentile_appendix.pe_percentile_median.toFixed(1)}%`
                  : (row.label === "待定" ? "不可得" : "—")}</td>
                <td className="wb-pool-reason">
                  {row.item?.valuation_judgment_rationale || (
                    <span className="wb-pool-valuation-empty">本条为初筛对照清单（未深取代表股分位），依据见证据条目</span>
                  )}
                </td>
                <td>
                  {row.item && <span className="soft-tag blue">{row.item.evidence_id}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="report-meta">
        以上为研究分类，非买卖指令。模型可推翻系统参考分类，但推翻须在正文给出逐维数值论证；
        候选池只应从「低估候选」象限提名。
      </p>
    </div>
  );
}

/**
 * v36 C02/C03：候选池象限约束与分类判定引用闸门的告警披露。
 * 旧报告无这两个字段 → 整块不渲染。
 */
export function DirectionQuadrantWarnings({ report }: { report: DirectionReport }) {
  const violations = report.pool_quadrant_violations ?? [];
  const claims = report.unsupported_judgment_claims ?? [];
  if (violations.length === 0 && claims.length === 0) return null;
  return (
    <div className="wb-direction-quadrant-warnings">
      {violations.length > 0 && (
        <details className="wb-quality-warn" open>
          <summary>候选池象限告警 {violations.length} 条（候选来自非低估象限且正文无论证推翻）</summary>
          <ul className="answer-list">
            {violations.map((item, i) => (
              <li key={`${item.symbol}-${i}`} className="coral">
                <b>{item.symbol}</b>
                {item.sector ? `（${item.sector}）` : ""} → 板块「{item.board}」分类为
                <span className="soft-tag coral">{item.label}</span>
                ——正文未见推翻该板块分类的数值论证。
              </li>
            ))}
          </ul>
        </details>
      )}
      {claims.length > 0 && (
        <details className="wb-quality-warn" open>
          <summary>分类判定引用告警 {claims.length} 条（分类标签表述缺少对应板块 [E#] 引用）</summary>
          <ul className="answer-list">
            {claims.map((item, i) => (
              <li key={`${item.section}-${i}`} className="amber">
                <b>{item.section}</b>
                {item.claims?.length ? `（分类表述：${item.claims.join("、")}）` : ""}——
                {JUDGMENT_GATE_REASON[item.reason] ?? item.reason}
                {item.boards?.length ? `；涉事板块：${item.boards.join("、")}` : ""}
                {item.refs?.length ? `（该小节引用 ${item.refs.join("/")}）` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** 方向反方审查卡（B06）。 */
export function DirectionCounterCheckCard({ report }: { report: DirectionReport }) {
  const check = report.counter_check;
  if (!check) return null;
  if (!check.ok) {
    return (
      <div className="wb-counter wb-counter-failed">
        <div className="wb-counter-head">
          <span className="answer-block-title">反方审查</span>
          <span className="soft-tag warn">本次未完成</span>
        </div>
        <p className="report-meta">{check.detail || "本次未能完成反方审查（不影响本研判的交付）。"}</p>
      </div>
    );
  }
  return (
    <div className="wb-counter">
      <div className="wb-counter-head">
        <span className="answer-block-title">反方审查</span>
        <span className="soft-tag blue">{check.verdict_robustness === "robust" ? "结论耐受" : check.verdict_robustness === "fragile" ? "结论脆弱" : "结论有争议"}</span>
      </div>
      <p className="wb-counter-note">独立反方视角：检查需求前置、供给过剩、政策兑现、价格下行与资本开支压力；不重写正文。</p>
      {check.strongest_counter && (
        <div className="wb-counter-item is-counter">
          <span className="wb-counter-label">最强反方论点</span>
          <p>{check.strongest_counter}</p>
        </div>
      )}
      {(check.affected_conclusions?.length ?? 0) > 0 && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">受影响的结论</span>
          <ul className="wb-followup-claim-list">
            {check.affected_conclusions!.map((item, i) => {
              const meta = CC_EFFECT_META[item.effect] ?? CC_EFFECT_META.cannot_judge!;
              return (
                <li key={`${item.id}-${i}`} className="wb-followup-claim">
                  <span className="soft-tag">{item.id}</span>
                  <span className={`soft-tag ${meta.tone}`}>{meta.label}</span>
                  <span className="wb-followup-claim-reason">{item.reason}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {(check.pool_challenges?.length ?? 0) > 0 && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">候选入选理由质疑</span>
          <ul className="answer-list">
            {check.pool_challenges!.map((item, i) => (
              <li key={`${item.symbol}-${i}`}><b>{item.symbol}</b>：{item.challenge}</li>
            ))}
          </ul>
        </div>
      )}
      {/* M3-D05：反方审查的定稿执行留痕（撤下/降级清单、正文标注数、未匹配原句）。 */}
      {report.counter_check_application?.applied && (
        <div className="wb-counter-item" data-testid="direction-counter-application">
          <span className="wb-counter-label">反方审查已进入定稿</span>
          <ul className="answer-list">
            <li>
              撤下判断 {(report.counter_check_application.removed_judgment_ids ?? []).length} 条
              {(report.counter_check_application.removed_judgment_ids ?? []).length > 0 && (
                <span>（{(report.counter_check_application.removed_judgment_ids ?? []).join("、")}）</span>
              )}
              ；降级判断 {(report.counter_check_application.downgraded_judgment_ids ?? []).length} 条
              {(report.counter_check_application.downgraded_judgment_ids ?? []).length > 0 && (
                <span>（{(report.counter_check_application.downgraded_judgment_ids ?? []).join("、")}）</span>
              )}
              ——
              {(report.counter_check_application.annotated_sentences ?? 0) > 0
                ? `已在正文加行内标注 ${report.counter_check_application.annotated_sentences} 处`
                : "正文未匹配到可标注的句子"}
              ，摘要与风险段落同步修订。
            </li>
            {(report.counter_check_application.annotation_misses ?? []).length > 0 && (
              <li className="amber">
                未在正文定位到原句 {(report.counter_check_application.annotation_misses ?? []).length} 条：
                {(report.counter_check_application.annotation_misses ?? []).join("；")}
                （已改为在正文开头集中列出并注明已撤下，如实展示、未静默丢弃）
              </li>
            )}
            {report.counter_check_application.catalysts_untouched_reason && (
              <li><small>{report.counter_check_application.catalysts_untouched_reason}</small></li>
            )}
          </ul>
        </div>
      )}
      {!report.counter_check_application?.applied && report.counter_check_application?.reason && (
        <div className="wb-counter-item">
          <span className="wb-counter-label">反方审查未进入定稿</span>
          <p>{report.counter_check_application.reason}</p>
        </div>
      )}
    </div>
  );
}

/** 核心判断分层列表（B05）。 */
export function DirectionJudgments({ report }: { report: DirectionReport }) {
  const judgments = report.core_judgments ?? [];
  if (judgments.length === 0) return null;
  return (
    <div className="wb-direction-judgments">
      <span className="wb-counter-label">核心判断（按 层级 分开陈述；「行业增长」不直接等于「公司利润增长」）</span>
      <ul className="wb-followup-claim-list">
        {judgments.map((item) => (
          <li key={item.id} className="wb-followup-claim">
            <span className="soft-tag">{item.id}</span>
            <span className={`soft-tag ${JUDGMENT_KIND_TONE[item.kind] ?? "gray"}`} title={`判断层级：${item.kind_label || item.kind}`}>{item.kind_label || item.kind}</span>
            <span className="wb-followup-claim-reason">
              {item.text}
              {item.confidence && <small> · 置信 {item.confidence}</small>}
              {(item.missing ?? []).length > 0 && <small className="amber"> · 缺 {item.missing!.join("、")}</small>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** v33 A06：方向研判阅读区四页签（概览 / 产业分析 / 候选股 / 证据与风险）。 */
export function DirectionResultTabs({
  report,
  onSendToTactics,
  onStudySingle,
  followup,
  followupCount = 0,
  followupActive = false,
}: {
  report: DirectionReport;
  onSendToTactics: (pool: string[], context: { sourceLabel?: string; sourceReportId?: string; topic?: string; question?: string; excluded?: { symbol: string; reason: string }[] }) => void;
  onStudySingle: (symbol: string, question?: string) => void;
  /** v38：追问面板（由外层挂载，复用个股研报同一 FollowUpPanel 与状态机）。 */
  followup?: ReactNode;
  followupCount?: number;
  followupActive?: boolean;
}) {
  const [tab, setTab] = useState<DirectionTabKey>("overview");
  const invalidCount = (report.stock_pool ?? []).filter((item) => item.identity_status === "invalid").length;
  const researchMeta = report.research_mode ? RESEARCH_MODE_META[report.research_mode] : undefined;
  return (
    <div className="wb-result-tabs wb-direction-tabs">
      <div className="wb-result-tabbar is-sticky" role="tablist" aria-label="方向研判阅读导航">
        {(Object.keys(DIRECTION_TAB_META) as DirectionTabKey[])
          // v38：追问页签仅在挂载了追问面板时出现（旧报告无 report_id 时不显示空页签）。
          .filter((key) => key !== "followup" || Boolean(followup))
          .map((key) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={`wb-result-tab ${tab === key ? "active" : ""}`}
            title={DIRECTION_TAB_META[key]!.hint}
            onClick={() => setTab(key)}
          >
            {DIRECTION_TAB_META[key]!.label}
            {key === "candidates" && invalidCount > 0 && (
              <span className="wb-tab-badge warn" title={`${invalidCount} 条候选身份无效，不可扫描`}>{invalidCount}</span>
            )}
            {key === "followup" && followupCount > 0 && (
              <span className={`wb-tab-badge ${followupActive ? "" : "ok"}`} title={`已生成 ${followupCount} 条分析附录`}>{followupCount}</span>
            )}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          <div className="wb-direction-quality">
            {/* v35 A01：主题类型徽章（产业/风格/复合/通用）——旧报告无该字段时不渲染。 */}
            <ThemeKindTag report={report} />
            {researchMeta && (
              <span className={`soft-tag ${researchMeta.tone}`} title={researchMeta.hint}>{researchMeta.label}</span>
            )}
            {report.quality_status_label && (
              <span
                className={`soft-tag ${QUALITY_STATUS_TONE[report.quality_status ?? ""] ?? "gray"}`}
                title="方向研判专属质量三态（与个股研报同口径：缺小节/无正文/无核心判断 → 不完整）"
              >
                质量：{report.quality_status_label}
              </span>
            )}
            {report.mode_label && <span className="soft-tag gray" title="方向研判独立篇幅预算（B08）">{report.mode_label} · {report.report_chars ?? "—"} 字</span>}
            {/* M3-D01：报告形态徽章（旧报告无 report_form → 整块不渲染）。 */}
            {report.report_form && DIRECTION_FORM_META[report.report_form] != null && (
              <span
                className={`soft-tag ${DIRECTION_FORM_META[report.report_form]?.tone ?? "gray"}`}
                title={DIRECTION_FORM_META[report.report_form]?.hint}
                data-testid="direction-form-badge"
              >
                {report.report_form_label || DIRECTION_FORM_META[report.report_form]?.label}
              </span>
            )}
            {report.is_draft && <span className="soft-tag warn">草稿 · 不计入正式报告</span>}
          </div>
          {report.executive_summary && (
            <p className="wb-direction-summary"><b>{report.executive_summary}</b></p>
          )}
          {/* v37 结论优先：三问速览置顶（分类已由服务端判定，首屏直接看答案）。 */}
          <DirectionAnswerFirst report={report} />
          {/* M3-D02：前置行业比较表（先研究谁 / 依据 / 等待什么变化；旧报告无字段时整块不渲染）。 */}
          <DirectionIndustryComparison report={report} />
          {/* v36 A02/D02：板块四维分类总表（旧报告无 board_judgments 时整块不渲染）。 */}
          <BoardJudgmentSummary report={report} />
          {report.next_verification && (
            <div className="wb-counter-item is-support">
              <span className="wb-counter-label">下一验证动作</span>
              <p>{report.next_verification}</p>
            </div>
          )}
          <DirectionJudgments report={report} />
          {/* v36 C02/C03：候选池象限与分类引用告警披露。 */}
          <DirectionQuadrantWarnings report={report} />
          {(report.quality_warnings?.length ?? 0) > 0 && (
            <details className="wb-quality-warn">
              <summary>质量告警 {report.quality_warnings!.length} 条（软告警，如实展示）</summary>
              <ul className="answer-list">{report.quality_warnings!.map((item, i) => <li key={i}>{item}</li>)}</ul>
            </details>
          )}
          {(report.quality_blockers?.length ?? 0) > 0 && (
            <ul className="answer-list wb-direction-blockers">
              {report.quality_blockers!.map((blocker, i) => <li key={i} className="coral">{blocker.message}</li>)}
            </ul>
          )}
        </div>
      )}

      {tab === "analysis" && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          {(report.missing_sections?.length ?? 0) > 0 && (
            <p className="wb-incomplete" role="alert">
              缺少必需小节：{report.missing_sections!.map((name) => <span key={name} className="soft-tag coral">{name} · 未生成</span>)}
            </p>
          )}
          {/* v37 分层折叠：十节按标题拆成可折叠块（首节默认展开），标题行可快速扫读。 */}
          <DirectionFoldedReport report={report} />
        </div>
      )}

      {tab === "candidates" && (
        <div className="wb-result-tabpane" role="tabpanel">
          {report.stock_pool.length > 0 ? (
            /* M3-D06：服务端已拆池时按「低估候选池 / 待验证研究池」分列渲染，
             * 待验证对象不进入低估语境；旧报告（无池字段）保持单一表格。 */
            (report.low_valuation_pool || report.research_pool) ? (
              <>
                <DirectionPoolTable
                  report={report}
                  rows={report.low_valuation_pool ?? []}
                  title={`低估候选池（${(report.low_valuation_pool ?? []).length} 家）——身份已核验 + 有估值回链 + 业务关联齐全 + 板块分类非高位象限`}
                  note="D06：只有全部资格通过的候选才计入低估候选池"
                  onSendToTactics={onSendToTactics}
                  onStudySingle={onStudySingle}
                />
                <DirectionPoolTable
                  report={report}
                  rows={report.research_pool ?? []}
                  title={`待验证研究池（${(report.research_pool ?? []).length} 家）——身份/估值/业务关联或板块象限尚未过闸，不得当作已确认低估`}
                  note="D06：附缺口与验证动作，验证通过后并入低估候选池"
                  onSendToTactics={onSendToTactics}
                  onStudySingle={onStudySingle}
                />
              </>
            ) : (
              <DirectionPoolTable report={report} onSendToTactics={onSendToTactics} onStudySingle={onStudySingle} />
            )
          ) : (
            <p className="wb-history-empty">
              本次研判没有给出候选公司{(report.data_gaps?.length ?? 0) > 0 ? `（原因：${report.data_gaps!.join("；")}）` : ""}——按 B12 口径，宁缺毋假。
            </p>
          )}
        </div>
      )}

      {tab === "evidence" && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          <DirectionCounterCheckCard report={report} />
          {/* M3-D03/D04/D07/D08：确定性检查复核块。 */}
          <DirectionQualityReview report={report} />
          <DirectionJudgments report={report} />
          {/* v36 A02/D02：板块四维分类总表。 */}
          <BoardJudgmentSummary report={report} />
          {/* v36 C02/C03：候选池象限与分类引用告警披露。 */}
          <DirectionQuadrantWarnings report={report} />
          {(report.evidence_snapshot?.length ?? 0) > 0 && (
            <div className="wb-direction-evidence">
              <span className="answer-block-title">证据快照（[E#] 可回链；来源 / 数据截至 / 抓取时间逐条可查）</span>
              <ul className="wb-evidence-sources">
                {report.evidence_snapshot!.map((item) => (
                  <li key={item.evidence_id}>
                    <span className="soft-tag blue">{item.evidence_id}</span>
                    <b>{item.title || item.source_type}</b>
                    <span className="wb-evidence-label">{item.source}</span>
                    {/* v36 A02/A04：系统参考分类徽章 + 初筛对照清单标记。 */}
                    <BoardValuationJudgmentTag
                      judgment={item.valuation_judgment}
                      rationale={item.valuation_judgment_rationale}
                    />
                    {item.prescreen_excluded && (
                      <span
                        className="soft-tag gray"
                        title="亏损面 ≥ 40%，按初筛闸门（A04）未纳入代表股深取名单，本条目仅披露估值中位数与亏损面"
                      >对照清单 · 未深取</span>
                    )}
                    <small>{item.content}</small>
                    <small className="wb-src-meta">抓取 {item.retrieved_at.slice(0, 19).replace("T", " ")}</small>
                    {/* v35 B03：板块估值证据的代表股分位附录（代理口径如实展示）。 */}
                    {item.percentile_appendix && (
                      <div className="wb-evidence-appendix">
                        <small>
                          代表股分位：可得 {item.percentile_appendix.comparable_count} / {item.percentile_appendix.representative_count} 只
                          {item.percentile_appendix.pb_percentile_median != null && ` · PB 分位中位数 ${item.percentile_appendix.pb_percentile_median.toFixed(1)}%`}
                        </small>
                        {(item.percentile_appendix.lowest_three?.length ?? 0) > 0 && (
                          <ul className="wb-evidence-appendix-list">
                            {item.percentile_appendix.lowest_three!.map((stock) => (
                              <li key={stock.symbol}>
                                {stock.name || stock.symbol}（{stock.symbol}）
                                {stock.pb_mrq_percentile != null && ` · PB 分位 ${stock.pb_mrq_percentile.toFixed(1)}%`}
                              </li>
                            ))}
                          </ul>
                        )}
                        {(item.percentile_appendix.incomparable_symbols?.length ?? 0) > 0 && (
                          <small className="wb-evidence-appendix-note">
                            不可比分位（正数样本不足）：{item.percentile_appendix.incomparable_symbols!.join("、")}
                          </small>
                        )}
                        {item.percentile_appendix.disclaimer && (
                          <small className="wb-evidence-appendix-note">{item.percentile_appendix.disclaimer}</small>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {/* v35 C03：四类缺口结构化留痕（无数据风格词 / 横截面失败 / 代表股分位不可得 / 候选池估值缺失）。 */}
          {(report.style_gap_entries?.length ?? 0) > 0 && (
            <div className="wb-direction-style-gaps">
              <span className="answer-block-title">风格主题数据缺口（按缺类分列，各带补证动作）</span>
              <ul className="wb-evidence-sources">
                {report.style_gap_entries!.map((gap) => (
                  <li key={gap.code}>
                    <span className="soft-tag warn">{gap.label || gap.code}</span>
                    <small>{gap.text}</small>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report.evidence_unavailable && (
            <p className="report-meta amber">来源不可用：{report.evidence_unavailable}（不静默换源，缺口如实声明）。</p>
          )}
          {report.catalysts.length > 0 && (
            <div><span className="answer-block-title">关键催化</span><ul className="answer-list">{report.catalysts.map((item) => <li key={item}>{item}</li>)}</ul></div>
          )}
          {report.risks.length > 0 && (
            <div><span className="answer-block-title">主要风险（风险/反证，非卖出建议）</span><ul className="answer-list">{report.risks.map((item) => <li key={item}>{item}</li>)}</ul></div>
          )}
        </div>
      )}

      {tab === "followup" && followup && (
        <div className="wb-result-tabpane wb-reading" role="tabpanel">
          <p className="report-meta">
            围绕本方向研判追问：原报告不可变，每次追问都作为独立「分析附录」追加留痕
            （记录父报告版本、补充材料、模型与提示词版本）。补充材料默认未独立验证，引用时会显著标注。
          </p>
          {followup}
        </div>
      )}
    </div>
  );
}
