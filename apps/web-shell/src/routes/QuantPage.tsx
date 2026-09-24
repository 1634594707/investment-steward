import { useEffect, useMemo, useRef, useState } from "react";
import type { ArtifactCard, ArtifactPoolView } from "@investment-steward/domain-contracts";
import type { AppView } from "../shell/nav";
import type {
  QuantFactorItem,
  QuantFactorMineResult,
  QuantPackRunResult,
  QuantParameterSet,
  QuantReplayResult,
  QuantStageInfo,
  QuantStrategyPack,
  QuantTrackRecord,
} from "../hooks/useQuant";

import "./quant.css";

import { createCoreClient } from "../state/coreClient";
import { useQuant } from "../hooks/useQuant";
import { ExperimentSection } from "./ExperimentSection";

interface Props {
  isDemo: boolean;
  marketPluginEnabled: boolean;
  /** B2：当前视图是否为量化页（KeepAlive 常驻挂载）。 */
  active: boolean;
  onNavigate: (view: AppView) => void;
}

/** 阶段条:开放状态与原因以服务端 /quant/stages 为准,以下为名称与回退描述。 */
const STAGES = [
  { key: "A", name: "阶段 A", title: "参数集", desc: "纯 JSON，无代码。官方内核解释执行，确定性回放。" },
  { key: "B", name: "阶段 B", title: "策略包", desc: "仅运行发布者签名制品;受限执行器阻断文件/网络/子进程访问。" },
  { key: "C", name: "阶段 C", title: "实盘核验", desc: "自报与对账单核验两级,只作筛选、不作排序。" },
  { key: "D", name: "阶段 D", title: "模型权重", desc: "线性模型权重纯 JSON;通用模型权重需更强执行隔离,后续开放。" },
] as const;

const WORKFLOW = [{ key: "data", label: "数据" }, { key: "factor", label: "因子" }, { key: "strategy", label: "策略" }, { key: "backtest", label: "回测" }, { key: "conclusion", label: "结论" }] as const;
type Workflow = typeof WORKFLOW[number]["key"];

type QuantTab = "pool" | "mine" | "lineage";
type TypeFilter = "all" | ArtifactCard["type"];

const TYPE_LABEL: Record<ArtifactCard["type"], string> = {
  parameter_set: "参数集",
  strategy_pack: "策略包",
  model_weights: "模型权重",
};
const STYLE_LABEL: Record<string, string> = {
  trend_following: "动量/趋势",
  mean_reversion: "均值回归",
  rotation: "轮动",
  multifactor: "多因子",
};
const INST_LABEL: Record<string, string> = {
  broad_etf: "宽基 ETF",
  industry_etf: "行业 ETF",
  stock: "个股",
};

/** 筛选字典(策略风格/标的/复现状态/许可/样本外长度)。 */
const FILTER_GROUPS: Array<{ key: string; label: string; options: Array<[string, string]> }> = [
  { key: "style", label: "策略风格", options: Object.entries(STYLE_LABEL) },
  { key: "inst", label: "标的", options: Object.entries(INST_LABEL) },
  {
    key: "repro",
    label: "复现状态",
    options: [["reproduced", "已本地复现"], ["unverified", "未复现"], ["locked", "不可安装"]],
  },
  {
    key: "license",
    label: "许可",
    options: [["MIT", "MIT"], ["CC", "CC"], ["custom", "自定义"]],
  },
  {
    key: "sample",
    label: "样本外长度",
    options: [["ge12", "≥12 月"], ["ge6", "6–12 月"], ["lt6", "<6 月"]],
  },
];

