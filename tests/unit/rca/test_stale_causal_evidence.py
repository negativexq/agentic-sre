"""A leader resting only on evidence outside the incident window is not presented as a cause (roadmap C10)."""

from __future__ import annotations

from rca_builders import alert, at, deployment, event, pod, replicaset, service, version

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest as epistemic_digest
from packages.rca.model import Diagnosis
from packages.rca.source import InMemorySource

POD = "shop/Pod/worker-rs-abcde"


def diagnose(failure_minute: float) -> Diagnosis:
    """A consumer-lag alert at minute 6 and one pod failure at ``failure_minute``; nothing else."""
    source = InMemorySource(
        name="stale",
        alert_coverage_start=at(-400),
        alert_items=[alert("ConsumerLag", "worker", 6)],
        versions=[
            version("shop/Deployment/worker", -400, deployment("worker")),
            version("shop/ReplicaSet/worker-rs", -400, replicaset("worker-rs", "worker")),
            version(POD, -400, pod("worker-rs-abcde", "worker", "worker-rs")),
            version("shop/Service/worker", -400, service("worker")),
        ],
        event_items=[
            event(
                POD,
                "BackOff",
                failure_minute,
                type_="Warning",
                message="Back-off restarting failed container",
            ).model_copy(update={"involved_uid": "p1"})
        ],
    )
    return diagnose_case(build_case(source), config=EngineConfig(timing_stability=False))


def test_a_leader_with_only_evidence_hours_before_the_window_is_withheld() -> None:
    stale = diagnose(-300)
    assert stale.resolution_trace is not None
    assert stale.resolution_trace.claim_level == "UNESTABLISHED"
    assert (
        stale.root_cause is not None and stale.root_cause.canonical == POD
    )  # the ranking is untouched
    assert not stale.leading_actor_established
    assert stale.leading_actor_withheld_reason == "NO_EVIDENCE_IN_INCIDENT_WINDOW"
    assert stale.summary.startswith("No causal candidate has evidence in the incident window")
    assert POD in stale.summary  # the observation is still named, as context, not as a cause


def test_the_same_leader_with_evidence_in_the_window_is_presented() -> None:
    fresh = diagnose(4)
    assert fresh.leading_actor_established and fresh.leading_actor_withheld_reason is None
    assert fresh.summary.startswith(f"Observed on {POD}")


def test_withholding_is_presentation_and_stays_out_of_the_epistemic_digest() -> None:
    stale = diagnose(-300)
    presented = stale.model_copy(
        update={"leading_actor_established": True, "leading_actor_withheld_reason": None}
    )
    assert epistemic_digest(stale) == epistemic_digest(presented)


def test_a_stored_document_from_before_the_field_is_presented_as_it_was() -> None:
    document = diagnose(-300).model_dump(mode="json")
    for key in ("leading_actor_established", "leading_actor_withheld_reason"):
        document.pop(key)
    assert Diagnosis.model_validate(document).leading_actor_established


def test_the_console_never_presents_a_withheld_actor_but_keeps_it_as_context() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from apps.control_plane.console.mappers import diagnosis_view, incident_list_item
    from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus

    stale, fresh = diagnose(-300), diagnose(4)
    view = diagnosis_view(stale)
    assert view.leading_root_actor is None and view.root_cause is None
    assert view.leading_actor_withheld_reason == "NO_EVIDENCE_IN_INCIDENT_WINDOW"
    assert diagnosis_view(fresh).leading_root_actor == POD

    now = datetime.now(UTC)
    incident = Incident(
        incident_id=uuid4(),
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.WARNING,
        source=IncidentSource.ALERTMANAGER,
        title="ConsumerLag",
        created_at=now,
        updated_at=now,
    )
    listed = incident_list_item(
        incident,
        {"root_cause": POD, "leading_actor_withheld_reason": "NO_EVIDENCE_IN_INCIDENT_WINDOW"},
    )
    assert listed.leading_root_actor is None and listed.leading_actor_withheld_reason
    # a view stored before the field existed is presented as it was
    assert incident_list_item(incident, {"root_cause": POD}).leading_root_actor == POD


# ---- m21 contract §18: an unestablished leader needs evidence near the onset ------------------------------


def test_a_leader_whose_only_evidence_is_in_the_window_but_long_before_the_onset_is_withheld() -> (
    None
):
    old = diagnose(-10)  # sixteen minutes before the alert, inside the two-hour window
    assert (
        old.root_cause is not None and old.root_cause.canonical == POD
    )  # the ranking is untouched
    assert not old.leading_actor_established
    assert old.leading_actor_display == "NOT_ESTABLISHED"
    assert old.leading_actor_withheld_reason == "NO_EVIDENCE_NEAR_ONSET"
    assert old.summary.startswith("No causal candidate has evidence near the onset")
    assert "16 min before it" in old.summary and POD in old.summary


def test_evidence_within_five_minutes_before_the_onset_still_presents_the_leader() -> None:
    near = diagnose(2)
    assert near.leading_actor_established and near.leading_actor_withheld_reason is None


def test_the_near_onset_rule_changes_no_digest() -> None:
    old = diagnose(-10)
    presented = old.model_copy(
        update={
            "leading_actor_established": True,
            "leading_actor_withheld_reason": None,
            "leading_actor_display": "SINGLE",
        }
    )
    assert epistemic_digest(old) == epistemic_digest(presented)
