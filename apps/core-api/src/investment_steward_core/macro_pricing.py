"""市场定价层（D-14 首批）。

方案 §2「市场定价层」：利率期货隐含的加降息概率、汇率与股指趋势作为「市场怎么看」
对照层。D-14 拍板：M4 先用 FRED 公开序列做代理指标，CME FedWatch 页面解析做成
可开关增强（后续）。

铁律（ADR-0006）：series_id 只写 100% 确定存在的 FRED 公开序列（H.10 汇率、
联邦基金目标区间、盈亏平衡通胀、VIX、S&P 500、ECB 存款便利利率）；不确定来源
（各国股指、政策利率、隐含概率）一律 `pending` 并写明待接原因，绝不编造。
序列缓存于 macro_cache 但用独立 `pricing:` 前缀，不与指标层混存；日度序列
24h 过期重拉（ttl_hours），拉取失败回退旧缓存（as_of 如实标注）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from investment_steward_core.domain import MacroPricingRow, MacroPricingSnapshot

if TYPE_CHECKING:
    from investment_steward_core.credential_store import CredentialStore
    from investment_steward_core.storage import Database

PRICING_TTL_HOURS = 24.0
FRED_SOURCE = "FRED（公开接口）"

# 各国市场定价代理序列（D-14 首批）：只列确定存在的 FRED 序列，其余 pending。
PRICING_DEFS: dict[str, list[dict[str, str]]] = {
    "us": [
        {"key": "ff_target_upper", "label": "联邦基金目标区间上限", "kind": "policy_rate",
         "fred": "DFEDTARU", "unit": "%", "note": "FRED DFEDTARU，政策利率轨迹（日度）"},
        {"key": "breakeven_10y", "label": "10 年盈亏平衡通胀", "kind": "inflation_expectation",
         "fred": "T10YIE", "unit": "%", "note": "FRED T10YIE，市场隐含通胀预期（日度）"},
        {"key": "sp500", "label": "标普 500 收盘", "kind": "equity",
         "fred": "SP500", "unit": "指数", "note": "FRED SP500（日度）"},
        {"key": "vix", "label": "VIX 波动率指数", "kind": "risk",
         "fred": "VIXCLS", "unit": "指数", "note": "FRED VIXCLS，CBOE 波动率（日度）"},
        {"key": "rate_odds", "label": "加降息隐含概率", "kind": "policy_odds",
         "note": "CME FedWatch 公开页解析（D-14 可开关增强），后续接入"},
    ],
    "eu": [
        {"key": "ecb_deposit_rate", "label": "ECB 存款便利利率", "kind": "policy_rate",
         "fred": "ECBDFR", "unit": "%", "note": "FRED ECBDFR（日度）"},
        {"key": "fx_eur", "label": "欧元兑美元", "kind": "fx",
         "fred": "DEXUSEU", "unit": "USD", "note": "FRED DEXUSEU，美联储 H.10（日度）"},
        {"key": "equity_eu", "label": "欧元区股指", "kind": "equity",
         "note": "FRED 无欧元区股指公开序列，待接数据源"},
    ],
    "jp": [
        {"key": "fx_jpy", "label": "日元兑美元", "kind": "fx",
         "fred": "DEXJPUS", "unit": "USD", "note": "FRED DEXJPUS，美联储 H.10（日度）"},
        {"key": "policy_rate_jp", "label": "日银政策利率", "kind": "policy_rate",
         "note": "待接数据源（日银公开），后续适配器接入"},
        {"key": "equity_jp", "label": "日经 225", "kind": "equity",
         "note": "series_id 待核实（不确定不接入），待接数据源"},
    ],
    "cn": [
        {"key": "fx_cny", "label": "人民币兑美元", "kind": "fx",
         "fred": "DEXCHUS", "unit": "USD", "note": "FRED DEXCHUS，美联储 H.10（日度）"},
        {"key": "policy_rate_cn", "label": "政策利率（OMO/MLF）", "kind": "policy_rate",
         "note": "待接数据源（央行公开），后续适配器接入"},
        {"key": "equity_cn", "label": "A 股宽基指数", "kind": "equity",
         "note": "series_id 待核实（不确定不接入），待接数据源"},
    ],
    "in": [
        {"key": "fx_inr", "label": "印度卢比兑美元", "kind": "fx",
         "fred": "DEXINUS", "unit": "USD", "note": "FRED DEXINUS，美联储 H.10（日度）"},
        {"key": "policy_rate_in", "label": "RBI 回购利率", "kind": "policy_rate",
         "note": "待接数据源（RBI 公开），后续适配器接入"},
        {"key": "equity_in", "label": "印度股指", "kind": "equity",
         "note": "series_id 待核实（不确定不接入），待接数据源"},
    ],
}

REGION_LABELS: dict[str, str] = {"us": "美国", "cn": "中国", "eu": "欧元区", "jp": "日本", "in": "印度"}


def trend_label(observations: list[dict[str, Any]], *, window: int = 5, threshold: float = 0.002) -> tuple[str | None, dict[str, Any] | None]:
    """近 window 期均值 vs 前 window 期均值的方向（最新在前）。

    返回 (趋势标签, 对照值行)；观测不足 2*window 时趋势返回 None（不编造）。
    阈值为相对变动 ±0.2%（透明规则，随代码发布可复算）。
    """
    if len(observations) < window * 2:
        return None, (observations[-1] if observations else None)
    recent = sum(row["value"] for row in observations[:window]) / window
    prior = sum(row["value"] for row in observations[window : window * 2]) / window
    if prior == 0:
        return None, None
    change = (recent - prior) / abs(prior)
    label = "上升" if change > threshold else ("下降" if change < -threshold else "持平")
    return label, None


def get_region_pricing(
    db: Database,
    credential: CredentialStore,
    region: str,
) -> MacroPricingSnapshot | None:
    """组装市场定价对照快照；未知 region 返回 None。"""
    if region not in PRICING_DEFS:
        return None
    from investment_steward_core import macro_feed

    rows: list[MacroPricingRow] = []
    pending_names: list[str] = []
    for definition in PRICING_DEFS[region]:
        if not definition.get("fred"):
            pending_names.append(definition["label"])
            rows.append(
                MacroPricingRow(
                    key=definition["key"], label=definition["label"], kind=definition["kind"],
                    status="pending", note=definition["note"],
                )
            )
            continue
        payload = macro_feed._resolve_series(
            db, credential, f"pricing:{region}:{definition['key']}", definition["fred"],
            ttl_hours=PRICING_TTL_HOURS,
        )
        if payload is None:
            reason = (
                "凭据库无 FRED key（设置 → 数据源与密钥录入）"
                if not credential.get("macro_data_key")
                else "拉取失败，稍后重试"
            )
            pending_names.append(definition["label"])
            rows.append(
                MacroPricingRow(
                    key=definition["key"], label=definition["label"], kind=definition["kind"],
                    status="pending", note=f"{definition['note']}；{reason}",
                )
            )
            continue
        observations = payload["observations"]
        trend, _ = trend_label(observations)
        ref_row = observations[20] if len(observations) > 20 else observations[-1]
        rows.append(
            MacroPricingRow(
                key=definition["key"], label=definition["label"], kind=definition["kind"],
                status="ok", latest=observations[0]["value"], obs_date=observations[0]["obs_date"],
                unit=definition["unit"], trend_5d=trend,
                ref_value=ref_row["value"], ref_date=ref_row["obs_date"],
                as_of=payload["as_of"], source=FRED_SOURCE,
                dataset_version=payload["dataset_version"], note=definition["note"],
            )
        )
    return MacroPricingSnapshot(
        region=region, label=f"{REGION_LABELS[region]} · 市场定价对照",
        rows=rows,
        degraded_reason="；".join(f"{name} 待接" for name in pending_names) or None,
    )
