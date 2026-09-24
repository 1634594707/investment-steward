import { useState } from "react";
import type { InvestorProfile } from "@investment-steward/domain-contracts";
import { useFocusTrap } from "./useFocusTrap";

/** 首次引导提交负载：与后端 InvestorProfileRequest 同源（investment_goal / horizon_years / liquidity_needs / knowledge_self_assessment / markets_and_assets / consent / privacy_settings）。 */
export interface InvestorOnboardingInput {
  investment_goal: string;
  horizon_years: number | null;
  liquidity_needs: string;
  knowledge_self_assessment: string;
  markets_and_assets: string[];
  consent: Record<string, boolean>;
  privacy_settings: Record<string, unknown>;
}

interface Props {
  saving: boolean;
  error: string | null;
  /** 编辑模式：传入已保存画像则预填；首次引导不传。组件随弹窗卸载，状态自然重置。 */
  initial?: InvestorProfile | null;
  onSubmit: (input: InvestorOnboardingInput) => void;
  onDismiss: () => void;
}

/** 可勾选的市场/资产类别（与后端取值自由文本，前端仅提供快捷集合）。 */
const MARKET_OPTIONS: { key: string; label: string }[] = [
  { key: "cn_stock", label: "A 股个股" },
  { key: "cn_etf", label: "A 股 ETF / 指数" },
  { key: "us_stock", label: "美港股" },
  { key: "bond", label: "债券 / 固收" },
  { key: "commodity", label: "商品 / 贵金属" },
  { key: "fund", label: "公募基金" },
];

const KNOWLEDGE_OPTIONS: { key: string; label: string }[] = [
  { key: "beginner", label: "入门：理解基本概念" },
  { key: "intermediate", label: "中级：能读财报与逻辑" },
  { key: "advanced", label: "进阶：熟悉组合与风控" },
];

/**
 * E3 首次引导：Core 返回 /investor/profile 为 null 时弹出，引导用户补齐投资者画像；
 * 传入 initial 时作为「编辑画像」弹窗复用（预填已保存值）。
 * 画像仅供研究链路作背景参考，绝不直接生成买卖建议（ADR：画像不产生建议）。
 */
export function InvestorOnboarding({ saving, error, initial, onSubmit, onDismiss }: Props) {
  const [goal, setGoal] = useState(initial?.investment_goal ?? "");
  const [horizon, setHorizon] = useState(initial?.horizon_years != null ? String(initial.horizon_years) : "");
  const [liquidity, setLiquidity] = useState(initial?.liquidity_needs ?? "");
  const [knowledge, setKnowledge] = useState(initial?.knowledge_self_assessment ?? "");
  const [markets, setMarkets] = useState<Set<string>>(new Set(initial?.markets_and_assets ?? []));
  const [consentStorage, setConsentStorage] = useState(initial?.consent?.profile_local_storage ?? false);
  const [consentContext, setConsentContext] = useState(initial?.consent?.research_context_use ?? false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const dialogRef = useFocusTrap<HTMLDivElement>(true);

  function toggleMarket(key: string) {
    setMarkets((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function submit() {
    const parsedHorizon = horizon.trim() === "" ? null : Number(horizon);
    if (parsedHorizon !== null && (!Number.isFinite(parsedHorizon) || parsedHorizon <= 0)) {
      setValidationError("投资年限必须是大于 0 的数字，或留空。");
      return;
    }
    setValidationError(null);
    onSubmit({
      investment_goal: goal.trim(),
      horizon_years: parsedHorizon,
      liquidity_needs: liquidity.trim(),
      knowledge_self_assessment: knowledge,
      markets_and_assets: [...markets],
      consent: { profile_local_storage: consentStorage, research_context_use: consentContext },
      // 编辑模式下保留既有隐私设置，避免整表覆盖时丢失。
      privacy_settings: initial?.privacy_settings ?? {},
    });
  }

  return (
    <div className="confirm-overlay" onClick={onDismiss}>
      <aside
        className="confirm-dialog investor-onboarding"
        role="dialog"
        aria-modal="true"
        aria-label={initial ? "编辑投资者画像" : "投资者画像首次引导"}
        onClick={(event) => event.stopPropagation()}
        ref={dialogRef}
      >
        <header className="confirm-head">
          <span className="section-kicker">{initial ? "INVESTOR PROFILE · EDIT" : "INVESTOR PROFILE · FIRST RUN"}</span>
          <h3>{initial ? "更新你的画像" : "先让我了解一下你"}</h3>
          <p>这些信息只保存在本机，作为研究答卷的背景参考，用于让内容更贴合你的情况；<b>不会用于生成任何买卖建议</b>。任何字段都可留空。</p>
        </header>

        <div className="onboard-fields">
          <label className="field-label">
            <span>投资目标</span>
            <textarea
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="如：长期稳健增值，跑赢通胀；3–5 年内有购房首付需求……"
              rows={3}
            />
          </label>

          <div className="field-grid">
            <label className="field-label">
              <span>投资年限（年）</span>
              <input
                type="number"
                min={1}
                value={horizon}
                onChange={(event) => setHorizon(event.target.value)}
                placeholder="如：10"
              />
            </label>
            <label className="field-label">
              <span>投资知识自评</span>
              <select value={knowledge} onChange={(event) => setKnowledge(event.target.value)}>
                <option value="">选择……</option>
                {KNOWLEDGE_OPTIONS.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="field-label">
            <span>流动性需求</span>
            <textarea
              value={liquidity}
              onChange={(event) => setLiquidity(event.target.value)}
              placeholder="如：每年需要取用一部分用于生活支出……"
              rows={2}
            />
          </label>

          <fieldset className="field-set">
            <legend className="field-label"><span>关注的市场 / 资产类别（可多选）</span></legend>
            <div className="chip-row">
              {MARKET_OPTIONS.map((item) => {
                const active = markets.has(item.key);
                return (
                  <button
                    key={item.key}
                    type="button"
                    className={`tag-button${active ? " active" : ""}`}
                    aria-pressed={active}
                    onClick={() => toggleMarket(item.key)}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </fieldset>

          <fieldset className="field-set">
            <legend className="field-label"><span>授权与知悉</span></legend>
            <label className="consent-line">
              <input type="checkbox" checked={consentStorage} onChange={(event) => setConsentStorage(event.target.checked)} />
              <span>我同意画像仅保存在本机磁盘（不离开设备）</span>
            </label>
            <label className="consent-line">
              <input type="checkbox" checked={consentContext} onChange={(event) => setConsentContext(event.target.checked)} />
              <span>我知悉研究答卷会引用画像作为背景上下文</span>
            </label>
          </fieldset>
        </div>

        {validationError && <div className="onboard-error">{validationError}</div>}
        {error && <div className="onboard-error">{error}</div>}

        <footer className="confirm-foot">
          <button className="secondary-button" onClick={onDismiss} disabled={saving}>
            {initial ? "取消" : "稍后再说"}
          </button>
          <button className="primary-button confirm-primary" onClick={submit} disabled={saving}>
            {saving ? "保存中…" : "保存画像"}
          </button>
        </footer>
      </aside>
    </div>
  );
}
