import { formatDate } from "../state/format";

interface Props {
  sourceName: string;
  pluginId?: string | null;
  release?: string | null;
  /** 数据生效时间（as_of / observed_at） */
  asOf?: string | null;
  isDemo?: boolean;
  /** 模型引擎标注（研究回答 / 学习卡）：模型方案名（provider） */
  modelProvider?: string | null;
  /** 模型引擎标注：模型名（model_name） */
  modelName?: string | null;
  /** 本地确定性引擎产出（未调用外部模型）时，展示「本地引擎」徽章 */
  isLocalEngine?: boolean;
}

/** 卡片来源行：来源 · 插件 id · 版本 · 模型引擎 · 时间。有插件来源才显示插件段。 */
export function SourceLine({ sourceName, pluginId, release, asOf, isDemo, modelProvider, modelName, isLocalEngine }: Props) {
  const modelTag = [modelProvider, modelName].filter(Boolean).join(" · ");
  return (
    <div className="data-line">
      <span className="source-dot" aria-hidden="true" />
      <span>{sourceName}</span>
      {pluginId && <span>{pluginId}</span>}
      {release && <span>v{release}</span>}
      {modelTag && <span>{modelTag}</span>}
      {asOf && <time>{formatDate(asOf)}</time>}
      {isLocalEngine && <span className="patch-tag">本地引擎</span>}
      {isDemo && <span className="patch-tag">演示</span>}
    </div>
  );
}