"""量化研究 / 策略分享池骨架（G6-2 预留端点族）。

分享池整体处于「待决策、落地前必须走 ADR 变更」状态（见 strategy-sharing-pool-plan）。
本模块只提供结构完整的空态响应：阶段 A（参数集）未开放，任何端点都不返回虚构制品、
不按收益率排序、不入池（ADR 合规红线）。真实制品与回测运行在 ADR 通过后填入。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArtifactMetric(BaseModel):
    """制品指标：每个数字都挂口径。"""

    label: str
    value: str
    caliber: str  # backtest | paper | simulated | self_reported | broker_verified


class ArtifactConsistency(BaseModel):
    """作者声称 vs 本地复现。"""

    author: str | None = None
    local: str | None = None
    diff: str | None = None
    status: str | None = None
    note: str = ""


class ArtifactParam(BaseModel):
    k: str
    v: str
    range: str
    note: str = ""


class RunNote(BaseModel):
    id: str
    who: str = "本地"
    env: str = ""
    result: str = ""
    status: str = ""


class ArtifactCard(BaseModel):
    """对齐设计稿 ARTIFACTS 字段，供前端分享池/详情/派生渲染（当前恒为空集）。"""

    id: str
    name: str
    type: str = "parameter_set"  # parameter_set | strategy_pack | model_weights
    author: str = ""
    ver: str = ""
    hash: str = ""
    style: str = "mean_reversion"  # trend_following | mean_reversion | rotation | multifactor
    instruments: str = "broad_etf"  # broad_etf | industry_etf | stock
    sample_months: int | None = None
    desc: str = ""
    updated: str = ""
    license: str = ""
    forks: int = 0
    mine: bool = False
    locked: bool = False
    caliber_score: str = "0/5"
    sample_out: str = ""
    repro: str = "未复现"
    metrics: list[ArtifactMetric] = Field(default_factory=list)
    live: ArtifactMetric | None = None
    consistency: ArtifactConsistency | None = None
    derived_from: str | None = None
    lineage: list[dict[str, str]] = Field(default_factory=list)
    runs: list[RunNote] = Field(default_factory=list)
    params: list[ArtifactParam] = Field(default_factory=list)


class ArtifactPoolView(BaseModel):
    """分享池视图响应：available=False 表示阶段未开放，不编造制品。"""

    available: bool
    stage: str  # A | B | C | D
    stage_label: str
    artifacts: list[ArtifactCard] = Field(default_factory=list)
    degraded_reason: str | None = None
    notice: str = ""


class ArtifactLineageView(BaseModel):
    """制品派生谱系响应：available=False 表示阶段未开放、无任何制品。"""

    available: bool
    artifact_id: str
    lineage: list[dict[str, str]] = Field(default_factory=list)


_STAGE_LABELS: dict[str, str] = {
    "A": "参数集（纯 JSON，官方内核解释执行）",
    "B": "策略包（发布者签名 + 受限执行器）",
    "C": "实盘核验（自报与对账单核验两级，只作筛选不作排序）",
    "D": "模型权重（线性模型，训练快照可复算）",
}

_NOTICE = (
    "分享池只展示可复现的制品，不生成收益排名。"
    "每个数字都要求挂口径，默认排序不含收益率；"
    "完整制品卡（跨用户分享与对比）随后续版本开放。"
)


def empty_pool_view(stage: str = "A") -> ArtifactPoolView:
    return ArtifactPoolView(
        available=False,
        stage=stage,
        stage_label=_STAGE_LABELS.get(stage, stage),
        artifacts=[],
        degraded_reason=(
            f"阶段 {stage}（{_STAGE_LABELS.get(stage, '规划中')}）的完整制品卡随后续版本开放；"
            "当前分享池展示的均为本机可复现制品。"
        ),
        notice=_NOTICE,
    )