"""压测前置探测：对候选端点逐个发单次请求，记录状态码 / 耗时 / 响应体大小。

用途：在正式压测前把「不可用 / 会外呼 / 参数缺失」的端点剔除，避免压测打的是
一堆 404 或真实外网请求，导致指标失真。

用法：
    .venv/Scripts/python.exe scripts/perf_probe.py --out .runtime/perf-probe.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:18765"
TOKEN = "manual-test-token"
HEADERS = {"X-Core-Session-Token": TOKEN}

# 候选清单来源：apps/web-shell/src 里前端实际引用的路径，与
# docs/architecture/route-inventory-final.json 交叉筛选后的只读业务端点。
CANDIDATES = [
    "/health",
    "/session",
    "/overview",
    "/brief/today",
    "/holdings",
    "/holdings/by-instrument",
    "/evidence",
    "/decisions",
    "/judgments",
    "/plans",
    "/research/runs",
    "/research/snapshots",
    "/research/templates",
    "/learning/activities",
    "/notifications/pending",
    "/notifications/channels",
    "/personal/settings",
    "/investor/profile",
    "/investment-policies",
    "/model-profiles",
    "/library/books",
    "/watch-items",
    "/slots",
    "/portfolio/risk",
    "/quant/experiments",
    "/quant/artifacts",
    "/quant/stages",
    "/channels",
    "/credentials",
    "/data-source/quality",
    "/plugins/catalog",
    "/capabilities",
]

ROUNDS = 3


TIMEOUT_S = 5.0


def probe(client: httpx.Client, path: str, rounds: int = ROUNDS, timeout: float = TIMEOUT_S) -> dict:
    samples: list[float] = []
    status = None
    size = 0
    error = None
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            resp = client.get(BASE + path, headers=HEADERS, timeout=timeout)
            samples.append((time.perf_counter() - t0) * 1000)
            status = resp.status_code
            size = max(size, len(resp.content))
        except Exception as exc:  # noqa: BLE001 - 探测阶段需要吞掉所有异常继续跑
            error = type(exc).__name__ + ": " + str(exc)[:80]
            break
        time.sleep(0.05)
    return {
        "path": path,
        "status": status,
        "error": error,
        "bytes": size,
        "p50_ms": round(statistics.median(samples), 2) if samples else None,
        "max_ms": round(max(samples), 2) if samples else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".runtime/perf-probe.json")
    parser.add_argument("--rounds", type=int, default=1, help="每个端点采样轮数")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results = []
    with httpx.Client() as client:
        for path in CANDIDATES:
            r = probe(client, path, args.rounds, args.timeout)
            results.append(r)
            flag = "OK " if r["status"] == 200 else "BAD"
            print(f"{flag} {r['status']} {str(r['p50_ms']):>8}ms {r['bytes']:>8}B  {path} {r['error'] or ''}")

    ok = [r for r in results if r["status"] == 200]
    ok.sort(key=lambda r: r["p50_ms"] or 0, reverse=True)
    out_path.write_text(
        json.dumps({"base": BASE, "rounds": args.rounds, "results": results, "ok_count": len(ok)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n{len(ok)}/{len(results)} endpoints healthy -> {out_path}")


if __name__ == "__main__":
    main()
