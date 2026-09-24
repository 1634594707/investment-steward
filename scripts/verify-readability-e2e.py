"""v2.2 研报可读性真实端到端核验（不起服务、不碰系统凭据管理器）。

背景（2026-09-10 用户实测 000060）：报告里出现 `MA20 6.648999999999999`、
`DIF -0.006371615255064356`、`revenue=42194101356.98` 这类机器精度裸浮点，
读起来无法判断；且 5 条验证点的「验证时点」全是 `2026-10前`。

做法（沿用 verify-valuation-e2e.py 的 file_client 模式）：
- TestClient + 临时数据目录，凭据后端换成文件库（`resolve_store` → DbCredentialStore），
  **绝不写系统 CredMan**；
- 行情/新闻/公告/财报/估值一律走**真实网络**（不 monkeypatch 任何 fetch_*）；
- 只把模型调用替换成捕获器，从而拿到真实拼装出的证据文本。

输出：.runtime-acceptance/readability-e2e.log（同时打印到 stdout）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / ".runtime-acceptance" / "readability-e2e.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

lines: list[str] = []

# 机器精度裸浮点：7 位及以上小数（展示精度最高为 MACD 的 4 位）。
MACHINE_FLOAT = re.compile(r"\d\.\d{7,}")


def log(message: str = "") -> None:
    print(message)
    lines.append(message)


def main() -> int:
    from fastapi.testclient import TestClient

    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    data_dir = ROOT / ".runtime-acceptance" / "readability-e2e-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 关键：凭据走文件库，避免覆盖用户系统凭据管理器里的真实模型 key。
    api_app.resolve_store = lambda db: DbCredentialStore(db)  # type: ignore[assignment]

    token = "e2e-readability-token"
    app = create_app(CoreSettings(session_token=token, data_dir=data_dir))
    client = TestClient(app)
    headers = {"X-Core-Session-Token": token}

    installed = client.post("/plugins/official.cn-market-data/install", headers=headers)
    log(f"[1] 安装 cn-market-data: {installed.status_code}")

    client.put("/credentials/model_api_key", json={"secret": "sk-e2e-not-a-real-key"}, headers=headers)
    created = client.post(
        "/model-profiles",
        json={"name": "E2E", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers)
    log(f"[2] 模型方案就绪 profile_id={created['profile_id']}")

    symbol = sys.argv[1] if len(sys.argv) > 1 else "000060"
    captured: dict[str, str] = {}

    # 刻意回放用户实测的坏形态：5 条验证点全部「2026-10前」，用于验证质量闸门告警。
    report_json = json.dumps({
        "title": f"{symbol}：可读性核验",
        "report": ("### 技术面\n量价结构见 [S1]\n### 消息面\nno news [S2]\n"
                   "### 基本面\n估值与 ROE 见 [S4][S5]\n### 综合判断\n结论 [S1][S5]"),
        "citations": ["S1", "S2", "S4", "S5"],
        "limitations": ["公开来源摘要口径"],
        "confidence": "medium",
        "valuation": {"pe_ttm": "见 S5", "pe_percentile": "见 S5",
                      "peer_position": "见 S5", "verdict": "合理", "basis": "S5 分位与同业"},
        "watchpoints": [
            {"signal": f"信号{i}", "verify_by": "2026-10前", "expected_if_true": "x"} for i in range(5)
        ],
    }, ensure_ascii=False)

    from investment_steward_core import model_client as mc

    def _capture(*args, **kwargs):
        captured["blob"] = json.dumps(
            [str(item) for item in args] + [str(value) for value in kwargs.values()], ensure_ascii=False
        )
        return {"choices": [{"message": {"content": report_json}}]}

    mc._post_json = _capture  # type: ignore[assignment]

    log(f"[3] 发起真实取数研报（{symbol}），行情/新闻/公告/财报/估值均走真实网络 ...")
    response = client.post(
        "/evidence/stock-research-report",
        json={"symbol": symbol, "question": "当前估值贵不贵，验证时点怎么安排"},
        headers=headers,
    )
    body = response.json()
    log(f"    status={response.status_code} ok={body.get('ok')} stage={body.get('stage')}")
    log(f"    source_keys={body.get('source_keys')}")
    log(f"    source_errors={body.get('source_errors')}")
    log(f"    policy={body.get('prompt_policy_version')}")
    warnings = body.get("quality_warnings") or []
    log("    质量告警：")
    for item in warnings:
        log(f"      - {item}")

    blob = captured.get("blob", "").replace("\\n", "\n")

    log("")
    log("=== 证据文本（真实数据，人类可读性肉眼核验）===")
    for label, marker in (("[S1] 技术面", "指标 {"), ("[S4] 财务摘要", "[S4] 财务摘要"), ("[S5] 估值快照", "[S5] 估值快照")):
        index = blob.find(marker)
        if index < 0:
            log(f"!! 未在 prompt 中找到：{marker}")
            continue
        start = max(0, index - 160)
        log(f"--- {label} ---")
        log(blob[start:start + 620])
        log("")

    hits = MACHINE_FLOAT.findall(blob)
    log("=== 断言 ===")
    checks = {
        "证据文本无机器精度裸浮点（无 7 位以上小数）": not hits,
        "证据文本含 MA20 等指标（未被顺手删掉）": "ma20" in blob,
        "S4 金额已按亿/万换单位": ("亿元" in blob and "万元" in blob),
        "提示词策略版本 2.2": body.get("prompt_policy_version") == "2.2",
        "提示词含铁律 7（验证时点须具体）": "7. 验证点清单" in blob,
        "提示词无泄漏示例日期「2026-10 前」": "2026-10 前」" not in blob,
        "质量闸门告警「验证时点完全相同」": any("验证时点完全相同" in item for item in warnings),
        "质量闸门告警「只写到月份」": any("只写到月份" in item for item in warnings),
    }
    failed = 0
    for name, ok in checks.items():
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1
    if hits:
        log(f"  命中样本（前 5 个）：{hits[:5]}")

    LOG.write_text("\n".join(lines), encoding="utf-8")
    log("")
    log(f"日志已写入 {LOG}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