export function QuantPage({ isDemo, marketPluginEnabled, active, onNavigate }: Props) {
  // B2：量化研究域数据自取（回调 props 已清零；别名对齐既有局部命名，页面主体零改动）。
  const client = useMemo(() => createCoreClient(), []);
  const {
    quantPool: pool, fetchFactorMine: onFactorMine, fetchQuantParameterSets: onListParameterSets,
    publishQuantParameterSet: onPublishParameterSet, forkQuantParameterSet: onForkParameterSet,
    fetchQuantReplay: onReplayParameterSet, fetchQuantLineage: onLineageParameterSet,
    fetchQuantStages: onFetchStages, trainQuantModel: onTrainModel,
    importQuantStrategyPack: onImportStrategyPack, fetchQuantStrategyPacks: onFetchStrategyPacks,
    runQuantStrategyPack: onRunStrategyPack, addQuantTrackRecord: onAddTrackRecord,
    verifyQuantTrackRecord: onVerifyTrackRecord,
  } = useQuant(client);
  // 阶段 3 本地实验面板走同一 client 通道（原 props.coreRequest 语义）。
  const coreRequest = client.request;
  const [workflow, setWorkflow] = useState<Workflow>("data");
  const detailSequence = useRef(0);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogRevision, setCatalogRevision] = useState(0);
  const [tab, setTab] = useState<QuantTab>("pool");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string[]>>({});
  const [detail, setDetail] = useState<ArtifactCard | null>(null);
  // AlphaMaster 补充:确定性因子挖掘状态
  const [mineSymbol, setMineSymbol] = useState("510300");
  const [mineBusy, setMineBusy] = useState(false);
  const [mineResult, setMineResult] = useState<QuantFactorMineResult | null>(null);
  const [mineError, setMineError] = useState<string | null>(null);
  // A4-1 分享池阶段 A 通道:本机参数集(列表/发布/Fork/详情回放)
  const [psList, setPsList] = useState<QuantParameterSet[] | null>(null);
  const [psBusyId, setPsBusyId] = useState<string | null>(null);
  const [psError, setPsError] = useState<string | null>(null);
  const [publishBusy, setPublishBusy] = useState<string | null>(null);
  const [publishNotice, setPublishNotice] = useState<string | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [psDetail, setPsDetail] = useState<{ set: QuantParameterSet; replay: QuantReplayResult | null; lineage: QuantParameterSet[] | null; error: string | null } | null>(null);
  const [psDetailLoading, setPsDetailLoading] = useState(false);
  // 阶段条:开放状态与原因来自服务端;未取到时使用本文件回退描述
  const [stages, setStages] = useState<QuantStageInfo[] | null>(null);
  // 阶段 D(限定形态):线性模型训练
  const [modelLambda, setModelLambda] = useState("1.0");
  const [modelBusy, setModelBusy] = useState(false);
  const [modelResult, setModelResult] = useState<QuantParameterSet | null>(null);
  const [modelError, setModelError] = useState<string | null>(null);
  // 阶段 B:策略包导入(验签)与受限执行器回测
  const [packs, setPacks] = useState<QuantStrategyPack[] | null>(null);
  const [packManifest, setPackManifest] = useState("");
  const [packBusy, setPackBusy] = useState(false);
  const [packNotice, setPackNotice] = useState<string | null>(null);
  const [packError, setPackError] = useState<string | null>(null);
  const [packRunBusyId, setPackRunBusyId] = useState<string | null>(null);
  const [packRunResult, setPackRunResult] = useState<QuantPackRunResult | null>(null);
  const [packRunError, setPackRunError] = useState<string | null>(null);
  // 阶段 C:两级实盘记录表单(详情页内)
  const [trBusy, setTrBusy] = useState(false);
  const [trError, setTrError] = useState<string | null>(null);
  const [trSelf, setTrSelf] = useState({ period_start: "", period_end: "", return_pct: "", max_drawdown_pct: "", note: "" });
  const [trVerify, setTrVerify] = useState({ statement: "", source: "", period_start: "", period_end: "", return_pct: "", max_drawdown_pct: "", note: "" });

  async function refreshSets() {
    try {
      const list = await onListParameterSets();
      if (list === null) setPsError("实验记录读取失败，请重试。");
      else setPsList(list);
    } catch { setPsError("实验记录读取失败，请重试。"); }
  }
  // 池内真实数据仅由本机产生;进入页面即拉一次,发布/Fork 后重拉。
  // (处理函数引用随父渲染变化,刻意只在挂载时拉取,避免随轮询反复刷新)
  useEffect(() => {
    let cancelled = false;
    setCatalogLoading(true);
    setCatalogError(null);
    void Promise.allSettled([onListParameterSets(), onFetchStages(), onFetchStrategyPacks()]).then(([sets, board, strategyPacks]) => {
      if (cancelled) return;
      if (sets.status === "fulfilled" && sets.value !== null) setPsList(sets.value);
      if (board.status === "fulfilled" && board.value !== null) setStages(board.value);
      if (strategyPacks.status === "fulfilled" && strategyPacks.value !== null) setPacks(strategyPacks.value);
      if ([sets, board, strategyPacks].some(result => result.status === "rejected" || result.value === null)) setCatalogError("部分量化数据读取失败，当前保留已有记录。");
      setCatalogLoading(false);
    });
    return () => { cancelled = true; detailSequence.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogRevision]);

  async function runMine() {
    if (!mineSymbol.trim() || mineBusy) return;
    setMineBusy(true);
    setMineError(null);
    if (!marketPluginEnabled) {
      setMineError("行情插件未启用:挖掘依赖本机行情管道,请先在扩展页启用「中国市场行情」。");
      setMineBusy(false);
      return;
    }
    try {
      const result = await onFactorMine(mineSymbol.trim());
      if (result === null) {
        setMineError("挖掘请求失败(Core 未就绪或行情拉取失败)。");
        return;
      }
      setMineResult(result);
    } catch { setMineError("请求失败，请重试。"); }
    finally { setMineBusy(false); }
  }

  // A3-3 导出参数集:top 公式序列化为分享池阶段 A 形态 JSON(本机下载,数据不出本机)。
  function exportParameterSet(item: QuantFactorItem) {
    if (!mineResult) return;
    const payload = {
      type: "parameter_set",
      stage: "A",
      name: `${mineResult.symbol} 因子参数集`,
      symbol: mineResult.symbol,
      formula_tokens: item.formula_tokens,
      formula: item.formula,
      metrics: { train_ic: item.train_ic, valid_ic: item.valid_ic, samples: item.samples },
      as_of: mineResult.as_of ?? null,
      dataset_version: mineResult.dataset_version ?? null,
      note: "因子挖掘导出:确定性枚举 + 验证集 IC 排序;仅作研究背景,不构成买卖建议",
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `factor-ps-${mineResult.symbol}-${item.formula_tokens.join("_").slice(0, 60)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  // A4-1 一键发布:把 top 公式发进本机参数集池(官方内核重算指标,内容寻址入库)。
  async function publishToPool(item: QuantFactorItem) {
    if (!mineResult || publishBusy) return;
    setPublishBusy(item.formula);
    setPublishNotice(null);
    setPublishError(null);
    try {
      const result = await onPublishParameterSet({ symbol: mineResult.symbol, formulaTokens: item.formula_tokens });
      if (!result.ok) {
        setPublishError(result.detail ?? "发布失败(Core 未就绪、行情插件未启用或公式非法)。");
        return;
      }
      setPublishNotice(`已发布 ${result.entry?.artifact_id ?? ""}。指标由内核以最新行情重算(确定性),可能与挖掘时点的 IC 略有差异。`);
      void refreshSets();
    } catch { setPublishError("请求失败，请重试。"); }
    finally { setPublishBusy(null); }
  }

  async function forkSet(ps: QuantParameterSet) {
    if (psBusyId) return;
    setPsBusyId(ps.artifact_id);
    setPsError(null);
    try {
      const forked = await onForkParameterSet(ps.artifact_id);
      if (!forked) {
        setPsError(`Fork 失败:${ps.artifact_id} 不存在或 Core 未就绪。`);
        return;
      }
      void refreshSets();
    } catch { setPsError("请求失败，请重试。"); }
    finally { setPsBusyId(null); }
  }

  async function openParameterSet(ps: QuantParameterSet) {
    const sequence = ++detailSequence.current;
    setPsDetail({ set: ps, replay: null, lineage: null, error: null });
    setPsDetailLoading(true);
    setTrError(null);
    try {
    const [replay, lineage] = await Promise.all([onReplayParameterSet(ps.artifact_id), onLineageParameterSet(ps.artifact_id)]);
    if (sequence !== detailSequence.current) return;
    setPsDetail({
      set: ps,
      replay,
      lineage,
      error: replay === null ? "回放请求失败，请重试。" : null,
    });
    } catch { if (sequence === detailSequence.current) setPsDetail({ set: ps, replay: null, lineage: null, error: "回放请求失败，请重试。" }); }
    finally { if (sequence === detailSequence.current) setPsDetailLoading(false); }
  }

  // 阶段 D:训练线性模型并入池(闭式岭回归,同输入同结果)。
  async function trainAndPublishModel() {
    if (!mineSymbol.trim() || modelBusy) return;
    if (!marketPluginEnabled) {
      setModelError("行情插件未启用:模型训练依赖本机行情管道,请先在扩展页启用「中国市场行情」。");
      return;
    }
    const lambdaValue = Number(modelLambda);
    if (!Number.isFinite(lambdaValue) || lambdaValue < 0) {
      setModelError("lambda 必须是非负数。");
      return;
    }
    setModelBusy(true);
    setModelError(null);
    setModelResult(null);
    try {
      const result = await onTrainModel({ symbol: mineSymbol.trim(), lambda: lambdaValue });
      if (!result.ok) {
        setModelError(result.detail ?? "训练失败。");
        return;
      }
      setModelResult(result.entry ?? null);
      void refreshSets();
    } catch { setModelError("请求失败，请重试。"); }
    finally { setModelBusy(false); }
  }

  // 阶段 B:导入策略包 manifest(Ed25519 验签;通过也仅锁定待沙箱,不可执行)。
  async function importPack() {
    if (packBusy) return;
    if (!packManifest.trim()) {
      setPackError("请先粘贴策略包 manifest JSON。");
      return;
    }
    setPackBusy(true);
    setPackError(null);
    setPackNotice(null);
    try {
      const result = await onImportStrategyPack(packManifest);
      if (!result.ok) {
        setPackError(result.detail ?? "导入失败。");
        return;
      }
      setPackNotice(`验签通过,已入库:${result.entry?.pack_id}(可在下方运行回测)。`);
      setPackManifest("");
      setPacks(await onFetchStrategyPacks());
    } catch { setPackError("请求失败，请重试。"); }
    finally { setPackBusy(false); }
  }

  // 阶段 B:受限执行器运行策略包,对最近 K 线产出确定性回测。
  async function runPack(packId: string) {
    if (packRunBusyId) return;
    setPackRunBusyId(packId);
    setPackRunError(null);
    setPackRunResult(null);
    try {
      const result = await onRunStrategyPack(packId);
      if (result === null) {
        setPackRunError("回测请求失败(Core 未就绪)。");
        return;
      }
      if (result.available === false) {
        setPackRunError(result.degraded_reason ?? "策略包运行失败。");
        return;
      }
      setPackRunResult(result);
    } catch { setPackRunError("请求失败，请重试。"); }
    finally { setPackRunBusyId(null); }
  }

  // 阶段 C:两级实盘记录——自报 / 对账单核验(哈希锚定)。
  async function submitSelfReport(artifactId: string) {
    if (trBusy) return;
    setTrBusy(true);
    setTrError(null);
    try {
      const result = await onAddTrackRecord(artifactId, {
        period_start: trSelf.period_start,
        period_end: trSelf.period_end,
        return_pct: Number(trSelf.return_pct) || 0,
        max_drawdown_pct: trSelf.max_drawdown_pct === "" ? null : Number(trSelf.max_drawdown_pct),
        note: trSelf.note,
      });
      if (!result.ok) {
        setTrError(result.detail ?? "自报记录提交失败。");
        return;
      }
      setTrSelf({ period_start: "", period_end: "", return_pct: "", max_drawdown_pct: "", note: "" });
      await syncDetailRecords(artifactId);
    } catch { setTrError("请求失败，请重试。"); }
    finally { setTrBusy(false); }
  }

  async function submitVerify(artifactId: string) {
    if (trBusy) return;
    setTrBusy(true);
    setTrError(null);
    try {
      const result = await onVerifyTrackRecord(artifactId, {
        statement: trVerify.statement,
        source: trVerify.source,
        period_start: trVerify.period_start,
        period_end: trVerify.period_end,
        return_pct: Number(trVerify.return_pct) || 0,
        max_drawdown_pct: trVerify.max_drawdown_pct === "" ? null : Number(trVerify.max_drawdown_pct),
        note: trVerify.note,
      });
      if (!result.ok) {
        setTrError(result.detail ?? "对账单核验失败。");
        return;
      }
      setTrVerify({ statement: "", source: "", period_start: "", period_end: "", return_pct: "", max_drawdown_pct: "", note: "" });
      await syncDetailRecords(artifactId);
    } catch { setTrError("请求失败，请重试。"); }
    finally { setTrBusy(false); }
  }

  async function syncDetailRecords(artifactId: string) {
    const list = await onListParameterSets();
    setPsList(list);
    const fresh = list?.find((item) => item.artifact_id === artifactId);
    if (fresh) setPsDetail((prev) => (prev && prev.set.artifact_id === artifactId ? { ...prev, set: fresh } : prev));
  }

  // 默认排序推荐（口径→样本外→复现），不含收益率（合规红线）。
  const poolReady = pool?.available === true;
  const artifacts = pool?.artifacts ?? [];

  const filtered = useMemo(() => {
    let list = [...artifacts];
    if (search.trim()) list = list.filter(item => [item.name, item.desc, item.id].join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
    if (typeFilter !== "all") list = list.filter((a) => a.type === typeFilter);
    for (const [key, values] of Object.entries(filters)) {
      if (!values.length) continue;
      if (key === "style") list = list.filter((a) => values.includes(a.style));
      if (key === "inst") list = list.filter((a) => values.includes(a.instruments));
      if (key === "repro") {
        list = list.filter((a) => {
          const v = a.locked ? "locked" : a.consistency?.local === "未复现" ? "unverified" : "reproduced";
          return values.includes(v);
        });
      }
      if (key === "license") {
        list = list.filter((a) => values.some((v) => a.license.toUpperCase().startsWith(v)));
      }
      if (key === "sample") {
        list = list.filter((a) => {
          const months = a.sample_months ?? 0;
          return values.some(value => value === "ge12" ? months >= 12 : value === "ge6" ? months >= 6 && months < 12 : months < 6);
        });
      }
    }
    return list;
  }, [artifacts, typeFilter, filters, search]);

  // 分享池三个页签：本机已发布制品（阶段 A 参数集 / 线性模型 / 策略包）是真实内容；
  // 「跨用户完整制品卡」未开放时卡片区显示未开放原因（/quant/artifacts available=false）。
  const renderTabBody = () => {
    if (tab === "pool") {
      // 本机已发布制品(参数集与线性模型)是当前真实内容;下方卡片区为完整制品卡的预留位。
      const hasReal = !!psList && psList.length > 0;
      return (
        <>
          {workflow === "conclusion" && <p className="sort-note">口径完整度 · 样本外长度 · 可复现性</p>}
          {hasReal && workflow !== "strategy" && (
            <div className="ps-band">
              <div className="ps-band-head">
                <b>本机已发布制品</b>
                <span>参数集与线性模型均为纯 JSON、内容寻址、不可变;发布时由官方内核重算指标(同输入同结果)。</span>
              </div>
              {psList.filter(ps => workflow !== "conclusion" || [ps.name, ps.symbol, ps.formula, ps.artifact_id].join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())).map((ps) => (
                <PsRow key={ps.artifact_id} ps={ps} busy={psBusyId === ps.artifact_id} onOpen={() => void openParameterSet(ps)} onFork={() => void forkSet(ps)} />
              ))}
              {workflow === "conclusion" && search.trim() && !psList.some(ps => [ps.name, ps.symbol, ps.formula, ps.artifact_id].join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase())) && <div className="pool-empty"><strong>没有匹配的实验</strong><button className="text-button" onClick={() => setSearch("")}>清除搜索</button></div>}
              {psError && <div className="ps-alert" role="alert">{psError}</div>}
            </div>
          )}
          {(workflow === "strategy" || workflow === "backtest") && <div className="ps-band pack-band">
            <div className="ps-band-head">
              <b>策略包</b>
              <span>仅运行发布者签名的包:Ed25519 验签 + 完整性校验;受限执行器阻断文件/网络/子进程访问,超时强制终止。</span>
            </div>
            {packs && packs.length > 0 && (
              <div className="pack-list">
                {packs.map((pack) => (
                  <div className="pack-item" key={pack.pack_id}>
                    <span className="mono">{pack.pack_id}</span>
                    <b>{pack.name}</b>
                    <span className="ver-tag">v{pack.version}</span>
                    {pack.symbol && <span className="mono dim">{pack.symbol}</span>}
                    <span className={`pack-state ${pack.state === "installed" ? "ok" : ""}`}>
                      {pack.state === "installed" ? "可运行 · 受限执行器" : "不可执行 · 需重新导入"}
                    </span>
                    <button
                      className="ghost-btn"
                      disabled={packRunBusyId === pack.pack_id || pack.state !== "installed"}
                      onClick={() => void runPack(pack.pack_id)}
                    >
                      {packRunBusyId === pack.pack_id ? "回测中…" : "运行回测"}
                    </button>
                  </div>
                ))}
              </div>
            )}
            {packRunResult && (
              <div className="ps-band pack-run-result">
                <div className="ps-band-head">
                  <b>{packRunResult.name} · 回测结果</b>
                  {packRunResult.source && <span className="mono dim">数据源 {packRunResult.source}</span>}
                </div>
                <div className="ps-metrics">
                  <span>最终权益 <b>{packRunResult.equity}</b></span>
                  <span>胜率 <b>{packRunResult.win_rate === null || packRunResult.win_rate === undefined ? "—" : `${(packRunResult.win_rate * 100).toFixed(1)}%`}</b></span>
                  <span>回测样本 <b>{packRunResult.bars}</b></span>
                </div>
                <p className="foot-note">{packRunResult.note}</p>
              </div>
            )}
            {packRunError && <div className="ps-alert" role="alert">{packRunError}</div>}
            {(!packs || packs.length === 0) && (
              <p className="evidence-group-empty">目录为空——导入发布者签名的策略包后在此列出并运行回测。</p>
            )}
            <textarea
              className="pack-input"
              placeholder='粘贴策略包 manifest JSON(kind="strategy_pack",含 payload 包代码、payload_sha256、artifact_sha256 与 signature)…'
              aria-label="策略包 manifest"
              value={packManifest}
              onChange={(event) => setPackManifest(event.target.value)}
            />
            <div className="market-toolbar">
              <button className="primary-btn" disabled={packBusy || !packManifest.trim()} onClick={() => void importPack()}>
                {packBusy ? "校验中…" : "导入策略包(验签)"}
              </button>
            </div>
            {packNotice && <div className="ps-alert ok">{packNotice}</div>}
            {packError && <div className="ps-alert" role="alert">{packError}</div>}
          </div>
          }
          {workflow === "conclusion" && <>
          {!hasReal && !poolReady && !catalogLoading && !catalogError && (
            <div className="pool-empty">
              <strong>分享池还没有制品</strong>
              <span>{pool?.degraded_reason ?? "本机还没有参数集或模型——到「我的实验」挖掘/训练并发布,即进入此列表。"}</span>
              <button className="primary-btn" onClick={() => { setWorkflow("factor"); setTab("mine"); }}>创建实验</button>
            </div>
          )}
          {poolReady && <div className="pool-layout">
            <aside className="pool-rail">
              {FILTER_GROUPS.map((group) => (
                <div className="filter-group" key={group.key}>
                  <h5>{group.label}</h5>
                  <div className="filter-list">
                    {group.options.map(([value, label]) => {
                      const checked = filters[group.key]?.includes(value);
                      return (
                        <label className="fr-item" key={value}>
                          <input
                            type="checkbox"
                            checked={checked ?? false}
                            onChange={() =>
                              setFilters((prev) => {
                                const cur = prev[group.key] ?? [];
                                const next = checked ? cur.filter((v) => v !== value) : [...cur, value];
                                return { ...prev, [group.key]: next };
                              })
                            }
                          />
                          {label}
                        </label>
                      );
                    })}
                  </div>
                </div>
              ))}
            </aside>
            <div className="pool-list">
              <div className="type-pills">
                {(["all", "parameter_set", "strategy_pack", "model_weights"] as TypeFilter[]).map((type) => (
                  <button
                    key={type}
                    className={`type-pill ${typeFilter === type ? "active" : ""}`}
                    onClick={() => setTypeFilter(type)}
                  >
                    {type === "all" ? "全部" : TYPE_LABEL[type]}
                  </button>
                ))}
              </div>
              {!poolReady && (
                <div className="pool-note">
                  列表将在此渲染 {filtered.length} 个命中筛选条件的制品（当前为空）。
                </div>
              )}
              {poolReady &&
                filtered.map((artifact) => (
                  <div className="pool-item" key={artifact.id}>
                    <div className="pi-main">
                      <div className="art-head">
                        <h4>{artifact.name}</h4>
                        <span className={`type-tag ${artifact.type}`}>{TYPE_LABEL[artifact.type]}</span>
                        <span className="ver-tag">{artifact.ver}</span>
                        <span className={`repro-chip ${artifact.locked ? "locked" : artifact.repro === "可复现" ? "reproduced" : "unverified"}`}>
                          {artifact.locked ? "不可安装" : artifact.repro}
                        </span>
                      </div>
                      <p className="art-desc">{artifact.desc}</p>
                      <div className="qmetric-band">
                        {artifact.metrics.map((metric) => (
                          <div className="qmetric" key={metric.label}>
                            <span className="m-label">{metric.label}</span>
                            <span className="m-value">{metric.value}</span>
                            <span className={`caliber ${metric.caliber}`}>{metric.caliber}</span>
                          </div>
                        ))}
                      </div>
                      <div className="pi-meta">
                        <span className="f">{STYLE_LABEL[artifact.style] ?? artifact.style}</span>
                        <span>{INST_LABEL[artifact.instruments] ?? artifact.instruments}</span>
                        <span>口径 {artifact.caliber_score}</span>
                        <span>{artifact.sample_out}</span>
                        <span>{artifact.license}</span>
                        <span>{artifact.forks} 次派生</span>
                        <span>更新 {artifact.updated}</span>
                      </div>
                    </div>
                    <div className="pi-side">
                      <button className="primary-btn center" onClick={() => setDetail(artifact)}>
                        查看制品详情 <span>→</span>
                      </button>
                      <button className="ghost-btn" disabled title="此制品尚未提供本机参数集入口">Fork 并调参</button>
                      <button className="ghost-btn" disabled title="此制品尚未提供本机运行入口">本地复现运行</button>
                    </div>
                  </div>
                ))}
              {poolReady && filtered.length === 0 && (
                <div className="pool-empty plain">
                  <strong>无匹配制品</strong>
                  <span>调整筛选条件或清除部分勾选后重试。</span>
                </div>
              )}
            </div>
          </div>}
          </>}
          {workflow === "backtest" && !hasReal && !catalogLoading && <div className="pool-empty"><strong>暂无可回放的参数集</strong><button className="primary-btn" onClick={() => { setWorkflow("factor"); setTab("mine"); }}>创建实验</button></div>}
        </>
      );
    }
    if (tab === "mine") {
      return (
        <div className="factor-mine">
          <div className="pool-empty">
            <strong>确定性因子挖掘</strong>
            <span>枚举公式空间并以验证集 IC 排序——同输入同结果,可复算;输出分享池阶段 A 形态参数集。需行情插件启用,数据实拉自本机缓存。</span>
          </div>
          <div className="market-toolbar">
            <input
              className="market-symbol-input"
              placeholder="标的代码,如 510300"
              aria-label="因子挖掘标的代码"
              value={mineSymbol}
              onChange={(event) => setMineSymbol(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") void runMine(); }}
            />
            <button className="primary-btn" disabled={mineBusy || !mineSymbol.trim()} onClick={() => void runMine()}>
              {mineBusy ? "挖掘中…" : "开始挖掘"}
            </button>
          </div>
          {mineError && <div className="connection-bar" role="alert"><span>{mineError}</span></div>}
          {publishError && <div className="connection-bar" role="alert"><span>{publishError}</span></div>}
          {publishNotice && <div className="ps-alert ok">{publishNotice}</div>}
          {mineResult && mineResult.available && (
            <table className="kv-table factor-table">
              <thead>
                <tr><th>公式(token 序列)</th><th className="num">训练 IC</th><th className="num">验证 IC</th><th className="num">样本</th><th>导出 / 发布</th></tr>
              </thead>
              <tbody>
                {mineResult.top.map((item) => (
                  <tr key={item.formula} title={`公式 tokens:${item.formula_tokens.join(" , ")}`}>
                    <td className="mono">{item.formula}</td>
                    <td className="mono num">{item.train_ic.toFixed(4)}</td>
                    <td className="mono num">{item.valid_ic.toFixed(4)}</td>
                    <td className="mono num">{item.samples}</td>
                    <td>
                      <div className="ps-actions">
                        <button
                          className="ghost-btn"
                          onClick={() => exportParameterSet(item)}
                          title={`导出分享池阶段 A 形态 JSON(公式 ${item.formula})`}
                        >
                          导出参数集
                        </button>
                        <button
                          className="ghost-btn"
                          disabled={publishBusy === item.formula}
                          onClick={() => void publishToPool(item)}
                          title="发布到本机参数集池(官方内核重算指标后入库)"
                        >
                          {publishBusy === item.formula ? "发布中…" : "发布到池"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {mineResult && !mineResult.available && (
            <p className="evidence-group-empty">{mineResult.degraded_reason ?? "未挖到通过阈值的公式(不编造)。"}</p>
          )}
          {mineResult?.note && <p className="foot-note">{mineResult.note}</p>}

          <div className="block-title model-title"><h4>线性模型训练 · 阶段 D</h4><span>纯 JSON 权重,官方内核解释执行;通用模型权重需更强执行隔离,后续开放</span></div>
          <p className="foot-note">
            闭式岭回归(同输入同结果)拟合 6 特征 → 未来 5 日收益;训练/验证 70/30 切分,以验证集 IC 展示。
            训练快照(数据版本/样本/切分/λ)完整入库,可复算。
          </p>
          <div className="market-toolbar">
            <input
              className="market-symbol-input model-lambda-input"
              placeholder="λ 正则强度,如 1.0"
              aria-label="模型正则强度"
              value={modelLambda}
              onChange={(event) => setModelLambda(event.target.value)}
            />
            <button className="primary-btn" disabled={modelBusy || !mineSymbol.trim()} onClick={() => void trainAndPublishModel()}>
              {modelBusy ? "训练中…" : `训练并发布模型(${mineSymbol.trim() || "标的"})`}
            </button>
          </div>
          {modelError && <div className="connection-bar" role="alert"><span>{modelError}</span></div>}
          {modelResult && (
            <div className="ps-band model-result">
              <div className="ps-band-head">
                <b>{modelResult.name}</b>
                <span className="mono">{modelResult.artifact_id}(已入池,权重不可变)</span>
              </div>
              <div className="ps-metrics">
                <span>训练 IC <b>{modelResult.metrics.train_ic.toFixed(4)}</b></span>
                <span>验证 IC <b>{modelResult.metrics.valid_ic.toFixed(4)}</b></span>
                <span>样本 <b>{modelResult.metrics.samples}</b></span>
                {modelResult.training?.lambda !== undefined && <span>λ <b>{modelResult.training.lambda}</b></span>}
              </div>
              <div className="ps-meta">
                {(modelResult.weights ?? []).map((w, index) => (
                  <span key={index}>{index === 0 ? "截距" : modelResult.feature_order?.[index - 1]} = {w}</span>
                ))}
              </div>
              <p className="foot-note">打分 position = tanh(score),回放口径与参数集一致;到「分享池」点「回放曲线」查看确定性回放。</p>
            </div>
          )}
        </div>
      );
    }
    const derived = (psList ?? []).filter((ps) => ps.parent_id && [ps.name, ps.symbol, ps.formula, ps.artifact_id].join(" ").toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
    if (tab === "lineage") {
      return derived.length > 0 ? (
        <div className="ps-band">
          <div className="ps-band-head">
            <b>我的派生 · 谱系链</b>
            <span>Fork 产生的派生参数集,沿 parent_id 可回溯至根。</span>
          </div>
          {derived.map((ps) => (
            <PsRow key={ps.artifact_id} ps={ps} busy={psBusyId === ps.artifact_id} onOpen={() => void openParameterSet(ps)} onFork={() => void forkSet(ps)} />
          ))}
          {psError && <div className="ps-alert" role="alert">{psError}</div>}
        </div>
      ) : (
        <div className="pool-empty">
          <strong>{search.trim() ? "没有匹配的派生实验" : "还没有派生谱系"}</strong>
          <button className="primary-btn" onClick={() => { setSearch(""); setTab("pool"); }}>查看制品</button>
        </div>
      );
    }
    return null;
  };

  // 制品详情:公式/权重 + 口径指标 + 确定性回放曲线 + 实盘记录 + 派生谱系。
  if (psDetail) {
    const ps = psDetail.set;
    const replay = psDetail.replay;
    const lineage = psDetail.lineage;
    return (
      <section className="page-view quant-page">
        <button className="back-btn" onClick={() => { detailSequence.current += 1; setPsDetail(null); }}>
          <span>←</span> 返回分享池
        </button>
        <div className="art-head">
          <span className="type-tag parameter_set">参数集</span>
          <span className="ver-tag">阶段 {ps.stage}</span>
          <span className="source-line">{ps.symbol} · {ps.author === "local" ? "本机发布" : ps.author}{ps.parent_id ? ` · 派生自 ${ps.parent_id}` : ""}</span>
        </div>
        <div className="q-head plain">
          <div>
            <h2>{ps.name}</h2>
            <p className="art-desc">{ps.note || "无备注"}</p>
          </div>
        </div>
        <div className="art-meta">
          <span>{ps.artifact_id}</span>
          {ps.dataset_version && <span>{ps.dataset_version}</span>}
          {ps.as_of && <span>数据截至 {ps.as_of.slice(0, 10)}</span>}
          <span>发布 {ps.created_at.slice(0, 10)}</span>
        </div>

        <div className="block-title"><h4>{ps.type === "model_weights" ? "模型权重" : "公式"}</h4><span>纯 JSON · 官方内核解释执行</span></div>
        <div className="ps-formula-box">
          {ps.type === "model_weights" ? (
            <>
              <code>score = {(ps.weights?.[0] ?? 0).toFixed(4)} + Σ w_i · feature_i</code>
              <span className="mono dim">
                {(ps.weights ?? []).slice(1).map((w, index) => `${ps.feature_order?.[index]}=${w}`).join("  ")}
              </span>
              <span className="mono dim">position = tanh(score) · 特征与 quant_factors 同一套,逐 bar 确定性求值</span>
            </>
          ) : (
            <>
              <code>{ps.formula}</code>
              <span className="mono dim">{JSON.stringify(ps.formula_tokens)}</span>
            </>
          )}
        </div>

        <div className="block-title"><h4>指标与口径</h4><span>训练/验证 70/30 切分 · IC = 与未来 5 日收益的秩相关</span></div>
        <div className="qmetric-band">
          <div className="qmetric">
            <span className="m-label">训练 IC</span>
            <span className="m-value">{ps.metrics.train_ic.toFixed(4)}</span>
            <span className="caliber backtest">样本内</span>
          </div>
          <div className="qmetric">
            <span className="m-label">验证 IC</span>
            <span className="m-value">{ps.metrics.valid_ic.toFixed(4)}</span>
            <span className="caliber backtest">样本外</span>
          </div>
          <div className="qmetric">
            <span className="m-label">样本数</span>
            <span className="m-value">{ps.metrics.samples}</span>
            <span className="caliber backtest">bars</span>
          </div>
        </div>

        <div className="block-title"><h4>确定性回放</h4><span>tanh 仓位 × 次日收益 · 闭 K 线 · 同输入同结果</span></div>
        {psDetailLoading && <p className="evidence-group-empty">回放计算中…</p>}
        {!psDetailLoading && psDetail.error && <div className="ps-alert" role="alert">{psDetail.error}<button className="text-button" onClick={() => void openParameterSet(ps)}>重新回放</button></div>}
        {!psDetailLoading && !psDetail.error && replay && replay.available !== false && replay.curve.length > 0 && (
          <>
            <ReplayCurve curve={replay.curve} />
            <div className="ps-metrics ps-stats">
              <span>最终权益 <b>{replay.equity}</b></span>
              <span>胜率 <b>{replay.win_rate === null ? "—" : `${(replay.win_rate * 100).toFixed(1)}%`}</b></span>
              <span>回放样本 <b>{replay.bars}</b></span>
              {replay.source && <span>数据源 <b>{replay.source}</b></span>}
            </div>
            <p className="foot-note">{replay.note}</p>
          </>
        )}
        {!psDetailLoading && !psDetail.error && replay && (replay.available === false || replay.curve.length === 0) && (
          <p className="evidence-group-empty">{replay.degraded_reason ?? "回放样本不足,未产生曲线(不编造)。"}</p>
        )}

        <div className="block-title"><h4>派生谱系</h4><span>{lineage && lineage.length > 1 ? "沿 parent_id 回溯至根" : ps.parent_id ? `上游 ${ps.parent_id}` : "无上游"}</span></div>
        {lineage && lineage.length > 1 && (
          <div className="ps-lineage">
            {[...lineage].reverse().map((node, index) => (
              <span key={node.artifact_id}>
                {index > 0 && <span className="ps-lineage-arrow"> → </span>}
                <code>{node.artifact_id}{node.artifact_id === ps.artifact_id ? "(当前)" : ""}</code>
              </span>
            ))}
          </div>
        )}

        <div className="block-title"><h4>实盘记录(两级制)</h4><span>只作筛选,绝不参与排序;无核验记录时不产生任何暗示可信的表述</span></div>
        {(ps.track_records ?? []).length > 0 ? (
          <div className="tr-list">
            {ps.track_records!.map((record) => (
              <div className={`tr-item ${record.state}`} key={record.record_id}>
                <span className={`tr-state ${record.state}`}>
                  {record.state === "broker_verified" ? "对账单核验(本机导入)" : "作者自报 · 未核验"}
                </span>
                <span className="mono">{record.period_start} → {record.period_end}</span>
                <span>收益 <b>{record.return_pct.toFixed(2)}%</b></span>
                {record.max_drawdown_pct !== null && <span>最大回撤 <b>{record.max_drawdown_pct.toFixed(2)}%</b></span>}
                {record.source && <span className="mono dim">依据:{record.source}</span>}
                {record.statement_sha256 && <span className="mono dim" title={record.statement_sha256}>对账单哈希 {record.statement_sha256.slice(0, 12)}…</span>}
                {record.note && <span className="dim">{record.note}</span>}
              </div>
            ))}
          </div>
        ) : (
          <p className="evidence-group-empty">暂无实盘记录——下面可提交自报,或导入对账单完成本机核验。</p>
        )}
        <div className="tr-forms">
          <div className="tr-form">
            <b>添加自报记录</b>
            <span className="foot-note">作者自述,平台未核验;展示时与回测指标分色分权重。</span>
            <div className="market-toolbar">
              <input className="market-symbol-input" placeholder="开始日 2025-01-01" aria-label="自报开始日" value={trSelf.period_start} onChange={(e) => setTrSelf({ ...trSelf, period_start: e.target.value })} />
              <input className="market-symbol-input" placeholder="结束日 2025-06-30" aria-label="自报结束日" value={trSelf.period_end} onChange={(e) => setTrSelf({ ...trSelf, period_end: e.target.value })} />
              <input className="market-symbol-input" placeholder="收益率 %,如 12.5" aria-label="自报收益率" value={trSelf.return_pct} onChange={(e) => setTrSelf({ ...trSelf, return_pct: e.target.value })} />
              <input className="market-symbol-input" placeholder="最大回撤 %(可选)" aria-label="自报最大回撤" value={trSelf.max_drawdown_pct} onChange={(e) => setTrSelf({ ...trSelf, max_drawdown_pct: e.target.value })} />
              <input className="market-symbol-input tr-note" placeholder="备注(可选)" aria-label="自报备注" value={trSelf.note} onChange={(e) => setTrSelf({ ...trSelf, note: e.target.value })} />
              <button className="ghost-btn" disabled={trBusy || !trSelf.period_start || !trSelf.period_end} onClick={() => void submitSelfReport(ps.artifact_id)}>提交自报</button>
            </div>
          </div>
          <div className="tr-form">
            <b>导入对账单核验</b>
            <span className="foot-note">对账单内容 SHA-256 锚定;本机形态下核验人是「你对过对账单」,不是平台,仍不构成收益保证。</span>
            <div className="market-toolbar">
              <textarea
                className="pack-input"
                placeholder="粘贴经纪商对账单文本/CSV 导出内容…"
                aria-label="对账单内容"
                value={trVerify.statement}
                onChange={(e) => setTrVerify({ ...trVerify, statement: e.target.value })}
              />
            </div>
            <div className="market-toolbar">
              <input className="market-symbol-input" placeholder="来源:券商/账户" aria-label="对账单来源" value={trVerify.source} onChange={(e) => setTrVerify({ ...trVerify, source: e.target.value })} />
              <input className="market-symbol-input" placeholder="开始日" aria-label="核验开始日" value={trVerify.period_start} onChange={(e) => setTrVerify({ ...trVerify, period_start: e.target.value })} />
              <input className="market-symbol-input" placeholder="结束日" aria-label="核验结束日" value={trVerify.period_end} onChange={(e) => setTrVerify({ ...trVerify, period_end: e.target.value })} />
              <input className="market-symbol-input" placeholder="收益率 %" aria-label="核验收益率" value={trVerify.return_pct} onChange={(e) => setTrVerify({ ...trVerify, return_pct: e.target.value })} />
              <button className="ghost-btn" disabled={trBusy || !trVerify.statement.trim() || !trVerify.source.trim()} onClick={() => void submitVerify(ps.artifact_id)}>核验入库</button>
            </div>
          </div>
        </div>
        {trError && <div className="ps-alert" role="alert">{trError}</div>}

        <div className="risk-box">
          <b>风险披露</b>
          <ul>
            <li>回放是在本机行情上重算的研究背景证据,不代表未来表现,也不代表任何实盘结果。</li>
            <li>复制本参数不会让你得到相同结果：成交价、滑点、手续费与数据版本都不同。</li>
            <li>本制品不构成投资建议；是否采纳由你确认，责任也在你。</li>
          </ul>
        </div>
      </section>
    );
  }

  if (detail) {
    return (
      <section className="page-view quant-page">
        <button className="back-btn" onClick={() => setDetail(null)}>
          <span>←</span> 返回分享池
        </button>
        <div className="art-head">
          <span className={`type-tag ${detail.type}`}>{TYPE_LABEL[detail.type]}</span>
          <span className="ver-tag">{detail.ver}</span>
          <span className="source-line">口径 {detail.caliber_score} · {detail.sample_out} · {detail.repro}</span>
        </div>
        <div className="q-head plain">
          <div>
            <h2>{detail.name}</h2>
            <p className="art-desc">{detail.desc}</p>
          </div>
        </div>
        <div className="art-meta">
          <span>{detail.author}</span>
          <span>{detail.id}</span>
          <span>内容哈希 {detail.hash}</span>
          <span>许可 {detail.license}</span>
        </div>

        <div className="block-title"><h4>指标与口径</h4><span>每个数字都必须挂口径</span></div>
        <div className="qmetric-band">
          {detail.metrics.map((metric) => (
            <div className="qmetric" key={metric.label}>
              <span className="m-label">{metric.label}</span>
              <span className="m-value">{metric.value}</span>
              <span className={`caliber ${metric.caliber}`}>{metric.caliber}</span>
            </div>
          ))}
        </div>

        {detail.consistency && (
          <>
            <div className="block-title"><h4>一致性对照</h4><span>作者声称 vs 本地复现</span></div>
            <table className="ptable">
              <thead>
                <tr><th>来源</th><th>年化收益</th><th>差异</th><th>说明</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>作者声称</td><td>{detail.consistency.author ?? "—"}</td><td>—</td>
                  <td rowSpan={2}>{detail.consistency.note}</td>
                </tr>
                <tr>
                  <td>本地复现</td><td>{detail.consistency.local ?? "—"}</td><td>{detail.consistency.diff ?? "—"}</td>
                </tr>
              </tbody>
            </table>
          </>
        )}

        {detail.params.length > 0 && (
          <>
            <div className="block-title"><h4>参数</h4><span>可 Fork 调参</span></div>
            <table className="ptable">
              <thead>
                <tr><th>参数</th><th>当前值</th><th>允许范围</th><th>说明</th></tr>
              </thead>
              <tbody>
                {detail.params.map((pr) => (
                  <tr key={pr.k}>
                    <td>{pr.k}</td>
                    <td>{pr.v}</td>
                    <td>{pr.range}</td>
                    <td>{pr.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        <div className="block-title"><h4>派生谱系</h4><span>{detail.derived_from ? `上游 ${detail.derived_from}` : "无上游"}</span></div>

        <div className="block-title"><h4>复现记录</h4><span>{detail.runs.length} 次运行</span></div>
        <table className="ptable">
          <thead>
            <tr><th>运行 ID</th><th>执行方</th><th>环境</th><th>结果</th><th>状态</th></tr>
          </thead>
          <tbody>
            {detail.runs.map((run) => (
              <tr key={run.id}>
                <td>{run.id}</td><td>{run.who}</td><td>{run.env}</td><td>{run.result}</td><td>{run.status}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="risk-box">
          <b>风险披露</b>
          <ul>
            <li>回测收益不代表未来表现，也不代表任何实盘结果。</li>
            <li>复制本参数不会让你得到相同结果：成交价、滑点、手续费与数据版本都不同。</li>
            <li>本制品不构成投资建议；是否采纳由你确认，责任也在你。</li>
            <li>实盘数字为作者自报，未经券商核验，仅作参考。</li>
          </ul>
        </div>
      </section>
    );
  }

  return (
    <section className="page-view quant-page">
      <header className="quant-heading">
        <div><span className="kicker">专业模式 · {WORKFLOW.find(item => item.key === workflow)?.label}</span><h2>量化研究实验室</h2></div>
        <span className="quant-catalog-count">{psList?.length ?? 0} 项实验 · {packs?.length ?? 0} 个策略包</span>
      </header>
      <nav className="quant-workflow" aria-label="量化研究阶段">
        {WORKFLOW.map((stage, index) => <button key={stage.key} aria-current={workflow === stage.key ? "step" : undefined} onClick={() => { setWorkflow(stage.key); setTab(stage.key === "factor" ? "mine" : "pool"); }}>
          <span>{String(index + 1).padStart(2, "0")}</span>{stage.label}
        </button>)}
      </nav>
      {catalogLoading && <p role="status">正在读取实验记录…</p>}
      {catalogError && <div className="ps-alert" role="alert">{catalogError}<button className="text-button" onClick={() => setCatalogRevision(value => value + 1)}>重新读取</button></div>}
      <details className="quant-capabilities"><summary>能力开放状态</summary>
      <div className="stage-bar">
        {STAGES.map((stage) => {
          // 开放状态以服务端 /quant/stages 为准;未取到时仅阶段 A 视作开放(与 pool.stage 一致)。
          const stageInfo = stages?.find((item) => item.key === stage.key);
          const on = stageInfo ? stageInfo.open : pool?.stage === stage.key;
          return (
            <div className={`stage-cell ${on ? "on" : "off"}`} key={stage.key}>
              <span className="s-tag">{on ? "已开放" : "未开放"}</span>
              <b>{stage.name} · {stage.title}</b>
              <small>{stageInfo?.reason ?? stage.desc}</small>
            </div>
          );
        })}
      </div>

      </details>

      {workflow === "conclusion" && <div className="quant-tabs">
        {(
          [
            ["pool", "分享池"],
            
            ["lineage", "我的派生"],
          ] as Array<[QuantTab, string]>
        ).map(([key, label]) => (
          <button key={key} className={`qtab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)}>
            {label}
            <span>{key === "pool" ? (psList?.length ?? artifacts.length) : (psList ?? []).filter(item => item.parent_id).length}</span>
          </button>
        ))}
      </div>}

      {workflow === "data" ? <section className="quant-data">
        <h3>研究数据</h3>
        <dl><div><dt>行情能力</dt><dd>{marketPluginEnabled ? "已启用" : "未启用"}</dd></div><div><dt>数据环境</dt><dd>{isDemo ? "演示数据" : "本机行情缓存"}</dd></div><div><dt>最近实验数据</dt><dd>{mineResult?.as_of ?? psList?.map(item => item.as_of ?? "").sort().at(-1) ?? "暂无记录"}</dd></div></dl>
        <label>标的代码<input type="text" value={mineSymbol} onChange={event => setMineSymbol(event.target.value)} /></label>
        <button className="primary-btn" disabled={!mineSymbol.trim()} onClick={() => { if (!marketPluginEnabled) onNavigate("extensions"); else { setWorkflow("factor"); setTab("mine"); } }}>{marketPluginEnabled ? "进入因子研究" : "配置行情插件"}</button>
      </section> : <>
        {workflow === "conclusion" && <label className="quant-search">搜索实验<input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="名称、代码或公式" /></label>}
        {renderTabBody()}
      </>}

      <p className="quant-risk">研究与回测结果不构成收益承诺；实盘自报与已核验记录分别展示。</p>

      <ExperimentSection coreRequest={coreRequest} />
    </section>
  );
}
/** 阶段 A/D 通道的制品行(分享池列表与「我的派生」共用):公式/权重 + 口径指标 + 回放/Fork 操作。 */
function PsRow({ ps, busy, onOpen, onFork }: { ps: QuantParameterSet; busy: boolean; onOpen: () => void; onFork: () => void }) {
  const isModel = ps.type === "model_weights";
  return (
    <div className="ps-item">
      <div className="ps-main">
        <div className="ps-head">
          <h4>{ps.name}</h4>
          <span className={`type-tag ${ps.type}`}>{isModel ? "模型权重" : "参数集"}</span>
          <span className="mono ps-symbol">{ps.symbol}</span>
          {ps.parent_id && <span className="ps-derived">派生自 {ps.parent_id}</span>}
        </div>
        <div className="ps-formula">
          {isModel
            ? `线性打分:${(ps.feature_order ?? []).join(" + ")}(闭式岭回归 λ=${ps.training?.lambda ?? "—"})`
            : ps.formula}
        </div>
        <div className="ps-metrics">
          <span>训练 IC <b>{ps.metrics.train_ic.toFixed(4)}</b></span>
          <span>验证 IC <b>{ps.metrics.valid_ic.toFixed(4)}</b></span>
          <span>样本 <b>{ps.metrics.samples}</b></span>
        </div>
        <div className="ps-meta">
          <span>{ps.artifact_id}</span>
          {ps.as_of && <span>数据截至 {ps.as_of.slice(0, 10)}</span>}
          {ps.dataset_version && <span>{ps.dataset_version}</span>}
          <span>发布 {ps.created_at.slice(0, 10)}</span>
          <span>结果状态：已发布</span>
        </div>
      </div>
      <div className="ps-actions">
        <span className="quant-record-risk">样本内外 IC 不代表未来收益</span>
        <button className="primary-btn" onClick={onOpen}>回放曲线</button>
        <button className="ghost-btn" disabled={busy} onClick={onFork}>{busy ? "Fork 中…" : "Fork 并调参"}</button>
      </div>
    </div>
  );
}

