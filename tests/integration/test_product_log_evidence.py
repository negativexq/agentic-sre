"""M20.3: log evidence on the FULL_SOURCE product path, end to end.

Two different paths, with different purposes, are proven here:

* Base path: caller-side logs captured into the FULL_SOURCE evidence universe are
  deterministically typed by F1.1 into a ``DEPENDENCY_ERRORS`` Finding that cites
  the persisted Loki evidence ids.
* Investigation path: an authorized dependency-health / log-error gap yields a
  bounded ``logs`` candidate; the deterministic FULL_SOURCE investigation makes an
  INVESTIGATION Loki read, persists and references the returned evidence (the
  records join the evidence overlay and the case is rebuilt), and records a neutral
  UNKNOWN outcome. That read returns the dependency's own logs, so no Finding cites
  it and the resolution does not change — log text alone has no resolution
  authority. The investigation read does not produce the base path's Finding.

No policy is scripted and no candidate is forced.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, cover_alert_channel, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.engine import EngineConfig, build_case
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import SEED_FULL_SOURCE
from packages.rca.model import (
    EntityRef,
    FindingKind,
    GapDimension,
    GapOutcomeKind,
    InvestigationPolicyKind,
    InvestigationResult,
    LogRecord,
    Resolution,
)
from packages.rca.replay import ReplaySource, replay_run
from packages.storage.models import DiagnosisRow, InvestigationReadRow, InvestigationRunRow
from packages.storage.repositories import DiagnosisRepository

CALLER_ERROR = "dependency.request error: payment-service URLError: Connection refused"


class ServiceLoki:
    """Answers each Loki read per service: the caller logs a named dependency failure."""

    offline = False

    def error_logs(
        self, services: Any, starts_at: Any, ends_at: Any, *, limit: int | None = None
    ) -> list[LogRecord]:
        del ends_at, limit
        return [
            LogRecord(
                service=service,
                at=T0 + timedelta(minutes=11),
                severity="ERROR",
                message=CALLER_ERROR
                if service == "order-service"
                else f"{service}: listener closed",
                evidence_id=f"loki:{service}:{starts_at.isoformat()}:{index}",
            )
            for index, service in enumerate(sorted(services))
            # Like Loki, only a real service label matches; a canonical ref never does.
            if "/" not in service
        ]


def _service(world: Any, readers: Readers, **kwargs: Any) -> DiagnosisService:
    factory, cluster, clock, _ = world
    return DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=readers.configured(),
        **kwargs,
    )


def _payment_service_change(world: Any, readers: Readers) -> None:
    """The payment Service's port changes before onset (as in the live smoke chain)."""
    _, cluster, clock, _ = world
    _service(world, readers).snapshot()
    clock.now = T0 + timedelta(minutes=10)
    service = copy.deepcopy(cluster.objects[2])
    service["metadata"]["resourceVersion"] = "9"
    service["spec"]["ports"] = [{"port": 80, "targetPort": 9999}]
    cluster.objects[2] = service
    _service(world, readers).snapshot()
    clock.now = T0 + timedelta(minutes=13)
    cover_alert_channel(world[0], start=T0, until=clock.now)


def _latest(factory: sessionmaker[Session], incident_id: Any) -> tuple[str, DiagnosisRow]:
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        session.expunge(row)
    return run_id, row


