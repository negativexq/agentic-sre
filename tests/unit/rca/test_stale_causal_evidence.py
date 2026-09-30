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
