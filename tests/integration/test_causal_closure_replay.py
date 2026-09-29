"""Real normalizers and active selector, with offline serialized-source replay."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from packages.evals.causal_recording import ReadTape, RecordedSeed
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.environment import SourceInvestigationBackend
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import SEED_FULL_SOURCE, InvestigationConfig
from packages.rca.model import Alert, ClusterEvent, EntityRef, ObjectVersion, Resolution
from packages.rca.source import InMemorySource, ObservationSource


def source(rejection: bool) -> InMemorySource:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    quota = EntityRef(kind="ResourceQuota", name="capacity", namespace="shop")
    workload = EntityRef(kind="StatefulSet", name="worker", namespace="shop")
    versions = [
        ObjectVersion(
            entity=entity,
            observed_at=at - timedelta(minutes=5),
            evidence_id="obj:" + entity.name,
            body=cast(dict[str, Any], body),
        )
        for entity, body in [
            (quota, {"status": {"hard": {"pods": "1"}, "used": {"pods": "1"}}}),
            (
                workload,
                {
                    "metadata": {"name": "worker", "namespace": "shop"},
                    "spec": {
                        "selector": {"matchLabels": {"app": "worker"}},
                        "template": {
                            "metadata": {"labels": {"app": "worker"}},
                            "spec": {"containers": [{"name": "app", "image": "worker:1"}]},
                        },
                    },
                },
            ),
        ]
    ]
    event = ClusterEvent(
        entity=workload,
        first_at=at,
        last_at=at,
        reason="FailedCreate",
        type="Warning",
        message="pods forbidden: exceeded quota: capacity",
        evidence_id="evt:quota",
    )
    full = InMemorySource(
        name="closure",
        versions=versions,
        event_items=[event] if rejection else [],
        alert_items=[
            Alert(name="ReplicasUnavailable", service="worker", namespace="shop", starts_at=at)
        ],
        cutoff=at + timedelta(minutes=5),
        alert_coverage_start=at - timedelta(minutes=10),
    )

    class Seed(InMemorySource):
        def investigation_backend(self) -> SourceInvestigationBackend:
            return SourceInvestigationBackend(full)

    # The rejection is initially unavailable and can enter only through a query.
    return Seed(**{**vars(full), "event_items": []})


def test_new_recorded_rejection_changes_decision_and_replays(tmp_path: Path) -> None:
    config = InvestigationConfig(seed_mode=SEED_FULL_SOURCE, max_wall_time_seconds=600)
    tape = ReadTape(source(True))
    result = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(tape)),
        policy=DeterministicIntentPolicy(),
        config=config,
    )
    assert result.initial_resolution is Resolution.INSUFFICIENT_EVIDENCE
    assert result.final_resolution is Resolution.RESOLVED
    assert result.diagnosis.incident_recovery == "NOT_ASSESSED"
    assert result.diagnosis.resolution_trace is not None
    assert result.diagnosis.resolution_trace.diagnosis_status == "MECHANISM_VERIFIED_CAUSE"
    assert any(
        a.state == "ANSWERED_ROLE_TRANSFERRED"
        for a in result.diagnosis.resolution_trace.frontier_answers
    )
    assert result.tool_calls >= 1
    assert result.action_audits[0].decision_state_changed
    assert (
        result.action_audits[0].causal_decision_before
        != result.action_audits[0].causal_decision_after
    )
    path = tmp_path / "reads.json"
    tape.save(path)
    # New objects, no source/provider attached, and every recorded read consumed.
    recorded = ReadTape.load(path)
    assert recorded.source is None and recorded.backend is None
    assert result.replay_contract is not None
    replay = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(recorded)),
        policy=DeterministicIntentPolicy(),
        config=config,
        recorded_terminal=result.replay_contract.terminal,
    )
    recorded.assert_consumed()
    assert diagnosis_epistemic_digest(replay.diagnosis) == diagnosis_epistemic_digest(
        result.diagnosis
    )
    assert [(a.causal_decision_before, a.causal_decision_after) for a in replay.action_audits] == [
        (a.causal_decision_before, a.causal_decision_after) for a in result.action_audits
    ]


def test_same_graph_empty_reads_never_authorize_resolution() -> None:
    tape = ReadTape(source(False))
    result = investigate_diagnosis(
        cast(ObservationSource, RecordedSeed(tape)),
        policy=DeterministicIntentPolicy(),
        config=InvestigationConfig(seed_mode=SEED_FULL_SOURCE, max_wall_time_seconds=600),
    )
    assert result.final_resolution is not Resolution.RESOLVED
    assert result.diagnosis.resolution_trace is not None
    assert not result.diagnosis.resolution_trace.mechanism_verified_hypotheses
    assert not result.diagnosis.resolution_trace.explanations
    physical = [
        (a.action.capability, a.action.target, a.action.query)
        for a in result.action_audits
        if a.action.action == "inspect"
    ]
    assert len(physical) == len(
        set(
            (c, t.canonical if t else None, q.model_dump_json() if q else None)
            for c, t, q in physical
        )
    )


def test_no_data_and_provider_errors_leave_material_frontier_open(monkeypatch: Any) -> None:
    for provider_error in (False, True):
        seed = source(False)
        at = seed.alert_items[0].starts_at
        seed.event_items = [
            ClusterEvent(
                entity=EntityRef(kind="StatefulSet", name="worker", namespace="shop"),
                first_at=at,
                last_at=at,
                type="Warning",
                reason="FailedCreate",
                message="admission failed, origin not recorded",
                evidence_id="evt:unattributed",
            )
        ]

        def read(
            *args: object, fail: bool = provider_error, **kwargs: object
        ) -> tuple[object, ...]:
            if fail:
                raise RuntimeError("recorded provider unavailable")
            return ()

        with monkeypatch.context() as patch:
            for name in dir(SourceInvestigationBackend):
                if name.startswith("query_"):
                    patch.setattr(SourceInvestigationBackend, name, read)
            result = investigate_diagnosis(
                seed,
                policy=DeterministicIntentPolicy(),
                config=InvestigationConfig(seed_mode=SEED_FULL_SOURCE, max_wall_time_seconds=600),
            )
        assert result.final_resolution is not Resolution.RESOLVED
        trace = result.diagnosis.resolution_trace
        assert trace is not None and trace.frontier_answers
        assert all(a.state == "OPEN" for a in trace.frontier_answers)
        assert not trace.explanations and not trace.mechanism_verified_hypotheses
        if provider_error:
            assert any(a.investigation_state == "BLOCKED_ACCESS" for a in trace.frontier_answers)
        else:
            assert all(
                a.investigation_state
                in {"INVESTIGATED_INCONCLUSIVE", "BLOCKED_BUDGET", "UNEXPLORED"}
                for a in trace.frontier_answers
            )


def test_bare_memory_quota_declaration_creates_question_without_support() -> None:
    from packages.rca.engine import build_case, diagnose_case

    seed = source(False)
    quota = seed.versions[0]
    seed.versions[0] = quota.model_copy(
        update={
            "body": {
                "spec": {"hard": {"memory": "1Gi"}},
            }
        }
    )
    case = build_case(seed)
    assert any(a.role == "quota" and a.actor == quota.entity for a in case.structural_alternatives)
    diagnosis = diagnose_case(case)
    assert diagnosis.resolution is not Resolution.RESOLVED
    assert (
        not diagnosis.resolution_trace
        or not diagnosis.resolution_trace.mechanism_verified_hypotheses
    )
