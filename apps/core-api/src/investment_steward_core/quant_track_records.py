"""实盘记录两级制(quant_track_records):分享池阶段 C 的本机忠实形态。

合规边界(对齐 strategy-sharing-pool-plan §4.2,红线 C1/C2/C6 不放宽):
- self_reported:作者自述,未核验,UI 必须与回测指标分色分权重展示;
- broker_verified:以导入的经纪商对账单为依据——对账单内容 SHA-256 锚定,
  记录核验依据与时间;本机形态下核验人是「你自己对过对账单」,不是平台,
  因此展示为「对账单核验(本机导入)」,仍不构成收益保证;
- 实盘记录只作筛选、绝不参与排序;无核验记录时不产生任何暗示可信的表述。

存储:layout/quant_track_records.json,按 artifact_id 追加(制品本体不可变,
记录是关于制品的外部证据,追加不修改制品内容)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from investment_steward_core.storage.paths import StorageLayout
from typing import Any

def _load(layout: StorageLayout) -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(layout.quant_track_records_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(layout: StorageLayout, records: dict[str, list[dict[str, Any]]]) -> None:
    layout.artifacts.mkdir(parents=True, exist_ok=True)
    layout.quant_track_records_file.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def list_for(layout: StorageLayout, artifact_id: str) -> list[dict[str, Any]]:
    return sorted(_load(layout).get(artifact_id, []), key=lambda item: item["created_at"], reverse=True)


def add_self_reported(
    layout: StorageLayout,
    artifact_id: str,
    *,
    period_start: str,
    period_end: str,
    return_pct: float,
    max_drawdown_pct: float | None,
    note: str = "",
) -> dict[str, Any]:
    """追加一条自报实盘记录(state=self_reported,永不参与排序)。"""
    if not period_start or not period_end:
        raise ValueError("自报记录必须提供 period_start / period_end")
    record = {
        "record_id": f"tr-{hashlib.sha256(json.dumps({'id': artifact_id, 'at': datetime.now(UTC).isoformat()}, ensure_ascii=False).encode('utf-8')).hexdigest()[:12]}",
        "artifact_id": artifact_id,
        "state": "self_reported",
        "period_start": period_start,
        "period_end": period_end,
        "return_pct": round(float(return_pct), 4),
        "max_drawdown_pct": round(float(max_drawdown_pct), 4) if max_drawdown_pct is not None else None,
        "source": None,
        "statement_sha256": None,
        "note": note,
        "created_at": datetime.now(UTC).isoformat(),
    }
    records = _load(layout)
    records.setdefault(artifact_id, []).append(record)
    _save(layout, records)
    return record


def verify_with_statement(
    layout: StorageLayout,
    artifact_id: str,
    *,
    statement: str,
    source: str,
    period_start: str,
    period_end: str,
    return_pct: float,
    max_drawdown_pct: float | None,
    note: str = "",
) -> dict[str, Any]:
    """对账单核验:statement 文本 SHA-256 锚定后记录 state=broker_verified。

    本机形态下「核验」= 你导入了经纪商对账单且对其内容负责;来源与哈希公开可查,
    不冒充平台核验,也不构成收益保证。
    """
    if not statement.strip():
        raise ValueError("对账单内容为空,拒绝核验")
    if not source.strip():
        raise ValueError("必须标注对账单来源(券商/账户)")
    statement_sha256 = hashlib.sha256(statement.encode("utf-8")).hexdigest()
    record = {
        "record_id": f"tr-{statement_sha256[:12]}",
        "artifact_id": artifact_id,
        "state": "broker_verified",
        "period_start": period_start,
        "period_end": period_end,
        "return_pct": round(float(return_pct), 4),
        "max_drawdown_pct": round(float(max_drawdown_pct), 4) if max_drawdown_pct is not None else None,
        "source": source.strip(),
        "statement_sha256": statement_sha256,
        "note": note,
        "created_at": datetime.now(UTC).isoformat(),
    }
    records = _load(layout)
    bucket = records.setdefault(artifact_id, [])
    bucket = [r for r in bucket if r["record_id"] != record["record_id"]]
    bucket.append(record)
    records[artifact_id] = bucket
    _save(layout, records)
    return record
