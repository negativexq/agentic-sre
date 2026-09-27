"""M20.1b: a normal (non-investigation) deterministic run replays offline to its own digest."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers
from test_trajectory_replay import TABLES, ProviderFirstPolicy, _prepare, go_offline

from apps.control_plane.diagnosis import DiagnosisService
from packages.evals.product import proof_inputs
from packages.rca.replay import (
    ReplayModeUnsupported,
    replay_base,
    replay_run,
)
from packages.storage.manifest import ReplayDataError
from packages.storage.models import DiagnosisRow, RunEvidenceManifestRow
from packages.storage.repositories import DiagnosisRepository


def _run(world: Any, readers: Readers, *, policy: Any = None) -> tuple[sessionmaker[Session], str]:
    factory, cluster, clock, incident_id = world
    clock.now = T0 + timedelta(minutes=30)
    kwargs: dict[str, Any] = {}
    if policy is not None:
        kwargs["bounded_policy_factory"] = policy
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=readers.configured(),
        **kwargs,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, run_id


def _persisted(factory: sessionmaker[Session], run_id: str) -> DiagnosisRow:
    with factory() as session:
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        session.expunge(row)
        return row


def _counts(factory: sessionmaker[Session]) -> tuple[int | None, ...]:
    with factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(table)) for table in TABLES)


@pytest.fixture
def normal(setup: Any) -> tuple[sessionmaker[Session], str, Readers]:  # noqa: F811
    _prepare(setup)
    readers = Readers()
    factory, run_id = _run(setup, readers)
    return factory, run_id, readers


def test_a_normal_run_replays_offline_to_its_persisted_digest(
    normal: tuple[sessionmaker[Session], str, Readers], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, run_id, readers = normal
    row = _persisted(factory, run_id)
    assert row.epistemic_digest and row.document["mode"] == "deterministic"
    before = _counts(factory)
    counts = go_offline(monkeypatch, readers)
    replayed = replay_base(run_id, session_factory=factory)
    assert replayed.digest == row.epistemic_digest
    assert replay_run(run_id, "base", session_factory=factory) == row.epistemic_digest
    assert _counts(factory) == before  # nothing written
    assert counts["source"] == 2  # one frozen source per replay; no live source


def test_a_run_with_an_investigation_trajectory_is_not_base_replayable(setup: Any) -> None:  # noqa: F811
    _prepare(setup)
    factory, run_id = _run(setup, Readers(), policy=ProviderFirstPolicy)
    with pytest.raises(ReplayModeUnsupported, match="trajectory"):
        replay_base(run_id, session_factory=factory)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param({"mode": "llm"}, "deterministic", id="non-deterministic"),
        pytest.param({"model_calls": 2}, "model call", id="model-assisted"),
    ],
)
def test_only_deterministic_runs_are_base_replayed(
    normal: tuple[sessionmaker[Session], str, Readers], change: dict[str, Any], message: str
) -> None:
    factory, run_id, _ = normal
    with factory() as session:
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        row.document = {**row.document, **change}
        session.commit()
    with pytest.raises(ReplayModeUnsupported, match=message):
        replay_base(run_id, session_factory=factory)


def test_another_engine_config_is_not_base_replayed(
    normal: tuple[sessionmaker[Session], str, Readers],
) -> None:
    factory, run_id, _ = normal
    with factory() as session:
        session.execute(
            update(DiagnosisRow).where(DiagnosisRow.run_id == run_id).values(config_digest="0" * 64)
        )
        session.commit()
    with pytest.raises(ReplayModeUnsupported, match="engine config"):
        replay_base(run_id, session_factory=factory)


def test_a_missing_manifest_member_fails_loudly(
    normal: tuple[sessionmaker[Session], str, Readers],
) -> None:
    factory, run_id, _ = normal
    with factory() as session:
        member = session.scalars(
            select(RunEvidenceManifestRow).where(
                RunEvidenceManifestRow.run_id == run_id,
                RunEvidenceManifestRow.source_type == "OBJECT_VERSION",
            )
        ).first()
        assert member is not None
        from packages.storage.models import ObjectVersionRow  # noqa: PLC0415

        session.execute(
            delete(ObjectVersionRow).where(ObjectVersionRow.version_id == int(member.source_id))
        )
        session.commit()
    with pytest.raises(ReplayDataError):
        replay_base(run_id, session_factory=factory)


def test_the_proof_collector_records_the_replay_status(
    normal: tuple[sessionmaker[Session], str, Readers], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, run_id, _ = normal
    diagnosis_id = _persisted(factory, run_id).diagnosis_id
    facts = proof_inputs.revision_facts(diagnosis_id, factory)
    assert (facts.replay_status, facts.replay_digest) == ("PASS", facts.persisted_digest)

    def unsupported(run: str, session_factory: Any) -> str:
        raise ReplayModeUnsupported("no")

    def broken(run: str, session_factory: Any) -> str:
        raise RuntimeError("bug")

    assert proof_inputs.revision_facts(diagnosis_id, factory, replay=unsupported).replay_status == (
        "UNSUPPORTED"
    )
    assert (
        proof_inputs.revision_facts(diagnosis_id, factory, replay=broken).replay_status == "ERROR"
    )
    assert (
        proof_inputs.revision_facts(
            diagnosis_id, factory, replay=lambda r, f: "x" * 64
        ).replay_status
        == "DIVERGED"
    )


def test_an_unconsumed_recorded_read_is_a_divergence(
    normal: tuple[sessionmaker[Session], str, Readers],
) -> None:
    from packages.rca.replay import ReplayUnconsumedReads  # noqa: PLC0415
    from packages.storage.models import InvestigationReadRow  # noqa: PLC0415

    factory, run_id, _ = normal
    with factory() as session:
        last = session.scalars(
            select(InvestigationReadRow)
            .where(InvestigationReadRow.run_id == run_id)
            .order_by(InvestigationReadRow.sequence.desc())
        ).first()
        assert last is not None and last.caller_class == "ENGINE"
        session.add(
            InvestigationReadRow(
                run_id=run_id,
                sequence=last.sequence + 1,
                caller_class="ENGINE",
                capability=last.capability,
                query_key=last.query_key + "-extra",
                query_descriptor=last.query_descriptor,
                started_at=last.started_at,
                finished_at=last.finished_at,
                committed_at=last.committed_at,
                status=last.status,
                observation=last.observation,
                evidence_ids=list(last.evidence_ids),
                error_type=None,
                error_message=None,
            )
        )
        session.commit()
    with pytest.raises(ReplayUnconsumedReads, match="never replayed"):
        replay_base(run_id, session_factory=factory)
