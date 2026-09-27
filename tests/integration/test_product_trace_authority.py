"""M20.5 closure: trace evidence reaches m16 authority on the product path, end to end.

This is a natural *product-path* Tempo E2E, not a live-cluster proof: the real
``DiagnosisService`` with its persistent tape, the FULL_SOURCE seed and the
deterministic intent selector read a protocol-faithful Tempo through the real
``TempoTraceReader`` (only its HTTP opener is replaced). The demo workloads do
not emit ``k8s.pod.uid`` today, so no Kind run can show positive Pod authority.

The chain proven: a natural dependency-health gap on the payment Pod yields a
``runtime_traces`` candidate → one INVESTIGATION Tempo read on the tape → the
parsed spans (``k8s.pod.uid`` preserved) join the evidence overlay → UID-aware
runtime propagation → Pod authority only for the exact instance (span UID ==
journal UID == the actor's Finding UID) → the existing m16 root-eligibility
consequence → offline replay of the same digest.

Completeness only limits absence claims: observed errors under BEST_EFFORT are
positive evidence; success-only spans under any reachable completeness stay
UNKNOWN. No policy is scripted and no candidate is forced.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import Message
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_product_log_evidence import ServiceLoki, _latest, _payment_service_change, _service
from test_replay_provider import Readers

import packages.rca.engine as engine_module
from packages.rca.causal_roles import (
    HypothesisCausalRole,
    HypothesisCausalRoles,
    derive_hypothesis_causal_roles,
)
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import SEED_FULL_SOURCE
from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
from packages.rca.live import ListingScope, ObjectListing
from packages.rca.model import (
    GapDimension,
    GapOutcomeKind,
    Hypothesis,
    InvestigationActionAudit,
    InvestigationObservation,
    InvestigationPolicyKind,
    InvestigationResult,
    RuntimeObservationState,
)
from packages.rca.replay import ReplayModeUnsupported, replay_run
from packages.rca.runtime_propagation import (
    RuntimeBindingVerificationState,
    RuntimePropagation,
    RuntimePropagationEdge,
)
from packages.storage.models import DiagnosisRow, InvestigationReadRow, InvestigationRunRow

POD_NAME = "payment-service-7d9f-x2x4q"
POD = {"kind": "Pod", "name": POD_NAME, "namespace": "sre-demo"}
POD_UID = "pod-uid-1"
OTHER_UID = "pod-uid-2"
ONSET = T0 + timedelta(minutes=11)
SPAN_AT = ONSET + timedelta(seconds=30)
PROPAGATED_EFFECT = "ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT"
# The roles through which m16 root eligibility excludes an actor (resolution.py).
M16_ROLES = {HypothesisCausalRole.PROPAGATED_EFFECT, HypothesisCausalRole.MANIFESTATION}
VERIFIED = RuntimeBindingVerificationState.VERIFIED
UNRESOLVED = RuntimeBindingVerificationState.UNRESOLVED
CONTRADICTED = RuntimeBindingVerificationState.CONTRADICTED


def _attributes(values: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def _nanos(at: datetime) -> str:
    return str(int(at.timestamp()) * 1_000_000_000)


class _Response(io.BytesIO):
    status = 200

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@dataclass
class ProtocolTempo:
    """Tempo's HTTP API: ``/api/search``, then ``/api/v2/traces/{id}`` in OTLP JSON.

    Each trace is payment-service (CLIENT, the payment Pod) → bank-gateway
    (SERVER). ``error`` marks both spans ERROR; otherwise both are OK.
    """

    client_uid: str | None = POD_UID
    error: bool = True
    traces: int = 1
    completed_jobs: int = 1
    total_jobs: int = 1
    missing: frozenset[int] = frozenset()
    requests: list[str] = field(default_factory=list)

    def _trace(self, number: int) -> dict[str, Any]:
        trace_id = f"{number:032x}"
        client_span, server_span = f"c{number:015x}", f"5{number:015x}"
        payment = {
            "service.name": "payment-service",
            "k8s.namespace.name": "sre-demo",
            "k8s.deployment.name": "payment-service",
            "k8s.pod.name": POD_NAME,
        }
        if self.client_uid is not None:
            payment["k8s.pod.uid"] = self.client_uid
        bank = {
            "service.name": "bank-gateway",
            "k8s.namespace.name": "sre-demo",
            "k8s.deployment.name": "bank-gateway",
        }
        common = {
            "traceId": trace_id,
            "startTimeUnixNano": _nanos(SPAN_AT),
            "endTimeUnixNano": _nanos(SPAN_AT + timedelta(seconds=1)),
            "status": {"code": 2 if self.error else 1},
            "attributes": _attributes({"rpc.system": "grpc"}),
        }
        client = {**common, "spanId": client_span, "name": "POST /charge", "kind": 3}
        server = {
            **common,
            "spanId": server_span,
            "parentSpanId": client_span,
            "name": "Charge",
            "kind": 2,
        }
        return {
            "trace": {
                "resourceSpans": [
                    {
                        "resource": {"attributes": _attributes(attributes)},
                        "scopeSpans": [{"spans": [span]}],
                    }
                    for attributes, span in ((payment, client), (bank, server))
                ]
            }
        }

    def ids(self) -> set[str]:
        """The evidence ids of every span the reader can fetch."""
        return {
            f"tempo:{number:032x}:{prefix}{number:015x}"
            for number in range(1, self.traces + 1)
            if number not in self.missing
            for prefix in ("c", "5")
        }

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request.full_url)
        path = urlsplit(request.full_url).path
        if path == "/api/search":
            body: dict[str, Any] = {
                "traces": [{"traceID": f"{n:032x}"} for n in range(1, self.traces + 1)],
                "metrics": {"completedJobs": self.completed_jobs, "totalJobs": self.total_jobs},
            }
        else:
            number = int(path.rsplit("/", 1)[1], 16)
            if number in self.missing:
                raise HTTPError(request.full_url, 404, "trace not found", Message(), None)
            body = self._trace(number)
        return _Response(json.dumps(body).encode())


@dataclass
class Run:
    factory: sessionmaker[Session]
    base_run: str
    base: DiagnosisRow
    run_id: str
    investigated: DiagnosisRow
    result: InvestigationResult
    tempo_reads: list[InvestigationReadRow]
    propagation: RuntimePropagation
    roles: HypothesisCausalRoles
    tempo: ProtocolTempo

    def pod_hypothesis_id(self) -> str:
        (entry,) = [
            entry
            for entry in self.investigated.document["hypothesis_inventory"]
            if entry["causal_actor"] == POD
        ]
        return str(entry["hypothesis_id"])

    def pod_exclusions(self, row: DiagnosisRow) -> list[dict[str, Any]]:
        """The m16 propagated-effect eliminations of the payment Pod in one revision."""
        trace = row.document.get("resolution_trace") or {}
        pod = self.pod_hypothesis_id()
        return [
            item
            for item in trace.get("eliminations", [])
            if item.get("code") == PROPAGATED_EFFECT and item.get("hypothesis_id") == pod
        ]

    def replays(self, mode: str) -> bool:
        digest = replay_run(self.run_id, mode, session_factory=self.factory)
        return bool(digest == self.investigated.epistemic_digest)


def _event(uid: str | None, index: int) -> dict[str, Any]:
    involved: dict[str, Any] = dict(POD)
    if uid is not None:
        involved["uid"] = uid
    return {
        "kind": "Event",
        "metadata": {
            "name": f"payment-backoff-{index}",
            "namespace": "sre-demo",
            "uid": f"e{index}",
        },
        "involvedObject": involved,
        "reason": "BackOff",
        "type": "Warning",
        "message": "back-off restarting failed container",
        "firstTimestamp": ONSET.isoformat(),
        "lastTimestamp": ONSET.isoformat(),
        "count": 3,
    }


def _run(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    tempo: ProtocolTempo,
    *,
    event_uids: tuple[str | None, ...] = (POD_UID,),
    pod_deleted_before_span: bool = False,
) -> Run:
    factory, cluster, clock, incident_id = world
    cluster.objects[4]["metadata"]["uid"] = POD_UID
    readers = Readers()
    readers.loki = ServiceLoki()  # type: ignore[assignment]
    readers.tempo = TempoTraceReader(  # type: ignore[assignment]
        TempoConfig("http://tempo.test:3200"), opener=tempo
    )
    _payment_service_change(world, readers)
    if pod_deleted_before_span:
        # A complete Pod listing without the instance: the journal tombstones that
        # exact UID before the spans were emitted.
        clock.now = ONSET + timedelta(seconds=10)
        del cluster.objects[4]
        listed = cluster.list_objects

        def with_pod_scope(namespaces: Any) -> ObjectListing:
            listing = listed(namespaces)
            complete = listing.completed_scopes | {ListingScope("sre-demo", "Pod")}
            return ObjectListing(listing.objects, frozenset(complete), listing.failed_scopes)

        cluster.list_objects = with_pod_scope
        _service(world, readers).snapshot()
    clock.now = T0 + timedelta(minutes=12)
    cluster.events = [_event(uid, index) for index, uid in enumerate(event_uids)]
    _service(world, readers).snapshot()
    clock.now = T0 + timedelta(minutes=13)

    _service(world, readers).run(incident_id, "MANUAL")
    base_run, base = _latest(factory, incident_id)

    # Record, never alter, what the engine derives on each case rebuild.
    recorded: list[tuple[RuntimePropagation, HypothesisCausalRoles]] = []
    derive = derive_hypothesis_causal_roles

    def recording(
        hypotheses: tuple[Hypothesis, ...], propagation: RuntimePropagation
    ) -> HypothesisCausalRoles:
        roles = derive(hypotheses, propagation)
        recorded.append((propagation, roles))
        return roles

    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "derive_hypothesis_causal_roles", recording)
        _service(world, readers, bounded_policy_factory=DeterministicIntentPolicy).run(
            incident_id, "MANUAL"
        )
    run_id, investigated = _latest(factory, incident_id)

    with factory() as session:
        trajectory = session.get(InvestigationRunRow, run_id)
        assert trajectory is not None
        result = InvestigationResult.model_validate(trajectory.document)
        reads = list(
            session.scalars(
                select(InvestigationReadRow)
                .where(InvestigationReadRow.run_id == run_id)
                .where(InvestigationReadRow.capability == "tempo_traces")
                .order_by(InvestigationReadRow.sequence)
            )
        )
        session.expunge_all()
    propagation, roles = recorded[-1]
    return Run(
        factory, base_run, base, run_id, investigated, result, reads, propagation, roles, tempo
    )


def _natural_trace_read(run: Run) -> InvestigationActionAudit:
    """Every case: one natural, taped and acquired INVESTIGATION Tempo read."""
    contract = run.result.replay_contract
    assert contract is not None
    assert contract.policy_kind is InvestigationPolicyKind.INTENT_SELECTOR
    assert contract.config["seed_mode"] == SEED_FULL_SOURCE
    assert run.result.model_calls == 0

    (audit,) = [
        item for item in run.result.action_audits if item.action.capability == "runtime_traces"
    ]
    assert audit.gap_dimension is GapDimension.DEPENDENCY_HEALTH
    assert audit.authorization_result == "AUTHORIZED"
    assert audit.action.target is not None
    assert audit.action.target.canonical == f"sre-demo/Pod/{POD_NAME}"

    # The real reader spoke Tempo's protocol: one TraceQL search, then trace fetches.
    search, *fetches = run.tempo.requests
    assert urlsplit(search).path == "/api/search" and POD_NAME in search
    assert fetches and all("/api/v2/traces/" in url for url in fetches)

    (read,) = run.tempo_reads
    assert read.caller_class == "INVESTIGATION"
    assert set(read.evidence_ids) == run.tempo.ids()
    assert set(audit.new_evidence_refs) == run.tempo.ids()
    raw = json.dumps(read.observation)
    assert all(evidence_id in raw for evidence_id in run.tempo.ids())
    return audit


def _trace_observation(run: Run) -> InvestigationObservation:
    (observation,) = [
        item for item in run.result.observations if item.capability == "runtime_traces"
    ]
    assert observation.runtime is not None
    return observation


def _runtime_state(run: Run) -> RuntimeObservationState:
    runtime = _trace_observation(run).runtime
    assert runtime is not None
    return runtime.state


def _edge(run: Run) -> RuntimePropagationEdge:
    (edge,) = run.propagation.edges
    assert (edge.source_service, edge.affected_service) == ("bank-gateway", "payment-service")
    return edge


def _pod_role(run: Run) -> Any:
    assessment = run.roles.for_hypothesis(run.pod_hypothesis_id())
    assert assessment is not None
    return assessment


# A — positive authority, under BEST_EFFORT.


def test_an_exact_uid_chain_carries_trace_evidence_to_m16_authority(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(setup, monkeypatch, ProtocolTempo())
    audit = _natural_trace_read(run)

    # BEST_EFFORT does not void observed errors: they are positive evidence.
    (read,) = run.tempo_reads
    assert read.observation["diagnostics"]["completeness"] == "BEST_EFFORT"
    assert _runtime_state(run) is RuntimeObservationState.OBSERVED_ABNORMAL
    assert audit.observation_outcome is GapOutcomeKind.SUPPORTS

    # The parser kept the exact instance that emitted the span.
    (client,) = [span for span in read.observation["spans"] if span["span_kind"] == "CLIENT"]
    assert client["semantic_attributes"]["k8s.pod.uid"] == POD_UID

    # span UID == journal UID: the affected Pod endpoint is VERIFIED at both levels.
    edge = _edge(run)
    assert edge.affected_binding is not None
    assert edge.affected_binding.pod_uid == POD_UID
    assert edge.affected_pod_verification is VERIFIED
    assert edge.affected_deployment_verification is VERIFIED
    tempo_ids = run.tempo.ids()
    assert tempo_ids <= set(edge.evidence_ids)

    # == the actor's own Finding UID: the Pod takes an m16 role.
    role = _pod_role(run)
    assert role.verified_incoming_edges >= 1
    assert role.role in M16_ROLES

    # The existing m16 consequence, reached by the Tempo evidence.
    (exclusion,) = run.pod_exclusions(run.investigated)
    assert tempo_ids <= set(edge.evidence_ids) <= set(exclusion["evidence_ids"])
    # Without the trace read the base revision has no such exclusion.
    assert run.pod_exclusions(run.base) == []

    # D — the replay contract of an investigated revision, and of a base one.
    assert run.replays("trajectory")
    assert run.replays("selector")
    with pytest.raises(ReplayModeUnsupported):
        replay_run(run.run_id, "base", session_factory=run.factory)
    assert replay_run(run.base_run, "base", session_factory=run.factory) == (
        run.base.epistemic_digest
    )


# B — identity fail-closed: the same abnormal traces with a broken UID chain.


@pytest.mark.parametrize(
    ("client_uid", "event_uids", "deleted", "pod_level"),
    [
        pytest.param(None, (POD_UID,), False, UNRESOLVED, id="span-uid-missing"),
        pytest.param(OTHER_UID, (POD_UID,), False, UNRESOLVED, id="span-uid-wrong"),
        pytest.param(POD_UID, (None,), False, VERIFIED, id="finding-uid-missing"),
        pytest.param(POD_UID, (OTHER_UID,), False, VERIFIED, id="finding-uid-mismatch"),
        pytest.param(POD_UID, (POD_UID, OTHER_UID), False, VERIFIED, id="finding-multi-uid"),
        pytest.param(POD_UID, (POD_UID,), True, CONTRADICTED, id="span-uid-deleted"),
    ],
)
def test_a_broken_uid_chain_gives_no_pod_authority(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    client_uid: str | None,
    event_uids: tuple[str | None, ...],
    deleted: bool,
    pod_level: RuntimeBindingVerificationState,
) -> None:
    run = _run(
        setup,
        monkeypatch,
        ProtocolTempo(client_uid=client_uid),
        event_uids=event_uids,
        pod_deleted_before_span=deleted,
    )
    _natural_trace_read(run)
    # The same abnormal trace semantics as the positive case.
    assert _runtime_state(run) is RuntimeObservationState.OBSERVED_ABNORMAL

    edge = _edge(run)
    assert edge.affected_pod_verification is pod_level
    # Deployment-level verification does not depend on the Pod instance.
    assert edge.affected_deployment_verification is VERIFIED

    role = _pod_role(run)
    assert role.verified_incoming_edges == 0
    assert role.role not in M16_ROLES
    assert run.pod_exclusions(run.investigated) == []
    assert run.replays("trajectory")
    assert run.replays("selector")


# C — negative coverage: success-only spans are never OBSERVED_NORMAL.


@pytest.mark.parametrize(
    ("tempo", "completeness", "search_limit", "missing"),
    [
        pytest.param(ProtocolTempo(error=False), "BEST_EFFORT", False, 0, id="best-effort"),
        pytest.param(
            ProtocolTempo(error=False, completed_jobs=1, total_jobs=2),
            "TRUNCATED",
            False,
            0,
            id="truncated-incomplete-jobs",
        ),
        pytest.param(
            ProtocolTempo(error=False, traces=8), "TRUNCATED", True, 0, id="truncated-search-limit"
        ),
        pytest.param(
            ProtocolTempo(error=False, traces=2, missing=frozenset({2})),
            "BEST_EFFORT",
            False,
            1,
            id="best-effort-missing-trace",
        ),
    ],
)
def test_success_only_traces_are_never_observed_normal(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tempo: ProtocolTempo,
    completeness: str,
    search_limit: bool,
    missing: int,
) -> None:
    run = _run(setup, monkeypatch, tempo)
    audit = _natural_trace_read(run)

    # The completeness diagnostics are persisted on the tape.
    (read,) = run.tempo_reads
    diagnostics = read.observation["diagnostics"]
    assert diagnostics["completeness"] == completeness
    assert diagnostics["search_limit_reached"] is search_limit
    assert len(diagnostics["missing_trace_ids"]) == missing

    assert _runtime_state(run) is RuntimeObservationState.UNKNOWN
    assert audit.observation_outcome is not GapOutcomeKind.SUPPORTS
    assert audit.normalized_finding_ids == ()
    # No absence-derived Finding or authority: nothing cites the success-only spans.
    persisted = json.dumps(run.investigated.document)
    assert all(evidence_id not in persisted for evidence_id in run.tempo.ids())
    assert run.propagation.edges == ()
    assert run.pod_exclusions(run.investigated) == []
    assert run.replays("trajectory")
    assert run.replays("selector")
