/**
 * v32 研报追问面板（2026-09-13 方案 §三）+ v39 质量升级（2026-09-19 路线图 P0/P1）。
 * - 提问区：追问入口（Q10 解读本报告 / 补充研究）+ 问题输入 + 常用模板 + 补充材料；
 * - 附录区：全部历史追问按序展示（append-only，原报告不可变）。
 *
 * v39 的展示顺序是**固定的**（Q02 验收「首屏即可看到直接回答」）：
 *   问题 → 状态说明行 → 直接回答 → 三条状态轴 → 数据截至/补证结果 → 关键条件 → 补证缺口
 *   → 回答正文 → 受影响判断（摘要一行）→ 情景变化 → 新增验证点 → 价位时点与估值/倾向口径
 *   → 本次材料与来源目录 → 局限；判断影响表、模型与提示词版本折叠在「审计细节」里（仍在，只是不占首屏）。
 * 原因：上一版把 5 行「无法判断」的判断影响表排在回答之前，用户的问题答案被挤到折叠线以下。
 *
 * 颜色语义：支持=青绿（正向证据）、削弱=珊瑚红（风险/反证）、无法判断/待确认=琥珀、
 * 无关/中性=灰蓝；所有状态色表达**信息状态**，不表达买卖方向。
 */
import { useState } from "react";
import type { AnalysisTurn, FollowupClaim, ReportWatchpoint } from "./researchTypes";
import type { FollowUpSupplementInput } from "../../hooks/useResearch";
import { MarkdownishText, SourceCatalogTable, WatchpointTable } from "./panels";
import {
  priceRefCells,
  referenceBoundLabel,
  retrievalStatusView,
  scenarioProbabilityWording,
  valuationEvidenceDisplay,
  watchpointDateView,
  watchpointReview,
} from "./format";

/** 受影响 claim 四态 → 文案与色调（信息状态，非买卖方向；与全站状态色一致）。 */
export const EFFECT_META: Record<string, { label: string; tone: string; hint: string }> = {
  supports: { label: "支持", tone: "mint", hint: "补充信息支持该判断（正向证据）" },
  weakens: { label: "削弱", tone: "coral", hint: "补充信息可能推翻或削弱该判断（风险/反证）" },
  irrelevant: { label: "无关", tone: "gray", hint: "与该判断无直接影响路径" },
  cannot_judge: { label: "无法判断", tone: "warn", hint: "证据不足或路径不明确（待确认）" },
};

/** 结论变化五态 → 文案与色调。 */
export const CONCLUSION_CHANGE_META: Record<string, { label: string; tone: string; hint: string }> = {
  unchanged: { label: "结论不变", tone: "gray", hint: "新信息未改变原报告结论" },
  strengthened: { label: "结论增强", tone: "mint", hint: "新信息强化了原报告结论" },
  weakened: { label: "结论减弱", tone: "coral", hint: "新信息削弱了原报告结论" },
  changed: { label: "结论改变", tone: "blue", hint: "原报告结论需要按新方向修正" },
  undetermined: { label: "无法判定", tone: "warn", hint: "证据不足以判定结论变化方向" },
};

/* —— Q03（2026-09-19 路线图）：三条状态轴各说各的事，不再互相替身 ——
 * 上一版只有「结论不变」一个 chip，与 5 行「无法判断」并排时读者会误读成「模型已经复核过最新行情」。 */

/** 轴 1：原判断是否修订。 */
export const REVISION_META: Record<string, { label: string; tone: string; hint: string }> = {
  unrevised: { label: "原判断未修订", tone: "gray", hint: "本轮没有可改变原报告判断的新证据" },
  revised: { label: "原判断需修订", tone: "blue", hint: "新证据要求修订原判断；原报告不可变，修订只记在本附录" },
  undetermined: { label: "修订状态待定", tone: "warn", hint: "证据不足以判定原判断是否需要修订" },
};

/** 轴 2：本次问题能否回答（旧记录一律「未评估」，不替老记录宣称已回答）。 */
export const ANSWERABILITY_META: Record<string, { label: string; tone: string; hint: string }> = {
  answered: { label: "本次问题已回答", tone: "mint", hint: "回答覆盖了问题的主要部分" },
  partially_answered: { label: "部分回答（存在未证实环节）", tone: "warn", hint: "已回答，但有环节缺证据（见补证缺口）" },
  not_answerable: { label: "以现有证据无法回答", tone: "coral", hint: "现有证据不足以回答该问题，如实标注未编造" },
};
export const LEGACY_ANSWERABILITY_LABEL = "旧版记录未评估";

/** 轴 3：本轮用了什么数据（与后端 DATA_BASIS_LABELS 同词——屏显与导出共用同一份）。 */
export const DATA_BASIS_LABEL: Record<string, string> = {
  report_only: "沿用原报告判断，未更新行情",
  user_supplement: "使用用户补充材料（未独立验证），未更新行情",
  refreshed_data: "使用本次刷新获取的数据",
};
export const DATA_BASIS_TONE: Record<string, string> = {
  report_only: "gray",
  user_supplement: "warn",
  refreshed_data: "mint",
};
/** report_only 时必须带上这句：否则「未更新行情」会被读成已经看过最新数据。 */
export const REPORT_ONLY_AS_OF_NOTE = "（未更新行情，不代表已复核最新数据）";

