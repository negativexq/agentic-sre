"""M19-3.20: compact cross-boundary capture, manifest, and provider-tape proofs."""

from __future__ import annotations

import copy
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from test_evidence_manifest import WARNING, _request, _world
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers
from test_trajectory_replay import ORDER_POD, ORDER_RS

import apps.control_plane.diagnosis as diagnosis_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import LiveSource
from packages.rca.provider_adapter import ProviderAdapter
from packages.storage.models import (
    EventVersionRow,
    IncidentEventRow,
    InvestigationReadRow,
    LogObservationRow,
    RunEvidenceManifestRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import DiagnosisRepository


def test_live_capture_manifest_engine_calls_are_ordered_and_taped(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, cluster, clock, incident_id = setup
    cluster.objects.extend((copy.deepcopy(ORDER_RS), copy.deepcopy(ORDER_POD)))
    cluster.events = [copy.deepcopy(WARNING)]
    with factory() as session:
        from packages.storage.models import AlertRow

        alert = session.scalars(select(AlertRow)).one()
        alert.labels = {"pod": ORDER_POD["metadata"]["name"]}
        session.commit()

    readers = Readers()
    provider_calls: list[tuple[str, str, str]] = []
    original_provider_call = ProviderAdapter._provider_call

    def counted_provider_call(adapter: ProviderAdapter, *, capability: str, **kwargs: Any) -> Any:
        provider_calls.append((adapter.run_id, adapter.caller_class, capability))
        return original_provider_call(adapter, capability=capability, **kwargs)

    monkeypatch.setattr(ProviderAdapter, "_provider_call", counted_provider_call)

    manifest_seen: dict[str, int] = {}
    original_build_manifest = diagnosis_module.__dict__["build_manifest"]

    def observed_manifest(*args: Any, **kwargs: Any) -> Any:
        request = args[1]
        with factory() as observer:  # independent transaction sees capture commits
            manifest_seen["cycles"] = (
                observer.scalar(select(func.count()).select_from(SnapshotCycleRow)) or 0
            )
            manifest_seen["events"] = (
                observer.scalar(select(func.count()).select_from(EventVersionRow)) or 0
            )
            manifest_seen["logs"] = (
                observer.scalar(select(func.count()).select_from(LogObservationRow)) or 0
            )
            manifest_seen["capture_reads"] = (
                observer.scalar(
                    select(func.count())
                    .select_from(InvestigationReadRow)
                    .where(
                        InvestigationReadRow.run_id == request.run_id,
                        InvestigationReadRow.caller_class == "CAPTURE",
                    )
                )
                or 0
            )
            manifest_seen["manifest_before"] = (
                observer.scalar(
                    select(func.count())
                    .select_from(RunEvidenceManifestRow)
                    .where(RunEvidenceManifestRow.run_id == request.run_id)
                )
                or 0
            )
            committed_event_ids = {
                str(value) for value in observer.scalars(select(EventVersionRow.version_id)).all()
            }
            committed_log_ids = {
                str(value)
                for value in observer.scalars(select(LogObservationRow.observation_id)).all()
            }
        assert manifest_seen["cycles"] == 1
        assert manifest_seen["events"] >= 1
        assert manifest_seen["logs"] >= 1
        assert manifest_seen["capture_reads"] >= 1
        assert manifest_seen["manifest_before"] == 0
        entries = original_build_manifest(*args, **kwargs)
        event_members = {
            entry.source_id for entry in entries if entry.source_type == "EVENT_VERSION"
        }
        log_members = {entry.source_id for entry in entries if entry.source_type == "LOG"}
        assert event_members and event_members <= committed_event_ids
        assert log_members and log_members <= committed_log_ids
        return entries

    monkeypatch.setattr(diagnosis_module, "build_manifest", observed_manifest)

    engine_entry: dict[str, Any] = {}
    original_pressure = LiveSource.resource_pressure

    def observe_engine_start(source: LiveSource, *args: Any, **kwargs: Any) -> Any:
        run_id = source.provider_adapter.run_id  # type: ignore[union-attr]
        with factory() as observer:  # manifest and boundary must precede RCA
            engine_entry["manifest_rows"] = (
                observer.scalar(
                    select(func.count())
                    .select_from(RunEvidenceManifestRow)
                    .where(RunEvidenceManifestRow.run_id == run_id)
                )
                or 0
            )
            engine_entry["boundary_rows"] = (
                observer.scalar(
                    select(func.count())
                    .select_from(IncidentEventRow)
                    .where(
                        IncidentEventRow.event_type == "EVIDENCE_GATHERED",
                        IncidentEventRow.payload["run_id"].as_string() == run_id,
                    )
                )
                or 0
            )
            engine_entry["capture_rows"] = (
                observer.scalar(
                    select(func.count())
                    .select_from(InvestigationReadRow)
                    .where(
                        InvestigationReadRow.run_id == run_id,
                        InvestigationReadRow.caller_class == "CAPTURE",
                    )
                )
                or 0
            )
        assert engine_entry["manifest_rows"] >= 1
        assert engine_entry["boundary_rows"] == 1
        assert engine_entry["capture_rows"] >= 1
        return original_pressure(source, *args, **kwargs)

    monkeypatch.setattr(LiveSource, "resource_pressure", observe_engine_start)

    clock.now = T0 + timedelta(minutes=30)
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=readers.configured(),
        clock=clock,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        tape = list(
            session.scalars(
                select(InvestigationReadRow)
                .where(InvestigationReadRow.run_id == run_id)
                .order_by(InvestigationReadRow.sequence)
            )
        )
        logs = list(
            session.scalars(
                select(LogObservationRow).where(
                    LogObservationRow.evidence_id.like("loki:payment-service:%")
                )
            )
        )
        diagnosis_document = DiagnosisRepository(session).latest(incident_id)

    assert engine_entry and manifest_seen
    assert diagnosis_document is not None
    finding_ids = {
        evidence_id
        for finding in diagnosis_document.get("evidence", [])
        for evidence_id in finding.get("evidence_ids", [])
    }
    all_product_evidence_ids = finding_ids | {
        evidence_id for row in tape for evidence_id in row.evidence_ids
    }
    assert all(
        not evidence_id.startswith(("cluster:current", "cluster:missing"))
        for evidence_id in all_product_evidence_ids
    )
    assert readers.loki.calls >= 1
    assert readers.prometheus.calls >= 1
    assert (
        len(provider_calls) == readers.loki.calls + readers.prometheus.calls + readers.tempo.calls
    )
    assert len(provider_calls) == len(tape)  # one completed tape row per raw provider call
    calls_by_capability = Counter(capability for _, _, capability in provider_calls)
    assert calls_by_capability["loki_logs"] == readers.loki.calls
    assert calls_by_capability["resource_pressure"] + calls_by_capability["traffic"] == (
        readers.prometheus.calls
    )
    assert calls_by_capability["runtime_traces"] == readers.tempo.calls
    assert Counter((row.caller_class, row.capability) for row in tape) == Counter(
        (caller, capability) for _, caller, capability in provider_calls
    )
    assert [row.sequence for row in tape] == list(range(1, len(tape) + 1))
    capture_sequences = [row.sequence for row in tape if row.caller_class == "CAPTURE"]
    engine_sequences = [row.sequence for row in tape if row.caller_class == "ENGINE"]
    assert capture_sequences and engine_sequences
    assert max(capture_sequences) < min(engine_sequences)
    assert all(row.run_id == run_id for row in tape)
    assert all(call_run == run_id for call_run, _, _ in provider_calls)
    assert {(caller, capability) for _, caller, capability in provider_calls} <= {
        (row.caller_class, row.capability) for row in tape
    }
    assert any(
        row.caller_class == "ENGINE" and row.capability == "resource_pressure" for row in tape
    )
    assert any(row.caller_class == "CAPTURE" and row.capability == "loki_logs" for row in tape)
    capture_read_ids = {row.read_id for row in tape if row.caller_class == "CAPTURE"}
    assert logs
    assert all(log.source_read_id in capture_read_ids for log in logs)


@pytest.mark.postgres
def test_uncommitted_event_version_is_invisible_to_manifest_on_postgres(
    postgres_url: str,
) -> None:
    with _world(postgres_url) as (factory, incident):
        writer = factory()
        uncommitted = EventVersionRow(
            namespace="sre-demo",
            involved_kind="Pod",
            involved_name="p",
            dedup_key="uncommitted-manifest-proof",
            event_at=T0 + timedelta(minutes=2),
            observed_at=T0 + timedelta(minutes=2),
            body={"metadata": {"uid": "uncommitted"}, "reason": "BackOff"},
        )
        writer.add(uncommitted)
        writer.flush()
        try:
            entries = diagnosis_module.__dict__["build_manifest"](
                factory, _request(incident, "uncommitted-proof-run"), timestamp=datetime.now(UTC)
            )
            event_members = {
                entry.source_id for entry in entries if entry.source_type == "EVENT_VERSION"
            }
            with factory() as observer:
                committed_first = observer.scalar(
                    select(EventVersionRow.version_id).where(EventVersionRow.dedup_key == "first")
                )
            assert committed_first is not None
            assert str(committed_first) in event_members
            assert str(uncommitted.version_id) not in event_members
        finally:
            writer.rollback()
            writer.close()
