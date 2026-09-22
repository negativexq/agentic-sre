from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.evals.live.actions import (
    ApplyFile,
    ApplyManifest,
    Context,
    DeleteObject,
    EnvPatch,
    HttpFault,
    PatchConfigMap,
    PatchService,
    Scale,
    SetImage,
    SetResources,
)
from packages.evals.live.grader import Outcome, ScenarioResult, SuiteReport, grade
from packages.evals.live.scenarios import (
    DEMO_SCENARIOS,
    SCENARIO_BY_ID,
    SCENARIOS,
    Target,
    Tier,
    scenarios_for,
)

ROOT = Path(__file__).resolve().parents[3]
DEPLOYED_RULES = ROOT / "infra" / "kubernetes" / "observability.yaml"
WORKLOAD_MANIFEST = ROOT / "infra" / "kubernetes" / "workload.yaml"


def deployed_alert_names() -> set[str]:
    text = DEPLOYED_RULES.read_text()
    return set(re.findall(r"- alert:\s*(\S+)", text))


def test_every_scenario_alert_is_actually_deployed() -> None:
    """A scenario whose alert does not exist can never open an incident."""
    available = deployed_alert_names()
    missing = sorted({item.alert for item in SCENARIOS} - available)
    assert not missing, f"scenarios reference undeployed alerts: {missing}"


