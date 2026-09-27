"""M20.4: traffic evidence on the FULL_SOURCE product investigation path, end to end.

The live base path does not capture traffic (``LiveSource.traffic_observations()`` is
empty by contract). A FULL_SOURCE product investigation starts from the complete
persisted base evidence and may acquire traffic through the authorized
provider-backed investigation path: a natural METRIC_CHANGE gap yields a bounded
Prometheus traffic read, taped as INVESTIGATION; its records join the evidence
overlay and the case is rebuilt. A flat series stays neutral; a series that crosses
the traffic rule yields a TRAFFIC_INCREASE Finding. Traffic may support; it never
carries resolution, contradiction or elimination authority by itself.

No policy is scripted and no candidate is forced. The fake reader spaces its points
with the production reader's step function, so the <=32 point bound is the reader's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_product_log_evidence import ServiceLoki, _latest, _payment_service_change, _service
from test_replay_provider import FakePrometheus, Readers

from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.prometheus import _traffic_query_step_seconds
from packages.rca.investigation.state import SEED_FULL_SOURCE
from packages.rca.model import (
    GapDimension,
    GapOutcomeKind,
    InvestigationPolicyKind,
    InvestigationResult,
    Resolution,
    RuntimeObservationState,
    TrafficObservation,
)
from packages.rca.replay import replay_run
from packages.storage.models import InvestigationReadRow, InvestigationRunRow

ONSET = T0 + timedelta(minutes=11)


class SeriesPrometheus(FakePrometheus):
    """Request rate 2/s, multiplied by ``factor`` from the incident onset."""

    def __init__(self, factor: float) -> None:
        super().__init__()
        self.factor = factor

    def query_traffic(self, target: Any, query: Any) -> tuple[TrafficObservation, ...]:
        step = timedelta(seconds=_traffic_query_step_seconds(query.start, query.end, query.limit))
        points: list[TrafficObservation] = []
        at = query.start
        while at <= query.end:
            points.append(
                TrafficObservation(
                    entity=target,
                    metric="http_requests_per_second",
                    at=at,
                    value=2.0 * (self.factor if at >= ONSET else 1.0),
                    evidence_id=f"prom:traffic:{target.name}:{at.isoformat()}",
                )
            )
            at += step
        return tuple(points[: query.limit])


def _findings(document: Any, kind: str) -> Iterator[dict[str, Any]]:
    if isinstance(document, dict):
        if document.get("kind") == kind and "evidence_ids" in document:
            yield document
        for value in document.values():
            yield from _findings(value, kind)
    elif isinstance(document, list):
        for value in document:
            yield from _findings(value, kind)


def _run(world: Any, factor: float) -> dict[str, Any]:
    factory, _, _, incident_id = world
    readers = Readers()
    readers.loki = ServiceLoki()  # type: ignore[assignment]
    readers.prometheus = SeriesPrometheus(factor)
    _payment_service_change(world, readers)
    _service(world, readers).run(incident_id, "MANUAL")
    base_run, base = _latest(factory, incident_id)
    _service(world, readers, bounded_policy_factory=DeterministicIntentPolicy).run(
        incident_id, "MANUAL"
    )
    run_id, investigated = _latest(factory, incident_id)
    with factory() as session:
        trajectory = session.get(InvestigationRunRow, run_id)
        assert trajectory is not None
        result = InvestigationResult.model_validate(trajectory.document)
        reads = [
            tuple(row.evidence_ids)
            for row in session.scalars(
                select(InvestigationReadRow)
                .where(InvestigationReadRow.run_id == run_id)
                .order_by(InvestigationReadRow.sequence)
            )
            if (row.caller_class, row.capability) == ("INVESTIGATION", "traffic")
        ]

    # The product path: deterministic intent selector on the full source, nothing scripted.
    contract = result.replay_contract
    assert contract is not None
    assert contract.policy_kind is InvestigationPolicyKind.INTENT_SELECTOR
    assert contract.config["seed_mode"] == SEED_FULL_SOURCE
    assert result.model_calls == 0

    # A natural METRIC_CHANGE gap yields one bounded traffic read, taped as INVESTIGATION.
    (audit,) = [item for item in result.action_audits if item.action.capability == "traffic"]
    assert audit.gap_dimension is GapDimension.METRIC_CHANGE
    assert audit.authorization_result == "AUTHORIZED"
    assert audit.action.target is not None
    assert audit.action.target.canonical == "sre-demo/Service/payment-service"
    assert audit.backend_execution_status.value == "SUCCEEDED"
    (tape_ids,) = reads
    assert 0 < len(tape_ids) <= 32
    # The read's evidence ids are acquired through the overlay.
    assert set(audit.new_evidence_refs) == set(tape_ids)
    (observation,) = [item for item in result.observations if item.capability == "traffic"]
    assert observation.runtime is not None

    # Traffic never carries resolution or elimination authority here.
    prom = {evidence_id for evidence_id in tape_ids}
    assert investigated.document["resolution"] != Resolution.RESOLVED.value
    for elimination in investigated.document.get("resolution_trace", {}).get("eliminations", []):
        assert not prom & set(elimination.get("evidence_ids", ()))

    # The investigated revision and the base revision replay offline to their digests.
    assert replay_run(base_run, "base", session_factory=factory) == base.epistemic_digest
    for mode in ("trajectory", "selector"):
        assert replay_run(run_id, mode, session_factory=factory) == investigated.epistemic_digest
    return {
        "audit": audit,
        "observation": observation,
        "tape_ids": set(tape_ids),
        "base": base,
        "investigated": investigated,
    }


def test_a_flat_traffic_read_is_preserved_as_neutral_evidence(setup: Any) -> None:  # noqa: F811
    run = _run(setup, factor=1.0)
    assert run["observation"].runtime.state is RuntimeObservationState.OBSERVED_NORMAL
    assert run["audit"].observation_outcome is GapOutcomeKind.UNKNOWN
    investigated, base = run["investigated"], run["base"]
    assert list(_findings(investigated.document, "TRAFFIC_INCREASE")) == []
    # Nothing decision-relevant was acquired: the decision is the base one.
    assert investigated.epistemic_digest == base.epistemic_digest


def test_a_traffic_spike_is_typed_from_the_read_without_resolution_authority(
    setup: Any,  # noqa: F811
) -> None:
    run = _run(setup, factor=2.5)
    assert run["observation"].runtime.state is RuntimeObservationState.OBSERVED_ABNORMAL
    assert run["audit"].observation_outcome is GapOutcomeKind.SUPPORTS
    investigated, base = run["investigated"], run["base"]
    findings = list(_findings(investigated.document, "TRAFFIC_INCREASE"))
    assert findings
    cited = {evidence_id for finding in findings for evidence_id in finding["evidence_ids"]}
    # Only that read's persistent evidence ids, acquired through the overlay.
    assert cited <= run["tape_ids"]
    assert cited <= set(run["audit"].new_evidence_refs)
    assert all(
        finding["entity"] == {"kind": "Service", "name": "payment-service", "namespace": "sre-demo"}
        for finding in findings
    )
    # New decision-relevant evidence may change the digest, never the resolution authority.
    assert investigated.epistemic_digest != base.epistemic_digest
    assert json.dumps(base.document).count("TRAFFIC_INCREASE") == 0
    # Fixture regression only (not a product rule): the decision itself is unchanged here.
    assert investigated.document["resolution"] == base.document["resolution"]
    assert investigated.document["root_cause"] == base.document["root_cause"]


@pytest.mark.parametrize("factor", [1.0, 2.5])
def test_the_traffic_read_is_bounded_by_the_reader_contract(setup: Any, factor: float) -> None:  # noqa: F811
    assert len(_run(setup, factor)["tape_ids"]) <= 32