/** 确定性回放权益曲线:纯 SVG(无图表库依赖),虚线为初始权益 1.0 基线。 */
function ReplayCurve({ curve }: { curve: Array<{ date: string; equity: number }> }) {
  const W = 640;
  const H = 170;
  const PAD = 10;
  const values = curve.map((point) => point.equity);
  const low = Math.min(1.0, ...values);
  const high = Math.max(1.0, ...values);
  const span = high - low || 1;
  const x = (index: number) => PAD + (index / Math.max(1, curve.length - 1)) * (W - PAD * 2);
  const y = (value: number) => H - PAD - ((value - low) / span) * (H - PAD * 2);
  const line = curve.map((point, index) => `${x(index).toFixed(1)},${y(point.equity).toFixed(1)}`).join(" ");
  const area = `${PAD},${H - PAD} ${line} ${(W - PAD).toFixed(1)},${(H - PAD).toFixed(1)}`;
  return (
    <div className="replay-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="确定性回放权益曲线(虚线为初始权益 1.0)">
        <polygon className="replay-area" points={area} />
        <line className="replay-base" x1={PAD} x2={W - PAD} y1={y(1.0).toFixed(1)} y2={y(1.0).toFixed(1)} />
        <polyline className="replay-line" points={line} />
      </svg>
      <div className="replay-caption">
        <span>{curve.at(0)?.date.slice(0, 10) ?? ""} → {curve.at(-1)?.date.slice(0, 10) ?? ""}</span>
        <span>虚线 = 初始权益 1.0</span>
      </div>
    </div>
  );
}
