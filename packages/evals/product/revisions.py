"""Revision schedule of the product-resolution harness (M19-6.10).

R1 is the control plane's auto-diagnosis (``INITIAL``), discovered with reads
only. R_early is the harness's single diagnosis POST (``MANUAL``) at
onset + 8 min. R2 is produced by the scheduler alone (``EVIDENCE_DEADLINE``);
the harness only reads until onset + 25 min.

R1 must be provenance-clean: its incident opened at or after T0 and every
alert behind it became active (exact Prometheus ``activeAt``) at or after T0.
Alertmanager's ``startsAt`` is the firing time and is never used to infer
activation. This proves post-T0 activation of each alert, not that every
sample in a rule's range vector is post-T0.

Any unknown, ambiguous or out-of-order fact is ``RevisionScheduleError``,
which makes the run ``ERROR``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from packages.evals.product.actions import ClusterControl

CHAIN = ("INITIAL", "MANUAL", "EVIDENCE_DEADLINE")


@dataclass(frozen=True, slots=True)
class IncidentView:
    incident_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AlertView:
    labels: Mapping[str, str]
    starts_at: datetime


@dataclass(frozen=True, slots=True)
class RevisionView:
    diagnosis_id: int
    revision_number: int
    trigger: str
    previous_diagnosis_id: int | None


class ControlPlaneReads(Protocol):
    """Read-only control-plane facts."""

    def incidents(self) -> Sequence[IncidentView]: ...

    def alerts(self, incident_id: str) -> Sequence[AlertView]: ...

    def revisions(self, incident_id: str) -> Sequence[RevisionView]: ...

    def onset(self, incident_id: str, revision_number: int) -> Any: ...


class ManualDiagnosis(Protocol):
    """The harness's only diagnosis/revision write: one MANUAL revision."""

    def post_manual(self, incident_id: str) -> None: ...


class AlertActivations(Protocol):
    """Prometheus's currently pending/firing alerts, each with ``labels`` and ``activeAt``."""

    def active_alerts(self) -> Sequence[Mapping[str, Any]]: ...


class RevisionScheduleError(RuntimeError):
    """The revision chain or R1's provenance is not what the schedule requires."""


class NoR1(RevisionScheduleError):
    """No qualifying R1 within the discovery bound; ``incidents`` were seen at its end."""

    def __init__(self, incidents: int) -> None:
        self.incidents = incidents
        super().__init__(f"no R1 within the discovery bound ({incidents} incident(s) seen)")


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    r1_timeout: timedelta = timedelta(minutes=10)  # after T0; harness liveness bound
    r_early_after_onset: timedelta = timedelta(minutes=8)
    r2_after_onset: timedelta = timedelta(minutes=25)
    poll: timedelta = timedelta(seconds=15)


@dataclass(frozen=True, slots=True)
class AlertActivation:
    labels: Mapping[str, str]
    starts_at: datetime
    active_at: datetime


@dataclass(frozen=True, slots=True)
class R1:
    incident_id: str
    diagnosis_id: int
    onset: datetime
    discovered_at: datetime
    activations: tuple[AlertActivation, ...]


@dataclass(frozen=True, slots=True)
class REarly:
    diagnosis_id: int
    posted_at: datetime