/** Q10：两种追问入口（默认解读本报告——不悄悄加钱加时延）。 */
export const FOLLOWUP_MODE_META: Record<string, { label: string; short: string; hint: string }> = {
  interpret: {
    label: "解读本报告",
    short: "解读",
    hint: "只依据原报告与你的补充材料作答，不获取新事实、不更新行情。",
  },
  supplement_research: {
    label: "补充研究",
    short: "补证",
    hint: "本轮重新获取行情/公告等数据；成功与失败都会如实列出，失败不会被包装成研究完成。",
  },
};
export const DEFAULT_FOLLOWUP_MODE = "interpret";

/** 三条状态轴（Q03）：屏显与导出共用同一回退口径，任何一处再写一遍三元表达式都会开始漂移。 */
export interface FollowupStatusAxes {
  revision: { status: string; label: string; tone: string; hint: string };
  answerability: { status: string; label: string; tone: string; hint: string };
  dataBasis: { status: string; label: string; tone: string };
  /** 紧跟问题的一行说明（含「未更新行情」这类限定，不暗示做过复核）。 */
  stateLine: string;
  /** 非空 = 模型自相矛盾（如声称结论改变却无任何证据支持），按琥珀告警如实展示。 */
  conflict: string;
  /** true = 1.1 契约的历史记录：三条轴字段缺失，按当时可得的信息确定性补全。 */
  legacy: boolean;
}

export function followupStatusAxes(turn: AnalysisTurn): FollowupStatusAxes {
  const supplements = turn.supplement_evidence ?? [];
  const revisionStatus = (turn.revision_status ?? "").trim();
  const basisStatus = (turn.data_basis ?? "").trim() || (supplements.length > 0 ? "user_supplement" : "report_only");
  const basisLabel = turn.data_basis_label || DATA_BASIS_LABEL[basisStatus] || "本轮数据来源未标注";
  const legacy = revisionStatus === "";
  const revisionStatusResolved = legacy
    // 旧记录没有修订轴：只按结论变化推导（不重新判断，也不暗示复核过最新数据）。
    ? (["strengthened", "weakened", "changed"].includes(turn.conclusion_change) ? "revised"
      : turn.conclusion_change === "undetermined" ? "undetermined" : "unrevised")
    : revisionStatus;
  const revisionMeta = REVISION_META[revisionStatusResolved] ?? REVISION_META.undetermined!;
  const answerabilityStatus = (turn.answerability ?? "").trim();
  const answerabilityMeta = ANSWERABILITY_META[answerabilityStatus];
  const stateLine = legacy
    ? `${basisLabel}${basisStatus === "report_only" ? REPORT_ONLY_AS_OF_NOTE : ""}（1.1 旧版记录，未重新评估）`
    : turn.state_line || `${basisLabel}${basisStatus === "report_only" ? REPORT_ONLY_AS_OF_NOTE : ""}`;
  return {
    revision: {
      status: revisionStatusResolved,
      label: legacy ? revisionMeta.label : (turn.revision_status_label || revisionMeta.label),
      tone: revisionMeta.tone,
      hint: legacy ? `${revisionMeta.hint}（旧记录按结论变化推导，未重新评估）` : revisionMeta.hint,
    },
    answerability: legacy
      ? { status: "", label: LEGACY_ANSWERABILITY_LABEL, tone: "gray", hint: "该附录生成于 1.2 契约之前，当时未记录可回答程度" }
      : {
        status: answerabilityStatus,
        label: turn.answerability_label || answerabilityMeta?.label || "可回答程度未标注",
        tone: answerabilityMeta?.tone ?? "gray",
        hint: answerabilityMeta?.hint ?? "该附录未记录可回答程度",
      },
    dataBasis: { status: basisStatus, label: basisLabel, tone: DATA_BASIS_TONE[basisStatus] ?? "gray" },
    stateLine,
    conflict: turn.status_conflict ?? "",
    legacy,
  };
}

/**
 * Q04：受影响判断的展示分组（只展开支持/削弱，其余合并成一行共同说明）。
 *
 * 后端 1.2 契约直接给 `expanded_claims / unresolved_claims / shared_limitation`；
 * 1.1 旧记录没有这些字段，这里按**同一规则**在前端分组——只改变展示层级，
 * 绝不把「无法判断」重分类成支持或削弱（Q04 验收）。
 */
export interface FollowupClaimGroups {
  expanded: FollowupClaim[];
  unresolved: FollowupClaim[];
  shared: string;
  /** 后端给的未分类原因行；旧记录没有时为前端合成行（文案里点明是合并说明）。 */
  sharedFromRecord: boolean;
}

const EXPANDED_EFFECTS = new Set(["supports", "weakens"]);

