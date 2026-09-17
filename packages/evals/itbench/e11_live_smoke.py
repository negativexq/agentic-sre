"""Explicitly authorized, single-scenario E11 live-smoke entry point.

This module is intentionally fail-closed.  The default command path never
constructs an OpenAI provider; a human must pass the explicit authorization
flag after reviewing the frozen manifest.  The authorized path reuses the
same E11InvestigationRuntime and JSON output boundary as the official run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchAgentOutput
from packages.evals.itbench.e11_official import E11OfficialManifestV1, load_e11_manifest
from packages.evals.itbench.persistence import atomic_json_write


class E11LiveSmokeAuthorizationError(RuntimeError):
    """Live provider execution was not explicitly authorized."""


def require_live_authorization(authorized: bool) -> None:
    """Fail before provider construction unless the human opt-in is present."""
    if not authorized:
        raise E11LiveSmokeAuthorizationError(
            "E11 live provider execution is disabled; pass --authorize-live-provider"
        )


def run_authorized_single_scenario(
    *,
    root: Path,
    dataset_root: Path,
    manifest: E11OfficialManifestV1,
    scenario_id: str,
    output: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    """Run exactly one scenario through the production E11 provider boundary.

    This function is not called by offline qualification.  It exists as the
    reviewed repository-owned entry point for a later human-authorized smoke.
    """
    if scenario_id not in manifest.scenario_order:
        raise ValueError(f"scenario is not in the frozen E11 order: {scenario_id}")
    from packages.evals.itbench.dataset import ITBenchLiteDataset
    from packages.evals.itbench.e11_runtime import E11InvestigationRuntime, E11RuntimeLimits
    from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
    from packages.model_policy import ModelPolicyError, validate_agent_environment
    from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

    config = live_model_config()
    if not config.enabled or config.model != "gpt-5.6-luna" or config.reasoning_effort != "none":
        raise E11LiveSmokeAuthorizationError("live Luna configuration is not explicitly enabled")
    try:
        environment = validate_agent_environment()
    except ModelPolicyError as error:
        raise E11LiveSmokeAuthorizationError(error.code) from error
    if environment.model != manifest.model or environment.reasoning != manifest.reasoning_effort:
        raise E11LiveSmokeAuthorizationError("environment disagrees with frozen manifest")
    if manifest.provider_retries != 0:
        raise E11LiveSmokeAuthorizationError("provider retries must be zero")

    dataset = ITBenchLiteDataset.open(dataset_root)
    scenario = dataset._load_scenario(scenario_id)
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
    budget = LiveModelBudget(manifest.runtime_limits.max_model_calls, ledger_path=str(ledger_path))
    provider = OpenAIProvider(budget=budget, config=config, max_retry=0)
    limits = E11RuntimeLimits(**manifest.runtime_limits.model_dump())
    result = E11InvestigationRuntime(
        provider, backend, limits=limits, execution_id=f"{manifest.execution}:{scenario_id}"
    ).run()
    # Keep the same canonical boundary used by predict_e11 before persisting.
    ITBenchAgentOutput.from_json_value(result.get("agent_output"))
    atomic_json_write(output, result)
    return {
        "execution": manifest.execution,
        "scenario_id": scenario_id,
        "model": manifest.model,
        "reasoning_effort": manifest.reasoning_effort,
        "provider_retries": manifest.provider_retries,
        "terminal": result.get("terminal"),
        "output": str(output),
        "provider_invocations": len(getattr(provider, "requests", ())),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must contain a JSON object")
    return value


__all__ = [
    "E11LiveSmokeAuthorizationError",
    "load_e11_manifest",
    "load_json",
    "require_live_authorization",
    "run_authorized_single_scenario",
]
