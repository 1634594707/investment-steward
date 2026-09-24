import type { UICard } from "@investment-steward/ui-card-schemas";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getSlotMeta } from "./registry";
import { renderCard } from "./renderers";
import { createCoreClient } from "../state/coreClient";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { EmptyState } from "../components/EmptyState";
import { Skeleton, SkeletonLines } from "../components/Skeleton";

interface Props {
  slot: string;
  /** 页面数据装载中（booting / candlesLoading 等）：渲染骨架屏而不是伪空态。 */
  loading?: boolean;
  children?: ReactNode | null;
}

/** 通用插槽容器。空态说明需要的插件能力；内容由内核负责渲染，不来自插件。 */
export function CardSlot({ slot, loading: pageLoading = false, children }: Props) {
  const meta = getSlotMeta(slot);
  const isEmpty = children === null || children === undefined;
  const client = useMemo(() => createCoreClient(), []);
  const [remoteCards, setRemoteCards] = useState<UICard[] | null>(null);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function loadCards() {
    setLoading(true);
    setRemoteError(null);
    const response = await client.request<UICard[]>({ method: "GET", path: `/cards/slots/${slot}` });
    if (response.status >= 400 || !Array.isArray(response.data)) {
      setRemoteCards(null);
      setRemoteError("卡片暂时无法加载");
    } else {
      setRemoteCards(response.data);
    }
    setLoading(false);
  }

  useEffect(() => {
    void loadCards();
  }, [client, slot]);

  const hasRemoteCards = remoteCards !== null;
  const busy = loading || pageLoading;

  return (
    <section className="card-slot" data-slot={slot}>
      {meta && (
        <span className="slot-kicker">
          {meta.label} · {meta.id}
        </span>
      )}
      {busy && !hasRemoteCards ? (
        // F2-1 骨架屏：加载中给形状占位（CLS≈0），不再用文字「正在加载」顶位。
        <div className="slot-skeleton" role="status" aria-label="卡片内容加载中">
          <SkeletonLines lines={2} />
          <Skeleton kind="block" />
        </div>
      ) : hasRemoteCards ? (
        remoteCards.length > 0 ? <>
          <div className="slot-card-list">{remoteCards.map((card) => (
            <ErrorBoundary key={card.card_id} variant="card">
              {renderCard({ ...card, evidence_refs: card.evidence_refs.map(String), action_mode: card.action_mode })}
            </ErrorBoundary>
          ))}</div>
          {!isEmpty && <ErrorBoundary variant="card">{children}</ErrorBoundary>}
        </> : !isEmpty ? <ErrorBoundary variant="card">{children}</ErrorBoundary> : (
          <EmptyState title="这里还没有内容" reason={meta?.empty_hint ?? "暂无真实数据或相应插件能力。"} action={remoteError ? "重试" : undefined} onAction={remoteError ? () => void loadCards() : undefined} />
        )
      ) : isEmpty ? (
        <EmptyState
          title="这里还没有内容"
          reason={meta?.empty_hint ?? "启用相应插件后，这里会显示对应内容。"}
          action={remoteError ? "重试加载" : undefined}
          onAction={remoteError ? () => void loadCards() : undefined}
        />
      ) : (
        <ErrorBoundary variant="card">{children}</ErrorBoundary>
      )}
    </section>
  );
}
