import type { Evidence } from "@investment-steward/domain-contracts";
import { formatDate } from "../state/format";
import { ActionModeChip } from "../components/ActionModeChip";
import { useFocusTrap } from "../components/useFocusTrap";

interface Props {
  items: Evidence[];
  evidenceId: string | null;
  onClose: () => void;
}

/** 全局证据抽屉。任意页面点击证据引用/卡片动作（open_evidence）打开。F1-3：焦点圈定与还原。 */
export function EvidenceDrawer({ items, evidenceId, onClose }: Props) {
  const item = items.find((candidate) => candidate.evidence_id === evidenceId);
  const drawerRef = useFocusTrap<HTMLElement>(Boolean(item));
  if (!item) return null;
  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="ev-drawer" role="dialog" aria-modal="true" aria-label="证据详情" onClick={(event) => event.stopPropagation()} ref={drawerRef}>
        <header className="ev-drawer-head">
          <div>
            <span className="section-kicker">EVIDENCE DETAIL</span>
            <h3>一条证据的完整说明</h3>
          </div>
          <button className="icon-button" aria-label="关闭" onClick={onClose}>
            ×
          </button>
        </header>

        <p className="ev-summary">{item.summary}</p>

        <dl className="ev-detail-list">
          <dt>来源</dt>
          <dd>{item.source_name}</dd>
          <dt>证据 id</dt>
          <dd className="mono">{item.evidence_id}</dd>
          <dt>类型</dt>
          <dd>{item.evidence_type}</dd>
          <dt>关系</dt>
          <dd>{item.relation}</dd>
          <dt>采集时间</dt>
          <dd>{formatDate(item.collected_at)}</dd>
          <dt>观测时间</dt>
          <dd>{item.observed_at ? formatDate(item.observed_at) : "—"}</dd>
          {item.valid_until && (
            <>
              <dt>有效期至</dt>
              <dd>
                {formatDate(item.valid_until)}
                {new Date(item.valid_until).getTime() < Date.now() && <span className="soft-tag gray">已过期 · 仅作历史依据</span>}
              </dd>
            </>
          )}
          <dt>发布/数据版本</dt>
          <dd>{item.source_dataset_version ?? "—"}</dd>
          <dt>生产插件</dt>
          <dd className="mono">
            {item.producer_plugin_id ?? "—"}
            {item.producer_release ? ` v${item.producer_release}` : ""}
          </dd>
          <dt>新鲜度</dt>
          <dd className="mono">{item.freshness}</dd>
          <dt>原文定位</dt>
          <dd className="mono">{item.raw_locator ?? "—"}</dd>
          <dt>内容哈希</dt>
          <dd className="mono">{item.content_hash.slice(0, 24)}…</dd>
          <dt>状态</dt>
          <dd>{item.status}</dd>
        </dl>

        {item.limitations.length > 0 && (
          <>
            <div className="ev-drawer-subtitle">限制说明</div>
            <ul className="ev-limits">
              {item.limitations.map((limit) => (
                <li key={limit}>{limit}</li>
              ))}
            </ul>
          </>
        )}
        {item.quality_flags.length > 0 && (
          <>
            <div className="ev-drawer-subtitle">质量标记</div>
            <div className="tag-row">
              {item.quality_flags.map((flag) => (
                <span className="soft-tag gray" key={flag}>{flag}</span>
              ))}
            </div>
          </>
        )}
        <div className="ev-drawer-foot">
          本证据来自本地 Core 账本，未离开本机。
        </div>
        {/* 占位说明：单一证据暂以详情呈现；后续研究页组合为支持/相反证据时复用 ActionModeChip 分级启动。 */}
        <div className="ev-drawer-placeholder">
          <ActionModeChip mode="observe" />
        </div>
      </aside>
    </div>
  );
}