def parse_time(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RevisionScheduleError(f"{what} is not a timestamp: {value!r}") from error
    else:
        raise RevisionScheduleError(f"{what} is missing")
    if parsed.tzinfo is None:
        raise RevisionScheduleError(f"{what} has no timezone: {value!r}")
    return parsed


def check_chain(revisions: Sequence[RevisionView], *, at_most: int) -> tuple[RevisionView, ...]:
    """The revisions as a valid prefix of INITIAL → MANUAL → EVIDENCE_DEADLINE."""
    ordered = tuple(sorted(revisions, key=lambda item: item.revision_number))
    if len(ordered) > at_most:
        raise RevisionScheduleError(
            f"unexpected revision {ordered[at_most].revision_number} ({ordered[at_most].trigger})"
        )
    previous: RevisionView | None = None
    for position, revision in enumerate(ordered):
        if revision.revision_number != position + 1:
            raise RevisionScheduleError(
                f"revision numbers are not 1..n: {revision.revision_number}"
            )
        if revision.trigger != CHAIN[position]:
            raise RevisionScheduleError(
                f"revision {revision.revision_number} is {revision.trigger}, "
                f"expected {CHAIN[position]}"
            )
        expected_previous = previous.diagnosis_id if previous is not None else None
        if revision.previous_diagnosis_id != expected_previous:
            raise RevisionScheduleError(
                f"revision {revision.revision_number} links to "
                f"{revision.previous_diagnosis_id}, expected {expected_previous}"
            )
        previous = revision
    return ordered


def _activations(
    alerts: Sequence[AlertView], active: Sequence[Mapping[str, Any]], t0: datetime
) -> tuple[AlertActivation, ...]:
    if not alerts:
        raise RevisionScheduleError("the incident has no alerts to prove its provenance")
    proven: list[AlertActivation] = []
    for alert in alerts:
        wanted = dict(alert.labels)
        matches = [item for item in active if dict(item.get("labels") or {}) == wanted]
        if len(matches) != 1:
            raise RevisionScheduleError(
                f"{len(matches)} active Prometheus alerts match {wanted.get('alertname')!r}"
            )
        active_at = parse_time(matches[0].get("activeAt"), "activeAt")
        if active_at > alert.starts_at:
            raise RevisionScheduleError(
                f"{wanted.get('alertname')!r} activeAt {active_at.isoformat()} is after its "
                f"startsAt {alert.starts_at.isoformat()}: a newer occurrence"
            )
        if active_at < t0:
            raise RevisionScheduleError(
                f"{wanted.get('alertname')!r} became active at {active_at.isoformat()}, "
                "before T0: pre-history origin"
            )
        proven.append(AlertActivation(wanted, alert.starts_at, active_at))
    return tuple(proven)


def discover_r1(
    reads: ControlPlaneReads,
    activations: AlertActivations,
    control: ClusterControl,
    *,
    t0: datetime,
    config: ScheduleConfig,
) -> R1:
    """The unique provenance-clean incident whose revision 1 is INITIAL; reads only."""
    deadline = t0 + config.r1_timeout
    while True:
        incidents = list(reads.incidents())
        early = [item.incident_id for item in incidents if item.created_at < t0]
        if early:
            raise RevisionScheduleError(f"incident(s) opened before T0: {sorted(early)}")
        if len(incidents) > 1:
            raise RevisionScheduleError(
                f"{len(incidents)} incidents after T0; R1 is not chosen among them"
            )
        if incidents:
            (incident,) = incidents
            chain = check_chain(reads.revisions(incident.incident_id), at_most=1)
            if chain:
                (first,) = chain
                proven = _activations(
                    reads.alerts(incident.incident_id), activations.active_alerts(), t0
                )
                onset = parse_time(reads.onset(incident.incident_id, 1), "R1 symptoms.onset")
                if onset < t0:
                    raise RevisionScheduleError(f"R1 onset {onset.isoformat()} is before T0")
                return R1(incident.incident_id, first.diagnosis_id, onset, control.now(), proven)
        now = control.now()
        if now >= deadline:
            raise NoR1(len(incidents))
        control.wait(min(config.poll, deadline - now))


def run_r_early(
    r1: R1,
    reads: ControlPlaneReads,
    manual: ManualDiagnosis,
    control: ClusterControl,
    *,
    config: ScheduleConfig,
) -> REarly:
    """The single MANUAL POST at onset + 8 min, then its revision 2; never a late catch-up."""
    target = r1.onset + config.r_early_after_onset
    now = control.now()
    if now > target:
        raise RevisionScheduleError(
            f"R_early target {target.isoformat()} already passed at {now.isoformat()}"
        )
    check_chain(reads.revisions(r1.incident_id), at_most=1)
    if target > now:
        control.wait(target - now)
    posted_at = control.now()
    manual.post_manual(r1.incident_id)
    chain = check_chain(reads.revisions(r1.incident_id), at_most=3)
    if len(chain) < 2:
        raise RevisionScheduleError("the MANUAL POST produced no revision 2")
    return REarly(chain[1].diagnosis_id, posted_at)


def await_r2(
    r1: R1,
    reads: ControlPlaneReads,
    control: ClusterControl,
    *,
    config: ScheduleConfig,
) -> int:
    """Read until the scheduler's revision 3 exists; a final read happens at the deadline."""
    deadline = r1.onset + config.r2_after_onset
    while True:
        chain = check_chain(reads.revisions(r1.incident_id), at_most=3)
        if len(chain) < 2:
            raise RevisionScheduleError("revision 2 (MANUAL) is missing")
        if len(chain) == 3:
            return chain[2].diagnosis_id
        now = control.now()
        if now >= deadline:
            raise RevisionScheduleError("no EVIDENCE_DEADLINE revision by onset + 25 min")
        control.wait(min(config.poll, deadline - now))


__all__ = [
    "CHAIN",
    "R1",
    "AlertActivation",
    "AlertActivations",
    "AlertView",
    "ControlPlaneReads",
    "IncidentView",
    "ManualDiagnosis",
    "NoR1",
    "REarly",
    "RevisionScheduleError",
    "RevisionView",
    "ScheduleConfig",
    "await_r2",
    "check_chain",
    "discover_r1",
    "parse_time",
    "run_r_early",
]
