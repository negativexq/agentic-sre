"""F7 prerequisite: EnvPatch — the protocol's env-patch root as a verified product action."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_product_actions import FakeControl, FakeEvidence, at, version
from test_product_runner import Commands, _control

from packages.evals.product.actions import (
    ActionReceipt,
    ActionVerificationError,
    ClusterControl,
    EnvControl,
    EnvPatch,
)
from packages.evals.product.artifact import describe_action
from packages.evals.product.runner import (
    ProductRunner,
    RecordingBackend,
    RecordingControl,
    RunnerConfig,
    RunStatus,
    await_evidence,
)
from packages.evals.product.spec import Expectation, Phase, ProductScenario

ROOT = Path(__file__).resolve().parents[3]
SERVICE = "payment-service"
PATCH = EnvPatch(SERVICE, {"FAULT_PAYMENT_DELAY_MS": "10000"})
OTEL = {"name": "OTEL_SERVICE_NAME", "value": "payment-service"}


def _deployment(env: list[dict[str, Any]], image: str = "payment:1") -> dict[str, Any]:
    return {
        "metadata": {"name": SERVICE},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": SERVICE, "image": image, "env": env}]}},
        },
    }


def _replica_set(env: list[dict[str, Any]]) -> dict[str, Any]:
    body = _deployment(env)
    body["metadata"] = {
        "name": "payment-service-new",
        "ownerReferences": [{"kind": "Deployment", "name": SERVICE, "controller": True}],
    }
    return body


PATCHED = [OTEL, {"name": "FAULT_PAYMENT_DELAY_MS", "value": "10000"}]


def _evidence(
    after: dict[str, Any] | None = None,
    rollout: dict[str, Any] | None = None,
    rollout_at: float = 6,
) -> FakeEvidence:
    journal = [version("Deployment", SERVICE, -60, _deployment([OTEL]))]
    journal.append(version("Deployment", SERVICE, 2, after or _deployment(PATCHED)))
    if rollout is not None:
        journal.append(
            version("ReplicaSet", "payment-service-new", rollout_at, rollout, lifecycle="CREATED")
        )
    return FakeEvidence(journal=journal)


RECEIPT = ActionReceipt(at(0), at(10))


def test_a_verified_env_patch_passes() -> None:
    PATCH.verify(_evidence(rollout=_replica_set(PATCHED)), RECEIPT)


def test_changing_an_existing_value_is_the_same_intended_diff() -> None:
    before = [OTEL, {"name": "FAULT_PAYMENT_DELAY_MS", "value": "0"}]
    evidence = FakeEvidence(
        journal=[
            version("Deployment", SERVICE, -60, _deployment(before)),
            version("Deployment", SERVICE, 2, _deployment(PATCHED)),
            version(
                "ReplicaSet", "payment-service-new", 6, _replica_set(PATCHED), lifecycle="CREATED"
            ),
        ]
    )
    PATCH.verify(evidence, RECEIPT)


@pytest.mark.parametrize(
    ("evidence", "invariant"),
    [
        pytest.param(
            FakeEvidence(journal=[version("Deployment", SERVICE, -60, _deployment([OTEL]))]),
            "no journal change",
            id="no-change",
        ),
        pytest.param(
            _evidence(after=_deployment(PATCHED, image="payment:2"), rollout=_replica_set(PATCHED)),
            "changes beyond the env values",
            id="image-too",
        ),
        pytest.param(
            _evidence(
                after=_deployment([{"name": "OTEL_SERVICE_NAME", "value": "x"}, PATCHED[1]]),
                rollout=_replica_set(PATCHED),
            ),
            "changes beyond the env values",
            id="other-env-too",
        ),
        pytest.param(
            _evidence(
                after=_deployment([OTEL, {"name": "FAULT_PAYMENT_DELAY_MS", "value": "3000"}]),
                rollout=_replica_set(PATCHED),
            ),
            "intended env not applied",
            id="wrong-value",
        ),
        pytest.param(_evidence(), "no new ReplicaSet", id="no-rollout"),
        pytest.param(
            _evidence(rollout=_replica_set([OTEL])),
            "no new ReplicaSet",
            id="rollout-without-the-env",
        ),
        pytest.param(
            _evidence(rollout=_replica_set(PATCHED), rollout_at=-5),
            "no new ReplicaSet",
            id="rollout-before-the-action",
        ),
    ],
)
def test_each_broken_invariant_fails_verification(evidence: FakeEvidence, invariant: str) -> None:
    with pytest.raises(ActionVerificationError, match=invariant):
        PATCH.verify(evidence, RECEIPT)


def test_a_missing_value_is_named() -> None:
    patch = EnvPatch(SERVICE, {"FAULT_PAYMENT_DELAY_MS": "10000", "FAULT_PAYMENT_ERROR": "true"})
    with pytest.raises(ActionVerificationError, match="changes beyond|not applied"):
        patch.verify(_evidence(rollout=_replica_set(PATCHED)), RECEIPT)


def test_apply_sets_env_then_waits_for_the_rollout() -> None:
    events: list[tuple[Any, ...]] = []
    control = RecordingControl(events)
    receipt = PATCH.apply(control)
    assert events == [
        ("set_env", SERVICE, SERVICE, {"FAULT_PAYMENT_DELAY_MS": "10000"}),
        ("wait_for_rollout", SERVICE),
    ]
    assert receipt.started_at <= receipt.finished_at


def test_a_control_without_env_support_is_refused() -> None:
    with pytest.raises(TypeError, match="cannot set"):
        PATCH.apply(FakeControl())


def test_the_live_control_uses_kubectl_set_env_on_one_container() -> None:
    commands = Commands()
    control = _control(commands, [])
    assert isinstance(control, EnvControl)
    control.set_env(SERVICE, SERVICE, {"B": "2", "A": "1"})
    ((argv, stdin),) = commands.calls
    assert argv[argv.index("set") :] == [
        "set",
        "env",
        f"deployment/{SERVICE}",
        "-c",
        SERVICE,
        "A=1",
        "B=2",
    ]
    assert stdin is None


def test_existing_action_contracts_are_unchanged() -> None:
    assert not hasattr(ClusterControl, "set_env")
    assert not hasattr(FakeControl, "set_env")


@pytest.mark.parametrize("values", [{}, {"": "1"}, {"A": ""}])
def test_invalid_patches_are_refused(values: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        EnvPatch(SERVICE, values)


def test_the_descriptor_and_the_protocol_root() -> None:
    assert describe_action(PATCH) == "EnvPatch:payment-service"
    protocol = (ROOT / "docs/architecture/m19-product-resolution-protocol.md").read_text()
    assert (
        'EnvPatch(deployment="payment-service", values={"FAULT_PAYMENT_DELAY_MS": "10000"}, wait=True)'
        in protocol
    )
    assert (
        'EnvPatch(deployment="payment-service", values={"FAULT_PAYMENT_DELAY_MS": "3000"}, wait=True)'
        in protocol
    )


def test_the_runner_waits_for_the_rollout_milestone_and_verifies() -> None:
    evidence = _evidence(rollout=_replica_set(PATCHED))
    control = RecordingControl([], start=at(0))
    receipt = PATCH.apply(control)
    assert await_evidence(
        PATCH, receipt, evidence, control, RunnerConfig(evidence_timeout=timedelta(0))
    )
    backend = RecordingBackend(FakeEvidence())
    result = ProductRunner(backend, RunnerConfig(evidence_timeout=timedelta(0))).run(
        ProductScenario("s", (Phase(timedelta(0), (PATCH,)),), Expectation())
    )
    assert result.status is RunStatus.ERROR and result.error_type == "ActionVerificationError"
