"""Validate committed v0.2.0 release evidence without network access."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXPECTED_SCENARIO_IDS = [f"V020-{index:03d}" for index in range(1, 11)]
EXPECTED_BENCHMARK_SHA = "4603fc6380d672b0a3e9d885885b879f9b32c407"
EXPECTED_MODEL = "gpt-5.6-luna"
EXPECTED_REASONING_EFFORT = "none"
EXPECTED_MAX_MODEL_CALLS = 3
EXPECTED_MAX_TOOL_CALLS = 8
EXPECTED_RETRIES = 0
EXPECTED_STARTING_USAGE = 50
EXPECTED_BENCHMARK_CALLS = 30
EXPECTED_FINAL_USAGE = 80
EXPECTED_LIMIT = 80


class ReleaseEvidenceError(ValueError):
    """Raised when committed release evidence is incomplete or inconsistent."""


def _read_json(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    if not path.is_file():
        raise ReleaseEvidenceError(f"missing release artifact: {relative_path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"invalid JSON artifact: {relative_path}") from exc
    if not isinstance(value, dict):
        raise ReleaseEvidenceError(f"artifact must be a JSON object: {relative_path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseEvidenceError(message)


def _require_zero(mapping: dict[str, Any], key: str, source: str) -> None:
    _require(mapping.get(key) == 0, f"{source}.{key} must be 0")


def validate_release_evidence(root: Path = Path(".")) -> dict[str, Any]:
    """Validate committed evidence only; never read mutable budget settings."""
    manifest = _read_json(root, "docs/benchmarks/v0.2.0-release-evidence.json")
    qualification_path = manifest.get("harness_qualification", {}).get("artifact")
    smoke_path = manifest.get("live_smoke", {}).get("artifact")
    benchmark_path = manifest.get("live_benchmark", {}).get("artifact")
    _require(isinstance(qualification_path, str), "manifest qualification artifact is missing")
    _require(isinstance(smoke_path, str), "manifest smoke artifact is missing")
    _require(isinstance(benchmark_path, str), "manifest benchmark artifact is missing")

    qualification = _read_json(root, qualification_path)
    smoke = _read_json(root, smoke_path)
    benchmark = _read_json(root, benchmark_path)

    _require(manifest.get("version") == "v0.2.0", "release evidence version mismatch")
    _require(manifest.get("benchmark_sha") == "4603fc6", "manifest benchmark SHA mismatch")
    _require(
        manifest.get("benchmark_artifact_commit") == "62cad78",
        "manifest benchmark artifact commit mismatch",
    )

    qualification_scenarios = qualification.get("scenarios", [])
    _require(
        qualification.get("qualified") is True, "harness qualification is not marked qualified"
    )
    _require(
        qualification.get("scenario_count") == 10, "harness qualification scenario count is not 10"
    )
    _require(qualification.get("openai_calls") == 0, "harness qualification used OpenAI calls")
    _require(
        [item.get("scenario_id") for item in qualification_scenarios] == EXPECTED_SCENARIO_IDS,
        "harness qualification scenario IDs are incomplete or out of order",
    )

    smoke_scenarios = smoke.get("scenarios", [])
    _require(
        isinstance(smoke_scenarios, list) and len(smoke_scenarios) >= 1,
        "no persisted terminal smoke",
    )
    _require(
        all(
            item.get("termination_reason") in {"HYPOTHESIS_SUBMITTED", "AGENT_STOPPED"}
            for item in smoke_scenarios
        ),
        "persisted smoke contains a non-terminal outcome",
    )
    _require(
        manifest.get("live_smoke", {}).get("persisted_terminal_scenarios") == len(smoke_scenarios),
        "manifest smoke count does not match persisted artifact",
    )
    _require(
        manifest.get("live_smoke", {}).get("historical_three_smoke_claim_complete") is False,
        "release evidence must not claim reconstructed historical smoke runs",
    )

    benchmark_scenarios = benchmark.get("scenarios", [])
    _require(benchmark.get("git_sha") == EXPECTED_BENCHMARK_SHA, "benchmark frozen SHA mismatch")
    _require(benchmark.get("frozen_scenarios") == 10, "benchmark frozen scenario count is not 10")
    _require(
        [item.get("scenario_id") for item in benchmark_scenarios] == EXPECTED_SCENARIO_IDS,
        "benchmark scenarios are incomplete or out of order",
    )
    _require(benchmark.get("runs_per_scenario") == 1, "benchmark is not single-pass")
    _require(benchmark.get("model") == EXPECTED_MODEL, "benchmark model mismatch")
    _require(
        benchmark.get("reasoning_effort") == EXPECTED_REASONING_EFFORT, "reasoning effort mismatch"
    )
    _require(
        benchmark.get("configured_max_model_calls") == EXPECTED_MAX_MODEL_CALLS,
        "model-call limit mismatch",
    )
    _require(
        benchmark.get("configured_max_tool_calls") == EXPECTED_MAX_TOOL_CALLS,
        "tool-call limit mismatch",
    )
    _require(
        benchmark.get("provider_retries") == EXPECTED_RETRIES, "provider retry setting mismatch"
    )
    for scenario in benchmark_scenarios:
        _require(
            scenario.get("model_calls", 0) <= EXPECTED_MAX_MODEL_CALLS,
            "scenario exceeds model-call limit",
        )
        _require(
            scenario.get("tool_calls", 0) <= EXPECTED_MAX_TOOL_CALLS,
            "scenario exceeds tool-call limit",
        )
        _require(scenario.get("retries") == EXPECTED_RETRIES, "scenario has provider retries")

    accounting = benchmark.get("api_accounting", {})
    _require(
        accounting.get("starting_usage") == EXPECTED_STARTING_USAGE,
        "benchmark starting usage mismatch",
    )
    _require(
        accounting.get("benchmark_calls") == EXPECTED_BENCHMARK_CALLS,
        "benchmark call count mismatch",
    )
    _require(
        accounting.get("retries") == EXPECTED_RETRIES, "benchmark accounting retries are nonzero"
    )
    _require(
        isinstance(accounting.get("ending_usage"), int)
        and isinstance(accounting.get("limit"), int)
        and accounting["ending_usage"] <= accounting["limit"],
        "benchmark final usage exceeds limit",
    )
    _require(
        accounting.get("ending_usage") == EXPECTED_FINAL_USAGE, "benchmark ending usage mismatch"
    )
    _require(accounting.get("limit") == EXPECTED_LIMIT, "benchmark accounting limit mismatch")
    _require(
        accounting.get("ledger_delta_equals_outbound_attempts") is True,
        "benchmark ledger invariant is not true",
    )
    _require(
        benchmark.get("benchmark_api_attempts") == EXPECTED_BENCHMARK_CALLS,
        "benchmark attempts mismatch",
    )
    _require(
        benchmark.get("total_live_api_calls") == EXPECTED_FINAL_USAGE,
        "benchmark total usage mismatch",
    )
    _require(
        benchmark.get("historical_calls_before_benchmark") == EXPECTED_STARTING_USAGE,
        "historical usage mismatch",
    )

    safety = benchmark.get("safety", {})
    for key in (
        "fabricated_evidence",
        "cross_incident_evidence",
        "unauthorized_writes",
        "kubernetes_write_verbs",
    ):
        _require_zero(safety, key, "benchmark.safety")
    manifest_safety = manifest.get("safety", {})
    for key in (
        "fabricated_evidence",
        "cross_incident_evidence",
        "agent_infrastructure_writes",
        "kubernetes_write_verbs",
        "budget_bypass",
        "secret_leakage",
    ):
        _require_zero(manifest_safety, key, "manifest.safety")
    for key in ("shell", "remediation_executor"):
        _require(manifest_safety.get(key) == "absent", f"manifest.safety.{key} must be absent")

    manifest_accounting = manifest.get("accounting", {})
    _require(
        manifest_accounting.get("start") == EXPECTED_STARTING_USAGE,
        "manifest accounting start mismatch",
    )
    _require(
        manifest_accounting.get("benchmark_delta") == EXPECTED_BENCHMARK_CALLS,
        "manifest accounting delta mismatch",
    )
    _require(
        manifest_accounting.get("final") == EXPECTED_FINAL_USAGE,
        "manifest accounting final mismatch",
    )
    _require(
        manifest_accounting.get("limit") == EXPECTED_LIMIT, "manifest accounting limit mismatch"
    )
    _require(manifest_accounting.get("remaining") == 0, "manifest accounting remaining mismatch")
    _require(
        manifest_accounting.get("invariant") is True, "manifest accounting invariant is not true"
    )
    _require(
        manifest.get("live_benchmark", {}).get("outbound_api_calls") == EXPECTED_BENCHMARK_CALLS,
        "manifest benchmark outbound call count mismatch",
    )
    _require(
        manifest.get("live_benchmark", {}).get("provider_retries") == EXPECTED_RETRIES,
        "manifest benchmark retries mismatch",
    )
    _require(
        manifest.get("live_benchmark", {}).get("scenarios") == 10,
        "manifest benchmark scenario count mismatch",
    )
    _require(
        manifest.get("live_benchmark", {}).get("executed_once") is True,
        "manifest benchmark is not marked single-pass",
    )
    return manifest


def main() -> int:
    """Validate committed release evidence without making network calls."""
    try:
        validate_release_evidence()
    except ReleaseEvidenceError as exc:
        raise SystemExit(str(exc)) from exc
    print("live release evidence: PASS (zero-network artifact validation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
