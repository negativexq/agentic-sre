"""Write and load a run's recorded investigation trajectory (``investigation_runs``).

The trajectory artifact holds the run's actions and, from version 1.1, the
replay contract needed to replay them. Run boundary metadata stays in the
run's ``EVIDENCE_GATHERED`` event. Version 1.0 artifacts remain readable
elsewhere but are not trajectory-replayable: they never recorded the policy
family, config or terminal a replay needs, and none of it is inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from packages.rca.investigation.state import (
    InvestigationConfig,
    investigation_config_from_document,
)
from packages.rca.model import InvestigationResult, TrajectoryReplayContract
from packages.storage.manifest import ReplayDataError
from packages.storage.models import InvestigationRunRow

TRAJECTORY_ARTIFACT_VERSION = "1.1"


class ReplayTrajectoryMissing(ReplayDataError):
    """The run has no persisted investigation trajectory."""


class ReplayTrajectoryNotReplayable(ReplayDataError):
    """The trajectory's artifact version carries no replay contract (1.0) or is unknown."""


class ReplayTrajectoryMalformed(ReplayDataError):
    """A 1.1 trajectory artifact is missing its contract or is internally inconsistent."""


@dataclass(frozen=True)
class RecordedTrajectory:
    """One run's recorded investigation and the contract to replay it."""

    run_id: str
    result: InvestigationResult
    contract: TrajectoryReplayContract
    config: InvestigationConfig


def trajectory_document(result: InvestigationResult) -> dict[str, Any]:
    """The 1.1 artifact document; a result without a replay contract is never written."""
    if result.replay_contract is None:
        raise ValueError("a 1.1 trajectory artifact requires a replay contract")
    return result.model_dump(mode="json")


def load_trajectory(session: Session, run_id: str) -> RecordedTrajectory:
    """The run's own 1.1 trajectory artifact, validated; anything else is an error."""
    row = session.get(InvestigationRunRow, run_id)
    if row is None:
        raise ReplayTrajectoryMissing(f"run {run_id} has no investigation trajectory")
    if row.artifact_version != TRAJECTORY_ARTIFACT_VERSION:
        raise ReplayTrajectoryNotReplayable(
            f"run {run_id} trajectory artifact {row.artifact_version!r} is not replayable; "
            f"trajectory replay needs {TRAJECTORY_ARTIFACT_VERSION!r}"
        )
    try:
        result = InvestigationResult.model_validate(row.document)
    except ValidationError as error:
        raise ReplayTrajectoryMalformed(
            f"run {run_id} trajectory document is malformed: {error.error_count()} error(s)"
        ) from error
    contract = result.replay_contract
    if contract is None:
        raise ReplayTrajectoryMalformed(f"run {run_id} 1.1 trajectory has no replay contract")
    try:
        config = investigation_config_from_document(contract.config)
    except (TypeError, ValueError, KeyError) as error:
        raise ReplayTrajectoryMalformed(
            f"run {run_id} trajectory config is malformed: {error}"
        ) from error
    terminal = contract.terminal
    if (
        terminal.stop_reason is not result.stop_reason
        or terminal.turns != result.turns
        or terminal.audited_turns != len(result.action_audits)
        or terminal.turns - terminal.audited_turns not in (0, 1)
    ):
        raise ReplayTrajectoryMalformed(
            f"run {run_id} trajectory terminal disagrees with its recorded result"
        )
    return RecordedTrajectory(run_id, result, contract, config)


__all__ = [
    "TRAJECTORY_ARTIFACT_VERSION",
    "RecordedTrajectory",
    "ReplayTrajectoryMalformed",
    "ReplayTrajectoryMissing",
    "ReplayTrajectoryNotReplayable",
    "load_trajectory",
    "trajectory_document",
]
