"""Qualify the frozen A1 generalization fixtures without invoking a model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from packages.contracts import Alert, Incident, TimeWindow
from packages.evals import (
    A1_GENERALIZATION_SCENARIOS,
    GENERALIZATION_FIXTURE_BY_NAME,
    FixtureDefinition,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    generalization_dataset_hash,
    generalization_target_hash,
)
from packages.investigation.registry import live_observability_registry
from packages.tools import BoundedToolExecutor, ControlPlaneChangeReader, ToolFailure, ToolResponse

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/a1-generalization-qualification.json"
REPEAT_COUNT = 2
PROMETHEUS_CONFIGMAP = ("kubectl", "get", "configmap", "prometheus-config", "-n", "observability")


def _prometheus_rules() -> str:
    """Read the active rules so qualification can restore them exactly."""
    result = subprocess.run(
        [*PROMETHEUS_CONFIGMAP, "-o", r"jsonpath={.data.rules\.yml}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _apply_prometheus_rules(rules: str) -> None:
    """Apply a qualification-only alert identity change."""
    subprocess.run(
        [
            "kubectl",
            "patch",
            "configmap",
            "prometheus-config",
            "-n",
            "observability",
            "--type=json",
            "-p",
            json.dumps([{"op": "replace", "path": "/data/rules.yml", "value": rules}]),
        ],
        check=True,
        capture_output=True,
    )
    request = Request("http://localhost:19090/-/reload", method="POST")
    with urlopen(request, timeout=10):
        pass


def _restart_prometheus() -> None:
    """Make a ConfigMap-backed rules file visible before a qualification pass."""
    subprocess.run(
        ["kubectl", "rollout", "restart", "deployment/prometheus", "-n", "observability"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/prometheus",
            "-n",
            "observability",
            "--timeout=180s",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urlopen("http://localhost:19090/-/ready", timeout=5):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError("Prometheus did not become ready after rules restart")


def _qualified_alert_rules(original: str, canonical_name: str, qualified_name: str) -> str:
    """Return the active rules with one alert renamed for a fresh fingerprint."""
    marker = f"      - alert: {canonical_name}\n"
    if original.count(marker) != 1:
        raise RuntimeError(f"alert rule is not uniquely present: {canonical_name}")
    return original.replace(marker, f"      - alert: {qualified_name}\n", 1)


def _qualified_definition(scenario: Any, qualified_name: str) -> FixtureDefinition:
    """Build qualification-only metadata without changing the frozen scenario."""
    base = GENERALIZATION_FIXTURE_BY_NAME[scenario.fixture]
    return FixtureDefinition(
        fixture=base.fixture,
        alert_name=qualified_name,
        service=base.service,
        primary_tools=base.primary_tools,
    )


def _window(incident: Incident, alerts: tuple[Alert, ...]) -> TimeWindow:
    """Build the authoritative bounded window from normalized alerts."""
    starts = min([incident.created_at, *(item.starts_at for item in alerts)])
    ends = [item.ends_at for item in alerts if item.ends_at is not None]
    return TimeWindow(starts_at=starts, ends_at=max(ends) if ends else datetime.now(UTC))


def _preflight(
    registry: Any,
    incident: Incident,
    alerts: tuple[Alert, ...],
    surfaces: list[Any],
) -> list[dict[str, Any]]:
    """Execute each evaluator-only observability surface and retain bounded proof."""
    window = _window(incident, alerts)
    executor = BoundedToolExecutor()
    results: list[dict[str, Any]] = []
    for surface in surfaces:
        registered = registry.get(surface.tool)
        request = registered.request(
            incident.incident_id,
            surface.arguments,
            observation_window={
                "starts_at": window.starts_at.isoformat(),
                "ends_at": window.ends_at.isoformat(),
            },
        )
        result = executor.execute(registered.tool, request)
        if isinstance(result, ToolFailure):
            raise RuntimeError(f"surface failed: {surface.tool}:{result.code.value}")
        if not isinstance(result, ToolResponse) or result.result_count < 1:
            raise RuntimeError(f"surface returned no observable result: {surface.tool}")
        results.append(
            {
                "tool": surface.tool,
                "arguments": surface.arguments,
                "result_count": result.result_count,
                "temporal_mode": result.temporal_mode,
            }
        )
    return results


def _check_scenario(
    lifecycle: FixtureLifecycle,
    registry: Any,
    scenario: Any,
    qualified_alert_name: str,
) -> dict[str, Any]:
    """Run one fixture once and verify alert, incident, surfaces and recovery."""
    observed: dict[str, Any] = {}

    def investigate(incident: Incident, alerts: tuple[Alert, ...]) -> list[dict[str, Any]]:
        matching = [item for item in alerts if item.alert_name == qualified_alert_name]
        if len(matching) != 1 or matching[0].service != scenario.alert_scope_component.value:
            raise RuntimeError(f"alert mapping mismatch: {scenario.scenario_id}")
        observed["alert"] = {
            "name": matching[0].alert_name,
            "service": matching[0].service,
            "status": matching[0].status.value,
            "started": matching[0].starts_at.isoformat(),
        }
        observed["incident_id"] = str(incident.incident_id)
        return _preflight(registry, incident, alerts, scenario.observable_evidence_surfaces)

    trial, surfaces = lifecycle.run(
        scenario,
        investigate=investigate,
        snapshot_before_prepare=True,
    )
    if trial.incident_id is None:
        raise RuntimeError(f"no incident identity: {scenario.scenario_id}")
    if not trial.alert_resolved:
        raise RuntimeError(f"alert did not resolve: {scenario.scenario_id}")
    if not trial.baseline_restored:
        raise RuntimeError(f"baseline was not restored: {scenario.scenario_id}")
    return {
        "scenario_id": scenario.scenario_id,
        "fixture": scenario.fixture,
        "alert_name": scenario.alert_name,
        "qualified_alert_name": qualified_alert_name,
        "fault_injection": trial.fault_started_at is not None,
        "real_alert": trial.alert_fingerprint is not None,
        "real_incident": trial.incident_id is not None,
        "incident_id": str(trial.incident_id),
        "alert": observed.get("alert"),
        "observed_surfaces": surfaces,
        "cleanup": trial.cleanup_finished_at is not None,
        "resolution": trial.alert_resolved,
        "baseline_restored": trial.baseline_restored,
        "openai_calls": 0,
    }


def main() -> int:
    """Run each frozen generalization fixture twice with no provider dependency."""
    output_path = Path(os.getenv("A1_GENERALIZATION_QUALIFICATION_OUTPUT", str(OUTPUT)))
    environment = LiveBenchmarkEnvironment()
    environment.prepare_benchmark_state("a1-generalization-qualification")
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    original_prometheus_rules = _prometheus_rules()
    qualification_token = uuid4().hex[:8]
    records: list[dict[str, Any]] = []
    try:
        for repeat in range(1, REPEAT_COUNT + 1):
            qualified_names: dict[str, str] = {}
            rules = original_prometheus_rules
            for scenario in A1_GENERALIZATION_SCENARIOS:
                qualified_name = f"{scenario.alert_name}Q{qualification_token}{repeat}"
                rules = _qualified_alert_rules(rules, scenario.alert_name, qualified_name)
                qualified_names[scenario.scenario_id] = qualified_name
            _apply_prometheus_rules(rules)
            _restart_prometheus()
            for scenario in A1_GENERALIZATION_SCENARIOS:
                qualified_name = qualified_names[scenario.scenario_id]
                definition = _qualified_definition(scenario, qualified_name)
                lifecycle = FixtureLifecycle(
                    environment,
                    definitions={**GENERALIZATION_FIXTURE_BY_NAME, scenario.fixture: definition},
                )
                record = _check_scenario(lifecycle, registry, scenario, qualified_name)
                record["repeat"] = repeat
                records.append(record)
                print(f"{scenario.scenario_id} repeat {repeat} PASS", flush=True)
    finally:
        _apply_prometheus_rules(original_prometheus_rules)
        _restart_prometheus()
    report = {
        "artifact_type": "A1_GENERALIZATION_QUALIFICATION",
        "status": "PASS",
        "openai_calls": 0,
        "network_model_calls": 0,
        "scenario_count": len(A1_GENERALIZATION_SCENARIOS),
        "repeat_count": REPEAT_COUNT,
        "dataset_sha256": generalization_dataset_hash(),
        "target_sha256": generalization_target_hash(),
        "scenarios": records,
        "note": "Real fault-to-telemetry-to-alert-to-incident qualification only; no model quality result.",
    }
    output_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print("A1 generalization qualification: PASS (real fault path, 0 OpenAI calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
