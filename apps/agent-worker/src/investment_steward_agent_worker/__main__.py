from __future__ import annotations

import argparse
import json
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint
from investment_steward_agent_worker.client import CoreClient
from investment_steward_agent_worker.scheduler import run_tick


def main() -> None:
    parser = argparse.ArgumentParser(description="Investment Steward Agent Worker")
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--core-url", type=str, required=True)
    parser.add_argument("--session-token", type=str, required=True)
    parser.add_argument("--tick-seconds", type=int, default=60)
    args = parser.parse_args()

    stop_waiter = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stop_waiter.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = PersistentCheckpoint(args.status_file.with_name("agent-worker.checkpoint.jsonl"))
    client = CoreClient(args.core_url, args.session_token, timeout=15)

    def write_status(kind: str, **extra: object) -> None:
        payload = {
            "status": kind,
            "version": "0.2.0",
            "ts": datetime.now(UTC).isoformat(),
            "tool_calls": len(checkpoint.tool_calls),
            **extra,
        }
        args.status_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    write_status("running", reason="start")
    while not stop_waiter.is_set():
        performed = run_tick(client, checkpoint)
        done_count = sum(1 for entry in performed if entry.ok)
        write_status(
            "running",
            reason="tick",
            ran=len(performed),
            ok=done_count,
            last_at=datetime.now(UTC).isoformat(),
        )
        stop_waiter.wait(timeout=float(args.tick_seconds))

    write_status("stopped")
    stop_waiter.set()


if __name__ == "__main__":
    main()