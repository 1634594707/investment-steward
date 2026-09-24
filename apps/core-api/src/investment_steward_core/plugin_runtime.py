"""E1 · 插件运行不变式的策略源：network_allowlist 聚合 + 进程零密钥可观测面。

设计原则（对齐 G3-5「插件零密钥」）：
- 插件代码不进 Core 进程；Core 也不把任何凭据放进 runner 子进程环境。
- 出网白名单由 Core 聚合各能力声明（`_capabilities`）后，经 env 注入 runner；
  runner 侧以 `STEWARD_NETWORK_ALLOWLIST` 二次门控（见 plugin-runner.protocol）。
- `secret_env_markers` 是只读扫描，产出可观测的密钥泄漏清单，供 `/runner/config` 审计；
  它不改动任何环境变量（Core 进程自身可能持有数据源 token，这里只报告不阻断）。
"""
from __future__ import annotations

import os

# 与 plugin-runner 对称的密钥标记；两处独立维护，避免跨包硬耦。
_SECRET_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "APIKEY",
    "API_KEY",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "CREDENTIAL",
)


def secret_env_markers() -> list[str]:
    """返回 Core 进程环境中命中疑似密钥标记的变量名（仅名字，不暴露值）。"""
    return sorted(
        name for name in os.environ if any(marker in name.upper() for marker in _SECRET_KEY_MARKERS)
    )


def aggregate_network_allowlist() -> list[str]:
    """聚合全部能力声明的出网白名单，作为 runner 出网门控的注入值（去重排序）。"""
    from investment_steward_core.api.app import _capabilities

    merged: set[str] = set()
    for capability in _capabilities():
        merged.update(capability.network_allowlist)
    return sorted(merged)