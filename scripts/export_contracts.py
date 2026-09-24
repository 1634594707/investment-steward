"""Export Pydantic JSON Schema into schemas/ for review and cross-language consumers."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "apps" / "core-api" / "src"
sys.path.insert(0, str(CORE_SRC))

from investment_steward_core.domain import AgentResponse, Evidence, InvestmentPolicyVersion, PluginCapability  # noqa: E402


MODELS = {
    "investment-policy-version": InvestmentPolicyVersion,
    "evidence": Evidence,
    "plugin-capability": PluginCapability,
    "agent-response": AgentResponse,
}


def main() -> None:
    schema_dir = ROOT / "schemas"
    for name, model in MODELS.items():
        output = model.model_json_schema(mode="serialization")
        output["$id"] = f"https://investment-steward.local/schemas/{name}/1.0"
        (schema_dir / f"{name}.schema.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