export function followupClaimGroups(turn: AnalysisTurn): FollowupClaimGroups {
  const affected = (turn.affected_claims ?? []) as FollowupClaim[];
  const expanded = turn.expanded_claims ?? affected.filter((row) => EXPANDED_EFFECTS.has(row.effect));
  const unresolved = turn.unresolved_claims
    ?? affected.filter((row) => !EXPANDED_EFFECTS.has(row.effect) && row.effect !== "");
  const sharedFromRecord = Boolean(turn.shared_limitation?.trim());
  const shared = sharedFromRecord
    ? turn.shared_limitation!.trim()
    : unresolved.length > 0
      ? `${unresolved.map((row) => row.claim_id || "—").join("、")}：${followupStatusAxes(turn).dataBasis.label}`
        + "——未取得可改变这些判断的新材料，原结论按未复核处理（不因此转为支持或削弱）。"
      : "";
  return { expanded, unresolved, shared, sharedFromRecord };
}

/** 判断名称（编号配合原文展示；旧记录没有 claim_name 时只显示编号，不编造名称）。 */
function claimName(claim: FollowupClaim): string {
  return (claim.claim_name ?? "").trim();
}

/** 单条追问附录卡片（只读；顺序见文件头注释，Q02 验收「回答前置、审计折叠」）。 */
export function AnalysisTurnCard({ turn }: { turn: AnalysisTurn }) {
  const axes = followupStatusAxes(turn);
  const change = CONCLUSION_CHANGE_META[turn.conclusion_change] ?? CONCLUSION_CHANGE_META.undetermined!;
  const groups = followupClaimGroups(turn);
  const supplements = turn.supplement_evidence ?? [];
  const snapshots = turn.evidence_snapshot ?? [];
  const retrieval = retrievalStatusView(turn.retrieval_status);
  const watchpoints = turn.new_watchpoints ?? [];
  const priceRefs = turn.price_refs ?? [];
  const limitations = turn.limitations ?? [];
  const keyConditions = turn.key_conditions ?? [];
  const evidenceGaps = turn.evidence_gaps ?? [];
  const valuation = turn.valuation_view ? valuationEvidenceDisplay(turn.valuation_view) : null;
  const probabilityView = turn.probability_view ?? null;
  const probabilityWording = scenarioProbabilityWording(probabilityView);
  const sourceCatalog = turn.source_catalog ?? [];
  const modeMeta = FOLLOWUP_MODE_META[(turn.mode ?? "").trim()];
  return (
    <article className="wb-followup-turn" aria-label={`追问附录 ${turn.turn_index}`}>
      <header className="wb-followup-turn-head">
        <span className="soft-tag blue" title="追问结果保存为不可变分析附录（append-only），不改动原报告">附录 #{turn.turn_index}</span>
        <span className={`soft-tag ${change.tone}`} title={change.hint}>结论：{change.label}</span>
        {(turn.mode_label || modeMeta) && (
          <span className="soft-tag gray" title={modeMeta?.hint ?? turn.mode_label}>入口：{modeMeta?.label ?? turn.mode_label}</span>
        )}
        <span className="wb-followup-time" title={`锚定原报告 v${turn.base_report_version}`}>
          {new Date(turn.created_at).toLocaleString()}
        </span>
      </header>

      <p className="wb-followup-question"><b>问：</b>{turn.question}</p>
      <p className="wb-followup-state" title="三条状态轴之一：本轮用了什么数据——没有新证据时不得暗示已复核最新行情">
        {axes.stateLine}
      </p>

      {turn.direct_answer?.trim() && (
        <div className="wb-followup-direct">
          <span className="wb-counter-label">直接回答</span>
          <p>{turn.direct_answer.trim()}</p>
        </div>
      )}

      <div className="wb-followup-axis">
        <span className={`soft-tag ${axes.revision.tone}`} title={axes.revision.hint}>修订：{axes.revision.label}</span>
        <span className={`soft-tag ${axes.answerability.tone}`} title={axes.answerability.hint}>可回答：{axes.answerability.label}</span>
        <span className={`soft-tag ${axes.dataBasis.tone}`} title="三条状态轴之三：是否使用了新增数据">{axes.dataBasis.label}</span>
        {axes.conflict && <span className="soft-tag warn" title="模型自相矛盾，已如实留痕而非替模型选一个口径">状态轴冲突</span>}
      </div>
      {axes.conflict && <p className="report-meta amber" role="note">{axes.conflict}</p>}

      <p className="report-meta">
        {/* 旧记录连 data_as_of 都没有：整行不渲染，比写「未标注」更诚实（不暗示做过时点核对）。 */}
        {turn.data_as_of !== undefined && (
          <>数据截至 <b>{turn.data_as_of || "未标注"}</b> · </>
        )}
        <span className={`soft-tag ${retrieval.tone}`} title={retrieval.note || retrievalStatusView().note}>{retrieval.text}</span>
      </p>

      {keyConditions.length > 0 && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">关键条件（直接回答成立的前提）</span>
          <ul className="answer-list">
            {keyConditions.map((item, i) => <li key={`cond-${i}`}>{item}</li>)}
          </ul>
        </div>
      )}
      {evidenceGaps.length > 0 && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">补证缺口（还缺什么证据才能补齐）</span>
          <ul className="answer-list wb-followup-gaps">
            {evidenceGaps.map((item, i) => <li key={`gap-${i}`}>{item}</li>)}
          </ul>
        </div>
      )}

      <div className="wb-followup-answer">
        <MarkdownishText content={turn.answer} />
      </div>

      {(groups.expanded.length > 0 || groups.unresolved.length > 0) && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">受影响的核心判断（只展开有新增证据路径的判断）</span>
          {groups.expanded.length > 0 ? (
            <ul className="wb-followup-claim-list">
              {groups.expanded.map((claim, i) => {
                const meta = EFFECT_META[claim.effect] ?? EFFECT_META.cannot_judge!;
                return (
                  <li key={`${claim.claim_id}-x-${i}`} className="wb-followup-claim">
                    <span className="soft-tag">{claim.claim_id}</span>
                    <span className={`soft-tag ${meta.tone}`} title={meta.hint}>{meta.label}</span>
                    {claimName(claim) && <b className="wb-followup-claim-name">{claimName(claim)}</b>}
                    <span className="wb-followup-claim-reason">{claim.reason || "—"}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="report-meta">本轮没有任何判断获得可展开的支持或削弱证据。</p>
          )}
          {groups.unresolved.length > 0 && (
            /* Q04：无法判断/无关不再逐条刷屏（样报 5 行「无法判断」曾占满回答主体），
               合并成一行共同限制 + 编号与名称清单；完整理由在「审计细节」里逐条可查。 */
            <p className="report-meta wb-followup-unresolved" role="note">
              {groups.shared}
              <span className="wb-followup-unresolved-list">
                {groups.unresolved.map((claim, i) => (
                  <span key={`${claim.claim_id}-u-${i}`} className="wb-followup-unresolved-item">
                    {claim.claim_id}{claimName(claim) ? ` ${claimName(claim)}` : ""}
                    （{(EFFECT_META[claim.effect] ?? EFFECT_META.cannot_judge!).label}）
                  </span>
                ))}
              </span>
            </p>
          )}
        </div>
      )}

      {(turn.scenario_changes?.length ?? 0) > 0 && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">情景变化</span>
          <ul className="answer-list">
            {turn.scenario_changes!.map((item, i) => (
              <li key={i}><b>{item.name || "情景"}</b>：{item.change}</li>
            ))}
          </ul>
        </div>
      )}
      {watchpoints.length > 0 && <FollowUpWatchpoints watchpoints={watchpoints} />}
      {(priceRefs.length > 0 || probabilityView) && (
        <FollowUpPriceAndScenarios priceRefs={priceRefs} probabilityView={probabilityView} probabilityWording={probabilityWording} />
      )}
      {valuation && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">本次估值口径（三态：指标缺失 / 指标已取得但未评估合理价值 / 已评估）</span>
          <p className="wb-followup-valuation">
            <span className={`soft-tag ${valuation.tone}`} title={valuation.note}>{valuation.label}</span>
            <b> {valuation.verdictDisplay}</b>
            {valuation.derived && <span className="report-meta">（该记录未带三态字段，措辞按同一规则补齐，未新增判断）</span>}
          </p>
          {valuation.note && <p className="report-meta">{valuation.note}</p>}
        </div>
      )}

      {(supplements.length > 0 || snapshots.length > 0) && (
        <div className="wb-followup-block">
          <span className="wb-counter-label">本次使用的材料</span>
          {supplements.length > 0 && (
            <ul className="wb-followup-supplements">
              {supplements.map((item) => (
                <li key={item.supplement_id}>
                  <span className="soft-tag warn" title="用户补充材料未经独立验证，不能当作已证实事实（方案 §3.2）">
                    {item.trust_label}
                  </span>
                  <b>{item.source_name || "（未填来源）"}</b>
                  {item.event_date && <small> · 事件日期 {item.event_date}</small>}
                  <p>{item.text}</p>
                </li>
              ))}
            </ul>
          )}
          {snapshots.length > 0 && (
            <ul className="wb-followup-snapshots">
              {snapshots.map((item, i) => (
                <li key={`snap-${i}`}>
                  <span className={`soft-tag ${item.status === "ok" ? "mint" : "coral"}`} title={item.status === "ok" ? "本次取数成功" : item.error || "本次取数失败，未用推测填补"}>
                    {item.status === "ok" ? "新增数据" : "取数失败"}
                  </span>
                  <b>{item.label || item.kind}</b>
                  <span className="report-meta">
                    {item.source_name ? ` · 来源 ${item.source_name}` : ""}
                    {item.event_date ? ` · 数据日期 ${item.event_date}` : ""}
                    {item.retrieved_at ? ` · 获取 ${item.retrieved_at}` : ""}
                    {item.trust_label ? ` · ${item.trust_label}` : ""}
                  </span>
                  {(item.supported_claims?.length ?? 0) > 0 && (
                    <span className="report-meta"> · 支撑 {item.supported_claims!.join("、")}</span>
                  )}
                  {item.error && <p className="report-meta coral">{item.error}</p>}
                  {item.url
                    ? <a className="wb-followup-link" href={item.url} target="_blank" rel="noreferrer">{item.url}</a>
                    : <span className="report-meta">（未提供链接）</span>}
                </li>
              ))}
            </ul>
          )}
          {retrieval.failedItems.length > 0 && (
            <p className="report-meta amber" role="note">
              失败项：{retrieval.failedItems.map((item) => `${item.label || item.kind}${item.error ? `（${item.error}）` : ""}`).join("；")}
              ——这些缺口未被推测填补（{retrieval.text}）。
            </p>
          )}
        </div>
      )}
      {(turn.catalyst_chain?.steps?.length ?? 0) > 0 && (
        /* Q13：链条由服务端数环节，模型只解释——缺的那环必须显眼，不能被结论顺带过去。 */
        <div className="wb-followup-block">
          <span className="wb-counter-label">催化证据链（业务量 → 价格与成本 → 利润 → 市场预期 → 价格反应）</span>
          <ul className="answer-list">
            {(turn.catalyst_chain!.steps ?? []).map((step, i) => (
              <li key={`cat-${step.key || i}`}>
                <span
                  className={`soft-tag ${step.status === "evidenced" ? "mint" : "warn"}`}
                  title={step.note || (step.evidence ? `证据：${step.evidence}` : "该环无具体口径")}
                >
                  {step.status === "evidenced" ? "有证据" : "缺证据"}
                </span>
                <b> {step.label || step.key}</b>
                {step.evidence && <span className="report-meta"> · {step.evidence}</span>}
                {step.note && <p className="report-meta amber">{step.note}</p>}
              </li>
            ))}
          </ul>
          {turn.catalyst_chain!.note && <p className="report-meta">{turn.catalyst_chain!.note}</p>}
          {turn.catalyst_chain!.guardrail && (
            <p className="report-meta">护栏：{turn.catalyst_chain!.guardrail}</p>
          )}
        </div>
      )}
      {(turn.answer_flags?.length ?? 0) > 0 && (
        <p className="report-meta amber" role="note">
          <b>服务端标注（不改写正文）：</b>{turn.answer_flags!.join(" ")}
        </p>
      )}
      {sourceCatalog.length > 0 && (
        <SourceCatalogTable rows={sourceCatalog} title="本次分析可用的来源目录（编号 / 名称 / 可用链接 / 数据日期 / 获取时间 / 口径限制）" />
      )}
      {limitations.length > 0 && (
        <ul className="answer-list wb-followup-limits">
          {limitations.map((item, i) => <li key={i}>{item}</li>)}
        </ul>
      )}

      <details className="wb-followup-audit">
        <summary>审计细节（判断影响表 · 模型与提示词版本）</summary>
        <div className="wb-followup-audit-body">
          <p className="report-meta">
            模型 {turn.model || "—"}
            {turn.prompt_version ? ` · 提示词 ${turn.prompt_version}` : " · 提示词版本未记录"}
            {turn.fact_normalizations && turn.fact_normalizations.length > 0
              ? ` · 本次事实归一 ${turn.fact_normalizations.length} 条（主体/事件/日期/来源可信度/与标的关系）`
              : ""}
          </p>
          {turn.fact_normalizations && turn.fact_normalizations.length > 0 && (
            <ul className="answer-list">
              {turn.fact_normalizations.map((fact, i) => (
                <li key={`fact-${i}`}>
                  {fact.supplement_id ? `[${fact.supplement_id}] ` : ""}
                  {fact.subject || "主体未识别"} · {fact.event || "事件未识别"} · {fact.date || "日期未识别"}
                  {fact.date_is_input_time ? "（日期按输入时间填充）" : ""} · 来源 {fact.source_credibility || "未评级"} · {fact.relation || "关系未判定"}
                </li>
              ))}
            </ul>
          )}
          {((turn.affected_claims?.length ?? 0) > 0 || groups.unresolved.length > 0) && (
            <table className="wb-judgement-table">
              <thead><tr><th>编号</th><th>判断名称</th><th>影响</th><th>理由</th></tr></thead>
              <tbody>
                {[...groups.expanded, ...groups.unresolved].map((claim, i) => {
                  const meta = EFFECT_META[claim.effect] ?? EFFECT_META.cannot_judge!;
                  return (
                    <tr key={`audit-${claim.claim_id}-${i}`}>
                      <td>{claim.claim_id || "—"}</td>
                      <td>{claimName(claim) || "—"}</td>
                      <td><span className={`soft-tag ${meta.tone}`} title={meta.hint}>{meta.label}</span></td>
                      <td>{claim.reason || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {(turn.dropped_claim_ids?.length ?? 0) > 0 && (
            <p className="report-meta amber" role="note">
              已剔除 {turn.dropped_claim_ids!.length} 条模型自造的判断编号（{turn.dropped_claim_ids!.join("、")}）——
              原报告不存在这些编号，相关影响不采信。
            </p>
          )}
          {turn.conclusion_change_raw && turn.conclusion_change_raw !== turn.conclusion_change && (
            <p className="report-meta">模型原结论变化词「{turn.conclusion_change_raw}」不在词表内，已按「{change.label}」处理（原值留痕）。</p>
          )}
        </div>
      </details>
    </article>
  );
}

/** 新增验证点表（Q07：日期依据与假设拆分，凭空出现的截止日会被降级并留痕）。 */
function FollowUpWatchpoints({ watchpoints }: { watchpoints: ReportWatchpoint[] }) {
  return (
    <div className="wb-followup-block">
      <span className="wb-counter-label">新增验证点（进入待复盘口径，与原报告同源）</span>
      {/* JV06：与报告验证点**同一个表格组件**、同一份排序与折叠口径，不是各写一套。 */}
      <WatchpointTable
        points={watchpoints}
        withDateBasis
        renderSignal={(item) => {
          const date = watchpointDateView(item);
          const review = watchpointReview(item);
          return (
            <>
              {item.signal || "—"}
              {review.duplicateOf > 0 && (
                /* Q07：重复信号不再各占一条读起来像两件事；条目保留，只点名它是谁的重复。 */
                <span className="soft-tag gray" title="同一信号在上方已出现，合并看待即可">与第 {review.duplicateOf} 条重复</span>
              )}
              {date.assumptions.length > 0 && (
                /* Q07：「量价齐升」与「单票收入不大幅下降」不是一个口径，各自成条才能分别验证。 */
                <ul className="wb-followup-assumptions">
                  {date.assumptions.map((text, k) => <li key={`as-${k}`}>{text}</li>)}
                </ul>
              )}
              {review.note && <p className="report-meta amber" role="note">{review.note}</p>}
            </>
          );
        }}
        renderDateBasis={(item) => {
          const date = watchpointDateView(item);
          return (
            <span title={date.note || undefined}>
              <span className={`soft-tag ${date.tone}`}>{date.kind}</span>
              <span className="report-meta"> {date.basis}</span>
              {date.demotedFrom && <span className="report-meta amber"> 原值 {date.demotedFrom} 已降级</span>}
            </span>
          );
        }}
      />
      {watchpoints.some((item) => watchpointDateView(item).note) && (
        <p className="report-meta">
          日期口径说明：预计时间（交易日历推算）与事实时间（已披露日程 / 输入材料）分列；
          无可靠依据的具体日期一律降级为事件锚定，原值留痕。
        </p>
      )}
    </div>
  );
}

/** 价位时点表 + 情景倾向口径（Q06/Q08：历史快照不冒充实时阈值，未校准倾向不写成上涨概率）。 */
function FollowUpPriceAndScenarios({
  priceRefs,
  probabilityView,
  probabilityWording,
}: {
  priceRefs: NonNullable<AnalysisTurn["price_refs"]>;
  probabilityView: AnalysisTurn["probability_view"] | null;
  probabilityWording: { column: string; note: string; calibrated: boolean };
}) {
  const boundsOf = (name: string) => (probabilityView?.scenarios?.find((item) => item.name === name)?.bounds ?? []);
  return (
    <div className="wb-followup-block">
      {priceRefs.length > 0 && (
        <>
          <span className="wb-counter-label">本次引用的价位与时点</span>
          <table className="wb-judgement-table">
            <thead>
              <tr><th>价位</th><th>是什么位</th><th>数据截至</th><th>适用窗口</th><th>口径</th></tr>
            </thead>
            <tbody>
              {priceRefs.map((ref, i) => {
                const cells = priceRefCells(ref);
                return (
                  <tr key={`pr-${i}`} title={cells.note || undefined}>
                    <td>{cells.level}</td>
                    <td>{cells.role}</td>
                    <td>{cells.asOf}</td>
                    <td>{cells.window}</td>
                    <td>
                      <span className={`soft-tag ${ref.snapshot_only ? "warn" : "mint"}`} title={cells.note || undefined}>{cells.status}</span>
                      {cells.note && <span className="report-meta"> {cells.note}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}
      {probabilityView && (probabilityView.scenarios?.length ?? 0) > 0 && (
        <>
          <span className="wb-counter-label">{probabilityWording.column}（不是上涨概率）</span>
          <table className="wb-judgement-table">
            <thead>
              <tr><th>情景</th><th>{probabilityWording.column}</th><th>区间下界</th><th>区间上界</th></tr>
            </thead>
            <tbody>
              {(probabilityView.scenarios ?? []).map((item, i) => {
                const low = boundsOf(item.name).find((bound) => bound.side === "low");
                const high = boundsOf(item.name).find((bound) => bound.side === "high");
                const cell = (bound: (typeof low)) => {
                  if (!bound || bound.value == null) return <td>—</td>;
                  const labeled = referenceBoundLabel(bound);
                  return (
                    <td title={labeled.note || undefined}>
                      {labeled.isReference && <span className="soft-tag warn">{labeled.label}</span>}
                      {" "}{labeled.isReference ? "" : labeled.label} {String(bound.value)}
                    </td>
                  );
                };
                return (
                  <tr key={`pv-${i}`}>
                    <td>{item.name || "情景"}</td>
                    <td title={probabilityView.note || probabilityWording.note}>{item.probability_display || item.probability_level || "未给出"}</td>
                    {cell(low)}
                    {cell(high)}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {(probabilityView.reference_bounds?.length ?? 0) > 0 && (
            <p className="report-meta amber" role="note">
              参考位提示：{probabilityView.reference_bounds!.map((item) => `${item.level ?? "—"}（${item.scenario}）`).join("、")}
              只凭历史高点/低点身份进入区间，未给出推导过程——按参考压力/支撑位看待，不作为未来窗口的目标价。
            </p>
          )}
        </>
      )}
      {probabilityView?.note && <p className="report-meta">{probabilityView.note}</p>}
    </div>
  );
}

/** 来源目录（Q09）复用 panels.SourceCatalogTable——研报证据区与追问附录必须是同一张表，
 * 否则一处改了链接口径、另一处还在猜 URL。 */

/** 常用追问模板（方案 §3.1 的问题类型）。 */
const QUICK_QUESTIONS: { label: string; text: string }[] = [
  { label: "追问核心判断依据", text: "逐条检查核心判断的依据是否充分：哪条判断最不可靠？需要补什么证据？" },
  { label: "只看 3~5 年价值逻辑", text: "只保留 3~5 年价值投资逻辑（商业模式、护城河、正常化盈利、财务安全），忽略短线技术面，重新评估结论是否成立。" },
  { label: "最不可靠的结论", text: "这份报告里最不可靠的结论是什么？它依赖哪些来源与假设，缺什么证据才能把它说死？" },
  { label: "补充新信息重新评估", text: "请结合下方补充材料重新评估：受影响的核心判断、结论是否变化、以及需要新增的验证点。" },
];

/** 追问草稿（A14：按报告 id 保存，切换页签/报告不丢）。
 * v39 Q10：草稿带 `mode`——两种入口的成本与数据范围不同，切回来不该被悄悄改成补充研究。 */
export interface FollowUpDraft {
  question: string;
  supplements: FollowUpSupplementInput[];
  /** interpret=解读本报告（默认）| supplement_research=补充研究。 */
  mode?: string;
}

export const EMPTY_FOLLOWUP_DRAFT: FollowUpDraft = { question: "", supplements: [], mode: DEFAULT_FOLLOWUP_MODE };

/**
 * v33 A13（2026-09-14 路线图）：追问采用**列表与详情**——
 * 列表显示问题/时间/结论变化，详情先回答再看依据；默认选中最新一条，其余不同时展开。
 * A14：草稿按报告 id 由调用方保存（切页签/切报告不丢）；迟到响应由调用方按报告 id 隔离。
 * 宽窗双栏（列表｜详情），窄窗单列（列表 → 详情）。
 */
export function FollowUpPanel({
  reportId,
  turns,
  loading,
  busy,
  error,
  draft,
  onDraftChange,
  onSubmit,
}: {
  reportId: string;
  turns: AnalysisTurn[];
  loading: boolean;
  busy: boolean;
  error: string | null;
  /** 当前报告的追问草稿（受控，A14）。 */
  draft: FollowUpDraft;
  onDraftChange: (next: FollowUpDraft) => void;
  /** 提交追问；返回 false = 未完成（调用方负责错误提示与草稿保留）。
   * `mode` = 本次入口（Q10）：解读本报告 / 补充研究，缺省由调用方按 interpret 处理。 */
  onSubmit: (question: string, supplements: FollowUpSupplementInput[], mode: string) => Promise<boolean>;
}) {
  // 默认选中最新一条（turns 为时间正序，最后一条最新）；用户点选后按选择展示。
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const latest = turns.length > 0 ? turns[turns.length - 1]! : null;
  const selected = turns.find((turn) => turn.analysis_turn_id === selectedId) ?? latest;
  const mode = draft.mode && FOLLOWUP_MODE_META[draft.mode] ? draft.mode : DEFAULT_FOLLOWUP_MODE;

  function updateSupplement(index: number, patch: Partial<FollowUpSupplementInput>): void {
    onDraftChange({
      ...draft,
      supplements: draft.supplements.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    });
  }

  async function submit(): Promise<void> {
    if (!draft.question.trim() || busy) return;
    const ok = await onSubmit(draft.question.trim(), draft.supplements, mode);
    // 清空问题与材料，但**保留入口选择**：连续追问通常沿用同一模式，重选一次反而容易误触补充研究。
    if (ok) onDraftChange({ question: "", supplements: [], mode });
  }

  return (
    <div className="wb-followup" data-report-id={reportId}>
      <div className="wb-followup-compose">
        {/* Q10：两种入口先选再问——解读模式不声称获取新事实，补充研究才会去取数并如实回报失败项。 */}
        <div className="wb-followup-mode" role="radiogroup" aria-label="追问入口">
          {Object.entries(FOLLOWUP_MODE_META).map(([key, meta]) => (
            <button
              key={key}
              type="button"
              role="radio"
              aria-checked={mode === key}
              className={`tag-button ${mode === key ? "active" : ""}`}
              title={meta.hint}
              disabled={busy}
              onClick={() => onDraftChange({ ...draft, mode: key })}
            >
              {meta.label}
            </button>
          ))}
          <span className="wb-followup-mode-hint" title={FOLLOWUP_MODE_META[mode]!.hint}>
            {mode === "interpret"
              ? "只依据原报告与你的补充材料作答：不更新行情、不获取新事实。"
              : "本次会重新获取行情/公告等数据：成功与失败都如实列出，失败不会被写成研究完成。"}
          </span>
        </div>
        <div className="wb-quick" role="group" aria-label="常用追问模板">
          {QUICK_QUESTIONS.map((item) => (
            <button
              key={item.label}
              type="button"
              className="tag-button"
              title={item.text}
              disabled={busy}
              onClick={() => onDraftChange({ ...draft, question: item.text })}
            >
              {item.label}
            </button>
          ))}
        </div>
        <textarea
          className="wb-followup-question-input"
          value={draft.question}
          placeholder={"追问本报告（原报告不可变，结果保存为分析附录）…\n例：如果美联储继续加息，估值和盈利会怎样？"}
          rows={3}
          maxLength={500}
          disabled={busy}
          onChange={(event) => onDraftChange({ ...draft, question: event.target.value })}
        />
        {draft.supplements.map((item, index) => (
          <div key={index} className="wb-followup-supplement-row">
            <div className="wb-followup-supplement-grid">
              <textarea
                value={item.text ?? ""}
                placeholder="补充材料原文（如新闻、公告、宏观数据描述…默认按「用户补充材料/未独立验证」处理）"
                rows={2}
                maxLength={4000}
                disabled={busy}
                onChange={(event) => updateSupplement(index, { text: event.target.value })}
              />
              <input
                value={item.source_name ?? ""}
                placeholder="来源名称（如 财联社 / 公司公告）"
                maxLength={120}
                disabled={busy}
                onChange={(event) => updateSupplement(index, { source_name: event.target.value })}
              />
              <input
                value={item.url ?? ""}
                placeholder="URL（可选）"
                maxLength={500}
                disabled={busy}
                onChange={(event) => updateSupplement(index, { url: event.target.value })}
              />
              <input
                value={item.event_date ?? ""}
                placeholder="事件日期 YYYY-MM-DD（可选）"
                maxLength={10}
                disabled={busy}
                onChange={(event) => updateSupplement(index, { event_date: event.target.value })}
              />
            </div>
            <button
              type="button"
              className="ghost-btn"
              disabled={busy}
              aria-label="移除这条补充材料"
              onClick={() => onDraftChange({ ...draft, supplements: draft.supplements.filter((_, i) => i !== index) })}
            >移除</button>
          </div>
        ))}
        <div className="wb-followup-actions">
          {draft.supplements.length < 3 && (
            <button
              type="button"
              className="ghost-btn"
              disabled={busy}
              onClick={() => onDraftChange({ ...draft, supplements: [...draft.supplements, { text: "" }] })}
            >+ 添加补充材料（{draft.supplements.length}/3）</button>
          )}
          <button
            type="button"
            className="ghost-btn primary"
            disabled={busy || !draft.question.trim()}
            title="原报告不可变：本次追问会生成一条追加的分析附录（含受影响判断、结论变化与新增验证点）"
            onClick={() => void submit()}
          >{busy ? "分析中…" : "提交追问"}</button>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        <p className="report-meta">
          补充材料会先经事实归一化（主体/事件/日期/来源可信度/与标的关系），再进入影响分析；
          它不是已验证的公司事实，结论以「用户补充材料/未独立验证」标签呈现。
        </p>
      </div>

      <div className="wb-followup-body">
        <nav className="wb-followup-list" aria-label="追问附录列表">
          <span className="wb-counter-label">附录（{turns.length} 条 · 不可变）</span>
          {loading && <p className="report-meta">正在载入追问附录…</p>}
          {!loading && turns.length === 0 && (
            <p className="wb-history-empty">还没有追问。围绕某个数字、来源或核心判断提问，或粘贴一条外部信息重新评估。</p>
          )}
          <ul>
            {turns.map((turn) => {
              const change = CONCLUSION_CHANGE_META[turn.conclusion_change] ?? CONCLUSION_CHANGE_META.undetermined!;
              const isActive = selected?.analysis_turn_id === turn.analysis_turn_id;
              return (
                <li key={turn.analysis_turn_id}>
                  <button
                    type="button"
                    className={`wb-followup-list-item ${isActive ? "active" : ""}`}
                    aria-current={isActive ? "true" : undefined}
                    onClick={() => setSelectedId(turn.analysis_turn_id)}
                  >
                    <span className="soft-tag blue">#{turn.turn_index}</span>
                    <span className={`soft-tag ${change.tone}`}>{change.label}</span>
                    <span className="wb-followup-list-question">{turn.question}</span>
                    <small>{new Date(turn.created_at).toLocaleString()}</small>
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>
        <div className="wb-followup-detail" aria-live="polite">
          {selected ? (
            <AnalysisTurnCard turn={selected} />
          ) : (
            !loading && turns.length === 0 && (
              <p className="report-meta">提交追问后，附录会出现在这里（先回答，再看受影响判断、情景、材料与验证点）。</p>
            )
          )}
        </div>
      </div>
    </div>
  );
}
