"""Slice 6 (`negative-control`, testbed contract §16): a real cause plus a decoy that must never be blamed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.evals.live.ground_truth import (
    Chain,
    Link,
    Source,
    Stamp,
    Timeline,
    chain_problems,
    timeline_problems,
)
from packages.evals.live.journal import JournalEntry, injector_stamps
from packages.evals.live.testbed_lab import ROLE_DECOY_CREATED, negative_spec
from packages.evals.live.testbed_runner import Injection, derive_parameters, negative_chain

AT = datetime(2026, 10, 3, tzinfo=UTC)


def change(decoy: Injection | None) -> Injection:
    return Injection(
        "payment-service",
        "uid-d",
        "payment-service-abc-1",
        "uid-p",
        AT,
        kind="Deployment",
        execution_name="payment-service-abc",
        execution_uid="uid-rs",
        decoy=decoy,
    )


DECOY = Injection("decoy-61", "uid-x", "isolated-echo-1", "uid-e", AT, namespace="lab-control")


def test_the_decoy_is_listed_apart_from_the_links_so_it_is_never_a_chain_actor() -> None:
    chain = negative_chain(change(DECOY))
    assert [d.actor for d in chain.decoys] == ["lab-control/NetworkChaos/decoy-61"]
    assert "lab-control/NetworkChaos/decoy-61" not in chain.actors()
    assert chain.construction and chain_problems(chain, "negative-control") == []


def test_a_control_with_a_cause_but_no_decoy_is_invalid() -> None:
    assert "a negative control with a cause must list its decoy" in chain_problems(
        negative_chain(change(None)), "negative-control"
    )


def test_a_control_with_a_cause_needs_every_oracle_field_and_one_without_does_not() -> None:
    stamp = Stamp(at=AT, source=Source.INJECTOR)
    later = Stamp(at=AT + timedelta(seconds=40), source=Source.INJECTOR)
    bare = Timeline(cause_created_at=stamp, execution_started_at=stamp, alert_fired_at=later)
    with_cause = negative_chain(change(DECOY))
    assert "missing target_effect_at" in timeline_problems(bare, "negative-control", 0, with_cause)
    no_cause = Chain(construction="no call edge")
    assert "missing target_effect_at" not in timeline_problems(
        bare, "negative-control", 0, no_cause
    )
    assert not Chain(
        links=(Link(role="symptom", actor="a/B/c", knowable=False, mechanism="m"),)
    ).decoys


def test_a_decoy_created_first_never_becomes_the_causes_instant() -> None:
    entries = [
        JournalEntry(
            seq=1,
            at=AT,
            verb="apply",
            object="networkchaos decoy-61",
            ok=True,
            role=ROLE_DECOY_CREATED,
        ),
        JournalEntry(
            seq=2,
            at=AT + timedelta(seconds=20),
            verb="patch",
            object="deployment",
            ok=True,
            role="cause_created",
        ),
    ]
    assert injector_stamps(entries)["cause_created_at"] == AT + timedelta(seconds=20)


def test_the_decoy_starts_around_the_cause() -> None:
    params = derive_parameters(negative_spec(3, (61, 62, 63)), 61)
    assert params.fault == "env-delay" and -30 <= params.decoy_offset_seconds <= 30
