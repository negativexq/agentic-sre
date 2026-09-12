"""Run three explicit live investigations against existing incidents."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from packages.contracts import Alert, Incident
from packages.investigation import InvestigationLimits, InvestigationRuntime
from packages.investigation.registry import live_observability_registry
from packages.provider import LiveModelBudget, OpenAIProvider
from packages.tools import ControlPlaneChangeReader

CONTROL_PLANE_URL = "http://localhost:18081"


def _get_incidents() -> list[Incident]:
    """Read incidents from the live control plane without writing state."""
    try:
        with urlopen(f"{CONTROL_PLANE_URL}/api/v1/incidents", timeout=10) as response:
            payload = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("control plane incident lookup failed") from error
    if not isinstance(payload, list):
        raise RuntimeError("control plane returned an invalid incident list")
    return [Incident.model_validate_json(json.dumps(item)) for item in payload]


def _get_alerts(incident_id: str) -> tuple[Alert, ...]:
    """Load normalized alert scope from the control plane for model context."""
    try:
        with urlopen(
            f"{CONTROL_PLANE_URL}/api/v1/incidents/{incident_id}/alerts", timeout=10
        ) as response:
            payload = json.loads(response.read(1_000_001))
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError("control plane alert lookup failed") from error
    if not isinstance(payload, list):
        raise RuntimeError("control plane returned an invalid alert list")
    return tuple(Alert.model_validate_json(json.dumps(item)) for item in payload)


def _selected_incidents() -> list[Incident]:
    """Select caller-provided incidents, or the historical three by default."""
    incidents = _get_incidents()
    requested = os.getenv("SRE_LIVE_INCIDENT_IDS")
    if requested:
        wanted = [item.strip() for item in requested.split(",") if item.strip()]
        by_id = {str(item.incident_id): item for item in incidents}
        selected = [by_id[item] for item in wanted if item in by_id]
    else:
        selected = sorted(incidents, key=lambda item: item.created_at, reverse=True)[:3]
    if len(selected) not in {1, 3}:
        raise RuntimeError("agent-smoke-live requires one or three existing incidents")
    return selected


def main() -> int:
    """Run at most nine live model calls after worst-case preflight."""
    budget = LiveModelBudget.from_environment(require_shared_ledger=True)
    before = budget.snapshot()
    selected = _selected_incidents()
    budget.ensure_capacity(len(selected) * 3)
    print(
        json.dumps(
            {
                "budget_limit": before.limit,
                "calls_used": before.calls_used,
                "calls_remaining": before.calls_remaining,
                "shared_ledger_enabled": budget.shared_ledger_enabled,
                "ledger_path": budget.ledger_path,
            },
            sort_keys=True,
        )
    )
    provider = OpenAIProvider(budget=budget, max_retry=0)
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader(f"{CONTROL_PLANE_URL}/api/v1/changes").query,
    )
    results: list[dict[str, Any]] = []
    for incident in selected:
        result = InvestigationRuntime(
            provider,
            registry,
            model="gpt-5.6-luna",
            reasoning_effort="none",
            limits=InvestigationLimits(),
        ).run(incident, alerts=_get_alerts(str(incident.incident_id)))
        results.append(
            {
                "incident_id": str(incident.incident_id),
                "termination_reason": result.termination_reason.value,
                "terminal_decision": (
                    result.terminal_decision.value if result.terminal_decision else None
                ),
                "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "error_code": result.error_code,
                "validation_stage": (
                    result.validation_stage.value if result.validation_stage else None
                ),
                "validation_path": result.validation_path,
                "validator": result.validator,
                "hypothesis_submitted": result.hypothesis is not None,
                "evidence_count": len(result.evidence),
                "model_calls_used": result.usage.model_calls,
                "model_calls_limit": result.usage.model_calls_limit,
                "model_budget_exhausted_after_terminal_decision": (
                    result.usage.model_budget_exhausted_after_terminal_decision
                ),
                "actual_api_calls": result.usage.actual_api_calls,
                "provider_invocations": result.usage.provider_invocations,
                "outbound_api_attempts": result.usage.outbound_api_attempts,
                "provider_retries": result.usage.provider_retries,
                "tool_calls_used": result.usage.tool_calls,
                "turns": result.turns,
            }
        )
    after = budget.snapshot()
    outbound_attempts = sum(item["outbound_api_attempts"] for item in results)
    budget.verify_ledger_delta(before, after, outbound_attempts)
    payload = {
        "scenarios": results,
        "total_live_api_calls": after.calls_used,
        "budget": {
            "limit": after.limit,
            "calls_used_before": before.calls_used,
            "calls_used_after": after.calls_used,
            "outbound_api_attempts": outbound_attempts,
            "shared_ledger_enabled": budget.shared_ledger_enabled,
        },
    }
    Path("docs/benchmarks/v0.2.0-live-smoke.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, sort_keys=True))
    successful = all(
        item["termination_reason"] in {"HYPOTHESIS_SUBMITTED", "AGENT_STOPPED"}
        and item["terminal_decision"] in {"SUBMIT_HYPOTHESIS", "STOP"}
        for item in results
    )
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
