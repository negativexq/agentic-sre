"""Budget accounting preserves the independent denominator and evidence boundaries."""

from copy import deepcopy
from typing import Any

import pytest
from scripts.measure_frozen_binding_budget import measure
from scripts.rollout_resolution_dev_proof import source

from packages.evals.frozen_request_binding import FrozenLog, FrozenRequestEvidence, FrozenSpan


def inputs() -> tuple[FrozenRequestEvidence, list[dict[str, Any]]]:
    value = source()
    assert value.cutoff is not None and value.alert_coverage_start is not None
    provenance: dict[str, Any] = {
        "incident_id": "dev",
        "run_id": "dev",
        "captured_at": value.cutoff,
    }
    evidence = FrozenRequestEvidence(
        incident_id="dev",
        run_id="dev",
        episode_start=value.alert_coverage_start,
        episode_end=value.cutoff,
        frozen_cutoff=value.cutoff,
        caller_service="order-service",
        configured_peer="payment-service",
        logs=tuple(
            FrozenLog(**provenance, source_reference=f"log:{r.evidence_id}", record=r)
            for r in value.error_items
        ),
        spans=tuple(
            FrozenSpan(**provenance, source_reference=f"span:{r.evidence_id}", record=r)
            for r in value.trace_items
        ),
    )
    ledger = [
        {"request_id": r.attributes["request_id"], "status": 500, "scenario": "refusal"}
        for r in value.error_items
    ]
    return evidence, ledger


def test_caps_keep_driver_denominator_and_do_not_mutate_inputs() -> None:
    evidence, ledger = inputs()
    before = deepcopy((evidence, ledger))
    report = measure(evidence, ledger)
    full, oldest = report["profiles"][0:2]
    assert full["bound_failed_requests"] == full["driver_failed_requests"] == 4
    assert full["all_driver_refusal_requests_bound"] is True
    assert oldest["selected_spans"] == 8
    assert oldest["bound_failed_requests"] == 0  # baseline precedes the failed operations
    assert oldest["driver_failed_requests"] == 4
    assert oldest["all_driver_refusal_requests_bound"] is False
    assert report["rollout_guard_evaluated"] is report["rca_evaluated"] is False
    assert (evidence, ledger) == before


def test_missing_parent_cannot_be_repaired_from_driver_ledger() -> None:
    evidence, ledger = inputs()
    parents = {log.record.attributes["parent_span_id"] for log in evidence.logs}
    evidence = evidence.model_copy(
        update={"spans": tuple(s for s in evidence.spans if s.record.span_id not in parents)}
    )
    report = measure(evidence, ledger)
    assert report["profiles"][0]["bound_failed_requests"] == 0


def test_duplicate_driver_requests_or_log_ids_rejected() -> None:
    evidence, ledger = inputs()
    with pytest.raises(ValueError, match="request IDs"):
        measure(evidence, [*ledger, ledger[0]])
    with pytest.raises(ValueError, match="evidence IDs"):
        measure(evidence.model_copy(update={"logs": (*evidence.logs, evidence.logs[0])}), ledger)