def test_scenario_ids_are_unique_and_stable() -> None:
    ids = [item.id for item in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(SCENARIO_BY_ID)


def test_suite_is_large_enough_and_balanced() -> None:
    root_cause = [item for item in SCENARIOS if item.expects_root_cause]
    abstain = [item for item in SCENARIOS if not item.expects_root_cause]
    assert len(SCENARIOS) >= 25
    # Both axes must carry real weight: a suite that only rewards answers cannot
    # detect fabrication, and one that only rewards silence cannot detect misses.
    assert len(root_cause) >= 10
    assert len(abstain) >= 5


def test_holdout_tier_is_reserved_and_non_empty() -> None:
    holdout = scenarios_for(tier=Tier.HOLDOUT)
    dev = scenarios_for(tier=Tier.DEV)
    assert holdout and dev
    assert not {item.id for item in holdout} & {item.id for item in dev}
    # The holdout must test both expectation classes or it measures only one.
    assert any(item.expects_root_cause for item in holdout)
    assert any(not item.expects_root_cause for item in holdout)


def test_demo_scenarios_all_expect_a_root_cause() -> None:
    """The UI demo shows the engine working; abstention scenarios are not demos."""
    assert DEMO_SCENARIOS
    assert all(item.expects_root_cause for item in DEMO_SCENARIOS)


def test_every_cluster_mutation_has_a_teardown() -> None:
    """A scenario that changes the cluster and cannot undo it poisons later runs."""
    mutating = (
        EnvPatch,
        SetImage,
        Scale,
        ApplyManifest,
        DeleteObject,
        SetResources,
        PatchConfigMap,
        PatchService,
    )
    for scenario in SCENARIOS:
        if not any(isinstance(action, mutating) for action in scenario.setup):
            continue
        assert scenario.teardown, f"{scenario.id} mutates the cluster with no teardown"


def test_runtime_only_scenarios_never_touch_kubernetes() -> None:
    """The abstention expectation is only honest when no cluster change is staged."""
    for scenario in SCENARIOS:
        if scenario.expects_root_cause:
            continue
        assert all(isinstance(action, HttpFault) for action in scenario.setup), (
            f"{scenario.id} expects abstention but stages a cluster change"
        )


def test_scenarios_that_expect_a_root_cause_stage_a_cluster_change() -> None:
    for scenario in SCENARIOS:
        if not scenario.expects_root_cause:
            continue
        assert scenario.setup, f"{scenario.id} expects a root cause but stages nothing"
        assert not all(isinstance(action, HttpFault) for action in scenario.setup), (
            f"{scenario.id} expects a root cause from a runtime-only fault"
        )


def test_reapplied_manifest_paths_exist() -> None:
    for scenario in SCENARIOS:
        for action in scenario.teardown:
            if isinstance(action, ApplyFile):
                assert (ROOT / action.path).is_file(), f"{scenario.id}: missing {action.path}"


def test_deleted_objects_are_recreatable_from_the_repository() -> None:
    """Deleting a manifest-owned object is only safe if a file can restore it."""
    manifest = WORKLOAD_MANIFEST.read_text()
    for scenario in SCENARIOS:
        deletions = [item for item in scenario.setup if isinstance(item, DeleteObject)]
        for deletion in deletions:
            assert any(isinstance(item, ApplyFile) for item in scenario.teardown), (
                f"{scenario.id} deletes {deletion.name} without reapplying a manifest"
            )
            assert f"name: {deletion.name}" in manifest


def test_every_action_describes_itself_without_a_cluster() -> None:
    for scenario in SCENARIOS:
        for action in (*scenario.setup, *scenario.teardown):
            assert action.describe().strip()


def test_dry_run_context_applies_no_action() -> None:
    context = Context(dry_run=True)
    for scenario in SCENARIOS:
        for action in (*scenario.setup, *scenario.teardown):
            action.apply(context)


def test_workload_targets_have_an_endpoint() -> None:
    for scenario in SCENARIOS:
        if scenario.workload.target is Target.NONE:
            assert scenario.workload.count == 0
        else:
            assert scenario.workload.count > 0


def test_selecting_an_unknown_scenario_is_rejected() -> None:
    with pytest.raises(KeyError):
        scenarios_for(ids=("no_such_scenario",))


def test_fault_endpoints_are_enabled_in_the_deployed_workload() -> None:
    """Runtime-fault scenarios need the endpoint the manifest gates."""
    assert 'ENABLE_TEST_FAULTS: "true"' in WORKLOAD_MANIFEST.read_text()


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


def _document(root_cause: dict[str, str] | None, kinds: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "root_cause": root_cause,
        "resolution": "RESOLVED" if root_cause else "INSUFFICIENT_EVIDENCE",
        "confidence": "VERIFIED",
        "evidence": [{"kind": kind} for kind in kinds],
    }


def test_abstention_is_correct_when_nothing_is_named() -> None:
    scenario = SCENARIO_BY_ID["payment_error_spike"]
    assert grade(scenario, _document(None)).outcome is Outcome.CORRECT


def test_resolving_a_cause_with_no_change_is_a_fabrication() -> None:
    scenario = SCENARIO_BY_ID["payment_error_spike"]
    document = _document({"namespace": "sre-demo", "kind": "Deployment", "name": "payment-service"})
    assert document["resolution"] == "RESOLVED"
    result = grade(scenario, document)
    assert result.outcome is Outcome.FABRICATED
    assert not result.passed


def test_tentative_localization_without_a_change_is_honest_abstention() -> None:
    """A failing pod surfaced under INSUFFICIENT_EVIDENCE is localization off a
    real failure event, not a fabricated change-based root cause."""
    scenario = SCENARIO_BY_ID["payment_error_spike"]
    document = {
        "root_cause": {"namespace": "sre-demo", "kind": "Pod", "name": "payment-service-abc"},
        "resolution": "INSUFFICIENT_EVIDENCE",
        "confidence": "UNVERIFIED",
        "evidence": [{"kind": "FAILURE_EVENT"}],
    }
    result = grade(scenario, document)
    assert result.outcome is Outcome.CORRECT
    assert result.actual.endswith("Pod/payment-service-abc")


def test_expected_actor_is_graded_correct() -> None:
    scenario = SCENARIO_BY_ID["payment_config_change"]
    document = _document(
        {"namespace": "sre-demo", "kind": "Deployment", "name": "payment-service"},
        ("SPEC_CHANGE",),
    )
    result = grade(scenario, document)
    assert result.outcome is Outcome.CORRECT
    assert result.supported_by_expected_kind


def test_correct_actor_without_the_expected_finding_is_flagged() -> None:
    scenario = SCENARIO_BY_ID["payment_config_change"]
    document = _document(
        {"namespace": "sre-demo", "kind": "Deployment", "name": "payment-service"},
        ("TRAFFIC_INCREASE",),
    )
    result = grade(scenario, document)
    assert result.outcome is Outcome.CORRECT
    assert not result.supported_by_expected_kind
    assert "SPEC_CHANGE" in result.detail


def test_a_pod_owned_by_the_expected_deployment_counts_as_the_same_answer() -> None:
    scenario = SCENARIO_BY_ID["payment_pod_crash"]
    document = _document(
        {"namespace": "sre-demo", "kind": "Pod", "name": "payment-service-7d9c4b-xk2ls"},
        ("CONTAINER_FAILURE",),
    )
    assert grade(scenario, document).outcome is Outcome.CORRECT


def test_an_unrelated_pod_does_not_count_as_the_expected_deployment() -> None:
    scenario = SCENARIO_BY_ID["payment_pod_crash"]
    document = _document(
        {"namespace": "sre-demo", "kind": "Pod", "name": "order-service-7d9c4b-xk2ls"},
        ("CONTAINER_FAILURE",),
    )
    assert grade(scenario, document).outcome is Outcome.WRONG_ACTOR


def test_missing_a_staged_change_is_not_an_abstention_success() -> None:
    scenario = SCENARIO_BY_ID["payment_config_change"]
    assert grade(scenario, _document(None)).outcome is Outcome.MISSED


def test_absent_diagnosis_is_reported_separately() -> None:
    scenario = SCENARIO_BY_ID["payment_config_change"]
    assert grade(scenario, None).outcome is Outcome.NO_DIAGNOSIS


def test_report_excludes_harness_failures_from_the_denominator() -> None:
    scenario = SCENARIO_BY_ID["payment_error_spike"]
    correct = grade(scenario, _document(None))
    fabricated = grade(
        scenario, _document({"namespace": "sre-demo", "kind": "Deployment", "name": "x"})
    )
    never_alerted = ScenarioResult(
        scenario_id="order_worker_lag",
        outcome=Outcome.NO_INCIDENT,
        expected="sre-demo/Deployment/order-worker",
        actual="",
    )
    report = SuiteReport(results=(correct, fabricated, never_alerted))
    assert len(report.graded) == 2
    assert report.correct == 1
    assert report.accuracy() == pytest.approx(0.5)
    assert report.fabrications == 1


def test_render_states_the_headline_numbers() -> None:
    scenario = SCENARIO_BY_ID["payment_error_spike"]
    report = SuiteReport(results=(grade(scenario, _document(None)),))
    rendered = report.render()
    assert "payment_error_spike" in rendered
    assert "correct 1/1" in rendered
