"""Replay archived macro series without network access, migrations or database writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from investment_steward_core import macro_feed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        cached = {key: json.loads(payload) for key, payload in conn.execute("SELECT series_key,payload FROM macro_cache")}
    requests = []

    def resolve(db, credential, series_key, series_id, *, transform="none", **kwargs):
        # Same exact key construction as _resolve_series; absent data stays absent.
        key = series_key if transform == "none" else f"{series_key}:t:{transform}"
        payload = cached.get(key)
        requests.append({"series_key": key, "found": payload is not None})
        return payload

    with patch.object(macro_feed, "_resolve_series", resolve):
        snapshots = [macro_feed.get_region_snapshot(None, SimpleNamespace(get=lambda key: None), region).model_dump(mode="json")
                     for region in ("us", "cn", "eu", "jp", "in")]
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after, "Archive changed; do not write outputs"
    manual = macro_feed.MANUAL_MACRO_FILE
    evidence = {
        "database": str(database), "database_sha256_before": before, "database_sha256_after": after,
        "cached_series_count": len(cached), "requests": requests,
        "manual_source": {"path": str(manual), "sha256": hashlib.sha256(manual.read_bytes()).hexdigest(),
                          "scope": "Current repository manual supplements, not historical database observations"},
        "scope": "Offline replay using archived series, current manual supplements and current definitions/default weights; generated_at is replay time, not original snapshot time",
        "snapshots": snapshots,
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"database_unchanged": before == after, "cached_series": len(cached), "regions": [
        {"region": s["region"], "indicators": len(s["indicators"]),
         "missing_dimensions": [d["dim"] for d in s["positioning"]["dims"] if d["score"] is None]
         if s["positioning"] else None} for s in snapshots]}))


if __name__ == "__main__":
    main()
