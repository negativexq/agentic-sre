"""Frozen-fixture DEV proof; an experimental guard, never a production RCA rule."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packages.evals.rollout_refusal_accounting import assess_rollout_refusal_accounting
from packages.rca import resolution
from packages.rca.causal_closure import SHARED_EVIDENCE_RULE
from packages.rca.claims import actor_findings
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import (
    Alert,
    CausalExplanation,
    ClusterEvent,
    Diagnosis,
    FindingKind,
    Lifecycle,
    LogRecord,
    ObjectVersion,
    ResolutionTrace,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.source import InMemorySource

FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests/unit/evals/fixtures/current_refusal_rollout.json"
)


def source() -> InMemorySource:
    data = json.loads(FIXTURE.read_text())
    return InMemorySource(
        name=data["name"],
        alert_items=[Alert.model_validate(r) for r in data["alert_items"]],
        versions=[ObjectVersion.model_validate(r) for r in data["versions"]],
        error_items=[LogRecord.model_validate(r) for r in data["error_items"]],
        trace_items=[TraceSpanObservation.model_validate(r) for r in data["trace_items"]],
        event_items=[ClusterEvent.model_validate(r) for r in data["event_items"]],
        cutoff=datetime.fromisoformat(data["cutoff"]),
        alert_coverage_start=datetime.fromisoformat(data["alert_coverage_start"]),
    )


def variant(name: str) -> InMemorySource:
    value = source()
    if name == "missing_current_operations":
        value.trace_items = [
            r for r in value.trace_items if not r.evidence_id.startswith("failed-client:")
        ]
    elif name == "historical_manifestation":
        original = value.error_items[0]
        assert value.alert_coverage_start is not None
        old = original.model_copy(
            update={
                "at": value.alert_coverage_start.replace(minute=2),
                "evidence_id": "historical-log:independent",
                "attributes": {
                    **original.attributes,
                    "trace_id": "f" * 32,
                    "span_id": "e" * 16,
                    "parent_span_id": "d" * 16,
                    "request_id": "historical-request",
                },
            }
        )
        value.error_items.insert(0, old)
    elif name == "failure_already_in_baseline":
        value.trace_items = [
            r for r in value.trace_items if not r.evidence_id.startswith("baseline-server:")
        ]
        value.trace_items = [
            r.model_copy(update={"status": TraceSpanStatus.ERROR})
            if r.evidence_id.startswith("baseline-client:")
            else r
            for r in value.trace_items
        ]
    elif name == "competing_supported_change":
        initial_deployment = next(
            v
            for v in value.versions
            if v.entity.kind == "Deployment" and v.entity.name == "order-service"
        )
        body = deepcopy(initial_deployment.body)
        body["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "FAULT_ORDER_ERROR", "value": "true"}
        )
        assert value.alert_coverage_start is not None
        value.versions.append(
            initial_deployment.model_copy(
                update={
                    "body": body,
                    "observed_at": value.alert_coverage_start.replace(minute=10),
                    "lifecycle": Lifecycle.UPDATED,
                    "evidence_id": "other-change:order-config",
                }
            )
        )
    elif name == "unbound_request":
        value.error_items[0] = value.error_items[0].model_copy(
            update={
                "attributes": {**value.error_items[0].attributes, "request_id": "other-request"}
            }
        )
    elif name == "timeout_out_of_scope":
        value.error_items = [
            r.model_copy(
                update={
                    "message": "dependency.request error: payment-service timed out",
                    "attributes": {**r.attributes, "error.type": "TimeoutError"},
                }
            )
            for r in value.error_items
        ]
        value.trace_items = [
            r.model_copy(
                update={
                    "semantic_attributes": {**r.semantic_attributes, "error.type": "TimeoutError"}
                }
            )
            if r.evidence_id.startswith("failed-client:")
            else r
            for r in value.trace_items
        ]
    elif name != "current_refusal_complete":
        raise ValueError(name)
    return value


def evaluate(name: str) -> dict[str, Any]:
    observed = variant(name)
    case = build_case(observed)
    baseline = diagnose_case(case)
    accounting = assess_rollout_refusal_accounting(case, baseline)
    allowed = {row.claim_id for row in accounting if row.eligible_for_shared_explanation}
    guarded_targets = {
        h.hypothesis_id
        for h in case.hypotheses
        if h.causal_actor.kind == "Service"
        and any(f.kind is FindingKind.DEPENDENCY_ERRORS for f in actor_findings(h))
    }
    original = resolution.explanations  # type: ignore[attr-defined]

    def guarded(*args: Any, **kwargs: Any) -> list[CausalExplanation]:
        return [
            r
            for r in original(*args, **kwargs)
            if not (
                r.rule_id == SHARED_EVIDENCE_RULE
                and r.explained_claim in guarded_targets
                and r.explained_claim not in allowed
            )
        ]

    original_case = deepcopy(case.hypotheses)
    with patch.object(resolution, "explanations", guarded):
        experiment = diagnose_case(case)
    assert case.hypotheses == original_case
    assert resolution.explanations is original  # type: ignore[attr-defined]
    assert baseline.resolution_trace is not None and experiment.resolution_trace is not None

    def brief(d: Diagnosis) -> dict[str, Any]:
        t = d.resolution_trace
        assert t is not None
        return {
            "resolution": d.resolution.value,
            "root": d.root_cause.canonical if d.root_cause else None,
            "supported": t.plausible_hypotheses,
            "strong": t.mechanism_verified_hypotheses,
            "unresolved": t.unresolved_hypotheses,
            "material_frontier": t.material_frontier_ids,
            "decision_basis": t.decision_basis,
        }

    left, right = baseline.resolution_trace, experiment.resolution_trace

    def leader_witnesses(t: ResolutionTrace) -> list[dict[str, Any]]:
        return [
            s.model_dump(mode="json")
            for a in t.hypothesis_audits
            if a.hypothesis_id in t.mechanism_verified_hypotheses
            for s in a.root_support
        ]

    return {
        "case": name,
        "assessment_kind": "FROZEN_DEVELOPMENT_FIXTURE_COUNTERFACTUAL",
        "production": brief(baseline),
        "guarded_experiment": brief(experiment),
        "accounting": [asdict(row) for row in accounting],
        "invariants": {
            "root_unchanged": baseline.root_cause == experiment.root_cause,
            "supported_unchanged": left.plausible_hypotheses == right.plausible_hypotheses,
            "strong_unchanged": left.mechanism_verified_hypotheses
            == right.mechanism_verified_hypotheses,
            "leader_witnesses_unchanged": leader_witnesses(left) == leader_witnesses(right),
            "frontier_unchanged": left.frontier_answers == right.frontier_answers,
            "hypothesis_inventory_unchanged": case.hypotheses == original_case,
        },
        "production_rule_registered": False,
    }


CASES = (
    "missing_current_operations",
    "current_refusal_complete",
    "historical_manifestation",
    "failure_already_in_baseline",
    "competing_supported_change",
    "unbound_request",
    "timeout_out_of_scope",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a fresh output file")
    rows = [evaluate(name) for name in CASES]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {"assessment_kind": "FROZEN_DEVELOPMENT_FIXTURE_COUNTERFACTUAL", "cases": rows},
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            [
                {k: row[k] for k in ("case", "production", "guarded_experiment", "invariants")}
                for row in rows
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
