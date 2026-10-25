"""Read the supplied archive without opening application storage or migrations."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    before = digest(database)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only = ON")
        scans = [(row[0], row[1], json.loads(row[2])) for row in conn.execute(
            "SELECT job_id, state, summary FROM scan_jobs ORDER BY created_at"
        )]
        reports = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM ai_research_reports WHERE kind = 'stock'"
        )]
        baseline = json.loads(conn.execute(
            "SELECT payload FROM ai_research_reports WHERE report_id = ?",
            ("bea494cc-5e3f-44e4-9f8d-5b8156c85a30",),
        ).fetchone()[0])
        empty_counts = {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                        for table in ("research_snapshots", "quant_artifacts")}
    charts = []
    for report in reports:
        chart = report.get("price_chart") or {}
        candles = chart.get("candles") or []
        if candles:
            dates = sorted(bar["date"] for bar in candles)
            charts.append({"report_id": report["report_id"], "symbol": report["symbol"],
                           "generated_at": report["generated_at"], "adjust": chart["adjust"],
                           "provider": chart["provider"], "first_date": dates[0],
                           "last_date": dates[-1], "dates": dates})
    scan_audit = []
    signal_observations = Counter()
    for job_id, state, summary in scans:
        rows = summary["results"]
        coverage = []
        for row in rows:
            for hit in row["hit_detail"]:
                signal_observations[(row["symbol"], hit["tactic_id"], hit["date"])] += 1
            matching = [chart for chart in charts if chart["symbol"] == row["symbol"]]
            if matching:
                coverage.append({"symbol": row["symbol"],
                                 "scan_latest_bar_date": row["data_quality"]["latest_bar_date"],
                                 "charts": [{"report_id": chart["report_id"],
                                             "first_date": chart["first_date"],
                                             "last_date": chart["last_date"],
                                             "bars_after_scan": sum(
                                                 date > row["data_quality"]["latest_bar_date"]
                                                 for date in chart["dates"])} for chart in matching]})
        scan_audit.append({"job_id": job_id, "state": state,
                           "generated_at": summary["generated_at"], "mode": summary["mode"],
                           "results": len(rows), "ok": sum(row["ok"] for row in rows),
                           "eligible": dict(Counter(str(row["eligible"]) for row in rows)),
                           "primary_signal_state": dict(Counter(row["primary_signal_state"]["state"] for row in rows)),
                           "missing_primary_tactic_id": sum("primary_tactic_id" not in row for row in rows),
                           "report_chart_overlap": coverage})
    after = digest(database)
    if before != after:
        raise RuntimeError("Database changed during read-only audit; outputs were not written")
    args.output.mkdir(parents=True, exist_ok=True)
    baseline_path = args.output / "original-report.json"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {"database": str(database), "database_sha256_before": before,
             "database_sha256_after": after, "database_size": database.stat().st_size,
             "baseline_report_id": baseline["report_id"], "baseline_generated_at": baseline["generated_at"],
             "baseline_sha256": digest(baseline_path), "empty_table_counts": empty_counts,
             "scan_jobs": scan_audit,
             "signal_observation_identity": ["symbol", "tactic_id", "date"],
             "signal_observations": sum(signal_observations.values()),
             "distinct_signal_identities": len(signal_observations),
             "repeated_signal_identities": sum(count > 1 for count in signal_observations.values()),
             "report_charts": [{k: v for k, v in chart.items() if k != "dates"} for chart in charts]}
    (args.output / "history-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scan_jobs": len(scans), "report_charts": len(charts),
                      "signal_observations": audit["signal_observations"],
                      "distinct_signal_identities": len(signal_observations),
                      "repeated_signal_identities": audit["repeated_signal_identities"],
                      "overlap": [{"job_id": s["job_id"], "rows": len(s["report_chart_overlap"]),
                                   "rows_with_future_bars": sum(any(c["bars_after_scan"] > 0 for c in r["charts"])
                                                                for r in s["report_chart_overlap"])} for s in scan_audit],
                      "database_unchanged": before == after}, ensure_ascii=False))


if __name__ == "__main__":
    main()