def test_log_evidence_on_the_full_source_product_path(setup: Any) -> None:  # noqa: F811
    factory, _, _, incident_id = setup
    readers = Readers()
    readers.loki = ServiceLoki()  # type: ignore[assignment]
    _payment_service_change(setup, readers)

    _service(setup, readers).run(incident_id, "MANUAL")
    base_run, base = _latest(factory, incident_id)
    _service(setup, readers, bounded_policy_factory=DeterministicIntentPolicy).run(
        incident_id, "MANUAL"
    )
    run_id, investigated = _latest(factory, incident_id)

    with factory() as session:
        trajectory = session.get(InvestigationRunRow, run_id)
        assert trajectory is not None
        result = InvestigationResult.model_validate(trajectory.document)
        tape = session.scalars(
            select(InvestigationReadRow)
            .where(InvestigationReadRow.run_id == run_id)
            .order_by(InvestigationReadRow.sequence)
        ).all()
        loki_reads = [
            (row.evidence_ids, json.dumps(row.observation))
            for row in tape
            if (row.caller_class, row.capability) == ("INVESTIGATION", "loki_logs")
        ]

    # (1) The product path: deterministic intent selector on the full source, nothing scripted.
    contract = result.replay_contract
    assert contract is not None
    assert contract.policy_kind is InvestigationPolicyKind.INTENT_SELECTOR
    assert contract.config["seed_mode"] == SEED_FULL_SOURCE
    assert result.model_calls == 0

    # (2) A natural dependency-health / log-error gap yields a bounded logs candidate.
    logs = next(
        audit
        for audit in result.action_audits
        if audit.action.capability == "logs"
        and audit.action.target == EntityRef.parse("sre-demo/Service/payment-service")
        and audit.new_evidence_refs
    )
    assert logs.gap_dimension in {GapDimension.DEPENDENCY_HEALTH, GapDimension.LOG_ERROR_PATTERN}
    assert logs.authorization_result == "AUTHORIZED"
    assert logs.action.target is not None
    assert logs.action.target.canonical == "sre-demo/Service/payment-service"

    # (3) It is an INVESTIGATION Loki read on the tape.
    assert loki_reads
    tape_ids, raw = next(
        (ids, raw) for ids, raw in loki_reads if set(logs.new_evidence_refs) <= set(ids)
    )

    # (4) The returned evidence ids resolve to the raw records on the tape and were acquired.
    assert logs.new_evidence_refs
    assert set(logs.new_evidence_refs) <= set(tape_ids)
    assert all(evidence_id in raw for evidence_id in logs.new_evidence_refs)
    assert all(ref.startswith("loki:payment-service:") for ref in logs.new_evidence_refs)

    # (5) The dependency's own logs are no typed failure: neutral UNKNOWN, no Finding.
    assert logs.observation_outcome is GapOutcomeKind.UNKNOWN
    assert logs.normalized_finding_ids == ()
    # The acquired records enter the evidence overlay and the case is rebuilt; no Finding of
    # the investigated revision may cite them.
    persisted = json.dumps(investigated.document)
    assert all(ref not in persisted for ref in logs.new_evidence_refs)

    # (6) No new resolution authority: the investigated revision decides like the base one.
    assert investigated.document["resolution"] != Resolution.RESOLVED.value
    assert investigated.document["resolution"] == base.document["resolution"]
    assert investigated.document["root_cause"] == base.document["root_cause"]
    assert logs.decision_state_changed is False
    assert logs.hypothesis_states_before == logs.hypothesis_states_after

    # (7) The base path: the captured caller log is typed by F1.1 with its Loki provenance.
    case = build_case(ReplaySource.from_run(base_run, session_factory=factory), EngineConfig())
    dependency = [
        finding
        for hypothesis in case.hypotheses
        for finding in hypothesis.findings
        if finding.kind is FindingKind.DEPENDENCY_ERRORS
    ]
    assert dependency
    assert {finding.related for finding in dependency} == {
        (EntityRef.parse("sre-demo/Service/payment-service"),)
    }
    cited = {evidence_id for finding in dependency for evidence_id in finding.evidence_ids}
    assert cited and all(evidence_id.startswith("loki:order-service:") for evidence_id in cited)
    assert all(evidence_id in json.dumps(base.document) for evidence_id in cited)

    # Both revisions replay offline to their persisted digests.
    assert replay_run(base_run, "base", session_factory=factory) == base.epistemic_digest
    for mode in ("trajectory", "selector"):
        assert replay_run(run_id, mode, session_factory=factory) == investigated.epistemic_digest
