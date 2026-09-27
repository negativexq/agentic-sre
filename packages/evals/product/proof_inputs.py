"""Read-only collection of proof inputs before teardown (M19-7.P1).

The only proof-side module that touches storage. For each accepted revision it
reads the persisted diagnosis document and epistemic digest, builds ``U(Rn)``
from the product's own replay source (the records the engine could cite) plus
the run's committed tape, maps cited evidence to exact instance UIDs where the
record carries one, and replays the run offline with the product's replay to
compare digests. It writes nothing and calls no provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.evals.product.proof import ProofInput, RevisionFacts, receipts_of
from packages.evals.product.spec import Expectation
from packages.rca.episode_end import _SYNTHETIC_TOMBSTONES
from packages.rca.investigation.policy import ReplayTrajectoryDivergence
from packages.rca.model import Diagnosis
from packages.rca.replay import (
    ReplayDivergence,
    ReplayModeUnsupported,
    ReplaySource,
    ReplayUnconsumedReads,
    replay_run,
)
from packages.storage.models import DiagnosisRow, InvestigationRunRow
from packages.storage.repositories import InvestigationReadRepository

Replay = Callable[[str, sessionmaker[Session]], str]


def _replay(run_id: str, session_factory: sessionmaker[Session]) -> str:
    """Trajectory replay for a run with an investigation artifact, base replay otherwise."""
    with session_factory() as session:
        investigated = session.get(InvestigationRunRow, run_id) is not None
    mode = "trajectory" if investigated else "base"
    return replay_run(run_id, mode, session_factory=session_factory)


_DIVERGED = (ReplayDivergence, ReplayTrajectoryDivergence, ReplayUnconsumedReads)


def source_evidence(
    run_id: str, session_factory: sessionmaker[Session]
) -> tuple[set[str], dict[str, str], set[str]]:
    """(ids, id -> exact UID, synthetic ids) of the run's base evidence records."""
    source = ReplaySource.from_run(run_id, session_factory=session_factory)
    ids: set[str] = set()
    uids: dict[str, str] = {}
    synthetic: set[str] = set()
    for versions in source.object_history().values():
        for version in versions:
            ids.add(version.evidence_id)
            if version.uid:
                uids[version.evidence_id] = version.uid
            if version.evidence_id in _SYNTHETIC_TOMBSTONES:
                synthetic.add(version.evidence_id)
    for event in source.events():
        ids.add(event.evidence_id)
        if event.involved_uid:
            uids[event.evidence_id] = event.involved_uid
    for observation in source.pod_status_observations():
        ids.add(observation.evidence_id)
        if observation.uid:
            uids[observation.evidence_id] = observation.uid
    for log in source.error_logs():
        ids.add(log.evidence_id)
    for traffic in source.traffic_observations():
        ids.add(traffic.evidence_id)
    for span in source.trace_observations():
        ids.add(span.evidence_id)
    return ids, uids, synthetic


def tape_evidence(run_id: str, session_factory: sessionmaker[Session]) -> set[str]:
    """Evidence ids of the run's committed tape reads (capture, engine, investigation)."""
    with session_factory() as session:
        return {
            evidence
            for row in InvestigationReadRepository(session).list_for_run(run_id)
            if row.committed_at is not None
            for evidence in row.evidence_ids
        }


def revision_facts(
    diagnosis_id: int,
    session_factory: sessionmaker[Session],
    *,
    replay: Replay = _replay,
) -> RevisionFacts:
    with session_factory() as session:
        row = session.scalars(
            select(DiagnosisRow).where(DiagnosisRow.diagnosis_id == diagnosis_id)
        ).one()
        document: Mapping[str, Any] = row.document
        number, trigger, run_id, digest = (
            row.revision_number,
            row.trigger,
            row.run_id,
            row.epistemic_digest,
        )
    if not run_id:
        raise ValueError(f"diagnosis {diagnosis_id} has no run id")
    base, uids, synthetic = source_evidence(run_id, session_factory)
    tape = tape_evidence(run_id, session_factory)
    replayed: str | None = None
    reason: str | None = None
    try:
        replayed = replay(run_id, session_factory)
    except ReplayModeUnsupported as error:
        status, reason = "UNSUPPORTED", str(error)
    except _DIVERGED as error:  # the replay left the recorded execution
        status, reason = "DIVERGED", str(error)
    except Exception as error:  # a replay that could not run is never a silent match
        status, reason = "ERROR", f"{type(error).__name__}: {error}"
    else:
        status = "PASS" if replayed == digest else "DIVERGED"
        if status == "DIVERGED":
            reason = "replayed epistemic digest differs from the persisted one"
    return RevisionFacts(
        number=number,
        trigger=trigger,
        diagnosis=Diagnosis.model_validate(document),
        universe=frozenset(base | tape),
        tape_ids=frozenset(tape),
        evidence_uids=uids,
        synthetic_ids=frozenset(synthetic),
        persisted_digest=digest,
        replay_digest=replayed,
        replay_status=status,
        replay_reason=reason,
    )


def collect(
    expectation: Expectation,
    timeline: Sequence[object],
    diagnosis_ids: Sequence[int],
    session_factory: sessionmaker[Session],
    *,
    replay: Replay = _replay,
) -> ProofInput:
    """R1, R_early and R2 facts plus the verified receipts; reads only."""
    return ProofInput(
        expectation=expectation,
        receipts=receipts_of(timeline),
        revisions=tuple(
            revision_facts(item, session_factory, replay=replay) for item in diagnosis_ids
        ),
    )


__all__ = ["collect", "revision_facts", "source_evidence", "tape_evidence"]
