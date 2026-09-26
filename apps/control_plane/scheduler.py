"""Deadline reevaluation: OPEN evidence requirements trigger scheduler revisions (M19-5.6).

The scheduler has no RCA decision authority. It reads incidents' OPEN
requirements and revisions, marks requirements EXPIRED at their horizon, and
asks the diagnosis service for one ``EVIDENCE_DEADLINE`` revision per incident
whose deadline is due and not yet consumed. Whatever that revision concludes,
the requirement lifecycle (M19-5.5) applies it.

Idempotency is proven for the single control-plane, single watch-loop
deployment; concurrent scheduler workers need their own coordination contract.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from packages.storage.repositories import DiagnosisRepository, EvidenceRequirementRepository

logger = logging.getLogger(__name__)

EVIDENCE_DEADLINE = "EVIDENCE_DEADLINE"
_DURATION = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


@dataclass(frozen=True)
class ReevaluationConfig:
    """When OPEN requirements become due, and the bounds on reevaluation."""

    settle: timedelta = timedelta(seconds=90)
    max_revisions: int = 6
    horizon: timedelta = timedelta(hours=2)


@dataclass(frozen=True)
class SchedulerPass:
    """What one pass did; for logs and tests, never read by RCA."""

    expired: tuple[int, ...] = ()
    triggered: tuple[UUID, ...] = ()
    at_max_revisions: tuple[UUID, ...] = ()
    failed: tuple[UUID, ...] = ()


def _duration(name: str, value: str) -> timedelta:
    match = _DURATION.match(value)
    if match is None:
        raise ValueError(f"{name} must be a whole number of seconds, minutes (m) or hours (h)")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])


def reevaluation_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ReevaluationConfig | None:
    """``None`` unless ``SRE_REEVALUATE=true``; malformed bounds fail loudly."""
    env = os.environ if environ is None else environ
    if env.get("SRE_REEVALUATE", "").casefold() != "true":
        return None
    max_revisions = int(env.get("SRE_MAX_REVISIONS", "6"))
    if max_revisions < 1:
        raise ValueError("SRE_MAX_REVISIONS must be at least 1")
    return ReevaluationConfig(
        settle=_duration(
            "SRE_REEVALUATE_SETTLE_SECONDS", env.get("SRE_REEVALUATE_SETTLE_SECONDS", "90")
        ),
        max_revisions=max_revisions,
        horizon=_duration("SRE_REEVALUATE_HORIZON", env.get("SRE_REEVALUATE_HORIZON", "2h")),
    )


def run_scheduler_pass(
    session_factory: sessionmaker[Session],
    config: ReevaluationConfig,
    *,
    now: datetime,
    run: Callable[[UUID, str], Any],
) -> SchedulerPass:
    """One deterministic pass at ``now``.

    Order: load OPEN requirements; establish each horizon from its opening
    revision's onset; expire those at or past it; drop the not yet due
    (``now < not_before + settle``) and the deadline-consumed; group by
    incident; skip incidents at ``max_revisions``; trigger one revision per
    remaining incident. Expiry commits before any diagnosis runs.
    """
    with session_factory() as session:
        requirements = EvidenceRequirementRepository(session)
        diagnoses = DiagnosisRepository(session)
        open_rows = requirements.open_requirements()
        # Every horizon is established before anything is written: an
        # unreadable onset fails the whole pass rather than expiring anything.
        horizons = {row.requirement_id: requirements.opening_onset(row) for row in open_rows}
        expired = [
            row.requirement_id
            for row in open_rows
            if now >= horizons[row.requirement_id] + config.horizon
        ]
        requirements.expire(expired)
        session.commit()
        due: dict[UUID, None] = {}
        for row in open_rows:
            if row.requirement_id in expired or now < row.not_before + config.settle:
                continue
            if diagnoses.deadline_consumed(
                incident_id=row.incident_id,
                after_diagnosis_id=row.diagnosis_id,
                not_before=row.not_before,
            ):
                continue
            due[row.incident_id] = None
        eligible: list[UUID] = []
        at_max: list[UUID] = []
        for incident_id in due:
            if diagnoses.revision_count(incident_id) >= config.max_revisions:
                at_max.append(incident_id)
            else:
                eligible.append(incident_id)
    for incident_id in at_max:
        logger.info("incident %s is at SRE_MAX_REVISIONS; no deadline revision", incident_id)
    triggered: list[UUID] = []
    failed: list[UUID] = []
    for incident_id in eligible:
        try:
            run(incident_id, EVIDENCE_DEADLINE)
        except Exception:
            logger.warning("deadline revision failed for incident %s", incident_id, exc_info=True)
            failed.append(incident_id)
        else:
            triggered.append(incident_id)
    return SchedulerPass(
        expired=tuple(expired),
        triggered=tuple(triggered),
        at_max_revisions=tuple(at_max),
        failed=tuple(failed),
    )


__all__ = [
    "EVIDENCE_DEADLINE",
    "ReevaluationConfig",
    "SchedulerPass",
    "reevaluation_from_environment",
    "run_scheduler_pass",
]
