"""M20.2: the product investigation is deterministic by default and keeps the base evidence."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, _deployment, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers
from test_trajectory_replay import _prepare

import packages.rca.llm as llm_module
from apps.control_plane.diagnosis import DiagnosisService, service_from_environment
from packages.evals.product import proof_inputs
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import LLMInvestigationPolicy
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.state import (
    SEED_BOUNDED_INITIAL_VIEW,
    SEED_FULL_SOURCE,
    investigation_config_from_document,
)
from packages.rca.model import InvestigationResult
from packages.rca.replay import replay_run
from packages.storage.models import DiagnosisRow, InvestigationReadRow, InvestigationRunRow
from packages.storage.repositories import DiagnosisRepository

POLICIES = [
    pytest.param(DeterministicIntentPolicy, id="intent"),
    pytest.param(DeterministicObservationPolicy, id="observation"),
]


def _competing_changes(world: Any) -> None:
    """Two initiated changes before onset: the diagnosis stays AMBIGUOUS."""
    factory, cluster, clock, _ = world
    DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    ).snapshot()
    _prepare(world)
    clock.now = T0 + timedelta(minutes=10)
    cluster.objects[0] = _deployment("5000")
    order = copy.deepcopy(cluster.objects[1])
    order["metadata"]["resourceVersion"] = "77"
    order["spec"]["template"]["spec"]["containers"][0]["image"] = "order:2"
    cluster.objects[1] = order
    DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    ).snapshot()
    clock.now = T0 + timedelta(minutes=13)


def _run(world: Any, readers: Readers, **kwargs: Any) -> tuple[str, DiagnosisRow]:
    factory, cluster, clock, incident_id = world
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
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        session.expunge(row)
    return run_id, row


def _tape(factory: sessionmaker[Session], run_id: str, caller: str) -> list[tuple[str, str]]:
    with factory() as session:
        rows = session.scalars(
            select(InvestigationReadRow)
            .where(
                InvestigationReadRow.run_id == run_id, InvestigationReadRow.caller_class == caller
            )
            .order_by(InvestigationReadRow.sequence)
        ).all()
        return [(row.capability, row.query_key) for row in rows]


def _trajectory(factory: sessionmaker[Session], run_id: str) -> InvestigationResult:
    with factory() as session:
        row = session.get(InvestigationRunRow, run_id)
        assert row is not None
        return InvestigationResult.model_validate(row.document)


@pytest.mark.parametrize("policy", POLICIES)
def test_the_product_investigation_starts_from_the_base_evidence(setup: Any, policy: Any) -> None:  # noqa: F811
    _competing_changes(setup)
    factory = setup[0]
    base_run, base = _run(setup, Readers())
    run_id, investigated = _run(setup, Readers(), bounded_policy_factory=policy)

    result = _trajectory(factory, run_id)
    assert result.replay_contract is not None
    recorded = investigation_config_from_document(result.replay_contract.config)
    assert result.replay_contract.config["seed_mode"] == SEED_FULL_SOURCE
    assert recorded.seed_mode == SEED_FULL_SOURCE
    # The investigation really ran, deterministically.
    assert result.turns > 0 and result.action_audits
    assert result.model_calls == 0 and investigated.document["model_calls"] == 0
    assert investigated.document["resolution"] == "AMBIGUOUS"
    # Every ENGINE read the base path makes is still made.
    assert _tape(factory, base_run, "ENGINE")
    assert _tape(factory, run_id, "ENGINE") == _tape(factory, base_run, "ENGINE")
    # No-op identity: nothing decision-relevant was acquired, so the decision is the base one.
    assert investigated.epistemic_digest == base.epistemic_digest
    # The live fixture offers only source-backed candidates here, so no provider read.
    assert {audit.action.capability for audit in result.action_audits} == {"history"}
    assert _tape(factory, run_id, "INVESTIGATION") == []


@pytest.mark.parametrize("policy", POLICIES)
def test_a_product_investigation_replays_with_its_recorded_seed(setup: Any, policy: Any) -> None:  # noqa: F811
    _competing_changes(setup)
    factory = setup[0]
    run_id, row = _run(setup, Readers(), bounded_policy_factory=policy)
    for mode in ("trajectory", "selector"):
        assert replay_run(run_id, mode, session_factory=factory) == row.epistemic_digest
    assert proof_inputs.revision_facts(row.diagnosis_id, factory).replay_status == "PASS"


def test_the_seed_mode_is_part_of_the_recorded_config_digest(setup: Any) -> None:  # noqa: F811
    _competing_changes(setup)
    factory = setup[0]
    full_run, full = _run(setup, Readers(), bounded_policy_factory=DeterministicIntentPolicy)
    bounded_run, bounded = _run(
        setup,
        Readers(),
        bounded_policy_factory=DeterministicIntentPolicy,
        investigation_seed_mode=SEED_BOUNDED_INITIAL_VIEW,
    )
    assert full.config_digest != bounded.config_digest
    bounded_config = _trajectory(factory, bounded_run).replay_contract
    assert (
        bounded_config is not None
        and bounded_config.config["seed_mode"] == SEED_BOUNDED_INITIAL_VIEW
    )
    # The benchmark seed withholds the base path's ENGINE reads; the product seed does not.
    assert _tape(factory, bounded_run, "ENGINE") == []
    assert _tape(factory, full_run, "ENGINE")


def _no_client(*_: Any, **__: Any) -> Any:
    raise AssertionError("a deterministic policy must not build an LLM client")


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        pytest.param({}, DeterministicIntentPolicy, id="default"),
        pytest.param({"SRE_LLM_ENABLED": "true"}, DeterministicIntentPolicy, id="llm-available"),
        pytest.param(
            {"SRE_LLM_ENABLED": "true", "SRE_INVESTIGATION_POLICY": "deterministic_observation"},
            DeterministicObservationPolicy,
            id="observation",
        ),
    ],
)
def test_the_control_plane_builds_the_deterministic_policy_without_a_model(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    expected: type,
) -> None:
    for name in ("SRE_INVESTIGATION_ENABLED", "SRE_INVESTIGATION_POLICY", "SRE_LLM_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(llm_module, "OpenAIClient", _no_client)
    service = service_from_environment(setup[0])
    assert isinstance(service.bounded_policy_factory(), expected)
    assert service.investigation_seed_mode == SEED_FULL_SOURCE


def test_the_control_plane_builds_the_llm_policy_only_when_chosen(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SRE_INVESTIGATION_POLICY", "llm")
    monkeypatch.setenv("SRE_LLM_ENABLED", "true")
    monkeypatch.setattr(llm_module, "OpenAIClient", lambda: object())
    service = service_from_environment(setup[0])
    assert isinstance(service.bounded_policy_factory(), LLMInvestigationPolicy)


def test_investigation_disabled_keeps_the_direct_diagnose_path(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SRE_INVESTIGATION_ENABLED", "false")
    monkeypatch.delenv("SRE_LLM_ENABLED", raising=False)
    service = service_from_environment(setup[0])
    assert service.bounded_policy_factory() is None
    assert service.investigator_factory() is None
