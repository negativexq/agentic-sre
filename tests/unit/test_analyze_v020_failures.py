"""Offline tests for the frozen v0.2.0 forensic analysis."""

from __future__ import annotations

import importlib.util
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

SCRIPT = Path(__file__).parents[2] / "scripts" / "analyze_v020_failures.py"
SPEC = importlib.util.spec_from_file_location("analyze_v020_failures", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ROOT = Path(__file__).parents[2]


def _benchmark() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((ROOT / "docs/benchmarks/v0.2.0-single-agent-live.json").read_text()),
    )


def test_analysis_parses_all_frozen_scenarios_and_reconciles_totals() -> None:
    analysis = MODULE.analyze_benchmark(_benchmark())
    assert len(analysis["per_scenario"]) == 10
    assert analysis["turn_usage"]["model_calls_total"] == 30
    assert analysis["turn_usage"]["tool_requests_total"] == 66
    assert analysis["turn_usage"]["tool_executions_total"] == 66
    assert analysis["official_benchmark_metrics"]["completion_rate"] == 0.9
    assert analysis["official_benchmark_metrics"]["service_accuracy"] == 0.4
    assert analysis["official_benchmark_metrics"]["mechanism_accuracy"] == 0.8
    assert analysis["official_benchmark_metrics"]["trigger_accuracy"] == 0.0
    assert analysis["official_benchmark_metrics"]["composite_rca"] == 0.5
    assert analysis["official_benchmark_metrics"]["valid_evidence_reference_rate"] == 0.9
    assert analysis["score_consistency"]["service"]["miss_count_including_stop"] == 6
    assert analysis["score_consistency"]["mechanism"]["miss_count_including_stop"] == 2
    assert analysis["score_consistency"]["mechanism"]["taxonomy_excludes_stop"] is True
    assert analysis["score_consistency"]["mechanism"]["taxonomy_wrong_mechanism"] == 1
    assert analysis["score_consistency"]["trigger"]["submitted_hypotheses"] == 9
    assert len(analysis["component_targeting"]["service_wrong_scenarios"]) == 6
    assert len(analysis["failure_layers"]["F_termination_calibration"]["affected_scenarios"]) == 1
    assert analysis["metric_interpretability"]["service_accuracy"]["confidence"] == "MEDIUM"
    assert "exact free-text" in analysis["metric_interpretability"]["service_accuracy"]["basis"]
    assert analysis["metric_interpretability"]["mechanism_accuracy"]["confidence"] == "HIGH"
    assert analysis["metric_interpretability"]["trigger_accuracy"]["confidence"] == "LOW"
    assert analysis["evidence_integrity"]["submitted_hypotheses"] == 9
    assert analysis["evidence_integrity"]["valid_submitted_hypotheses"] == 9
    assert analysis["evidence_integrity"]["submitted_hypothesis_integrity_rate"] == 1.0
    assert analysis["evidence_integrity"]["official_scenario_rate"] == 0.9
    assert analysis["score_consistency"]["trigger"]["miss_count_submitted"] == 9
    assert analysis["score_consistency"]["trigger"]["taxonomy_excludes_stop"] is True


def test_analysis_reports_tool_and_target_patterns_without_model_dependency() -> None:
    analysis = MODULE.analyze_benchmark(_benchmark())
    usage = analysis["tool_usage"]
    assert usage["tool_frequency"]["recent_deployment_changes"] == 8
    assert usage["tool_frequency"]["recent_configuration_changes"] == 7
    assert usage["tool_frequency"]["trace_detail"] == 0
    assert analysis["component_targeting"]["all_requests_target_alert_service"] is True
    assert analysis["component_targeting"]["alert_service_target_rate"] == 1.0
    assert analysis["component_targeting"]["service_correct_runs_alert_service_target_rate"] == 1.0
    assert analysis["component_targeting"]["service_wrong_runs_alert_service_target_rate"] == 1.0
    assert "alert_service_anchoring" not in {
        item["name"] for item in analysis["system_failure_modes"]
    }
    targeting = analysis["component_targeting"]
    assert targeting["cross_component_scenarios"] == ["V020-003"]
    assert targeting["cross_component_explored_scenarios"] == []
    assert targeting["cross_component_target_counts"]["V020-003"]["outside_alert_scope"] == 0
    by_scenario = {item["scenario_id"]: item for item in targeting["by_scenario"]}
    assert by_scenario["V020-003"]["alert_service"] == "order-service"
    assert by_scenario["V020-003"]["ground_truth_component"] == "payment-service"
    assert by_scenario["V020-003"]["alert_scope_matches_ground_truth"] is False
    assert by_scenario["V020-005"]["alert_service"] == "payment-service"
    assert by_scenario["V020-005"]["ground_truth_component"] == "payment-service"
    assert by_scenario["V020-005"]["alert_scope_matches_ground_truth"] is True
    assert by_scenario["V020-007"]["alert_service"] == "order-worker"
    assert by_scenario["V020-007"]["ground_truth_component"] == "order-worker"
    assert by_scenario["V020-009"]["alert_service"] == "payment-service"
    assert by_scenario["V020-009"]["ground_truth_component"] == "payment-service"
    assert analysis["turn_usage"]["tool_budget_saturated_scenarios"] == [
        "V020-003",
        "V020-007",
        "V020-009",
    ]
    assert analysis["component_targeting"]["alert_scope_method"].startswith(
        "canonical FIXTURE_BY_NAME"
    )
    assert analysis["system_observations"]["all_target_requests_on_alert_scope"] is True
    assert analysis["supported_failure_modes"] == [
        "cross_component_exploration_failure",
        "change_evidence_acquisition_miss",
    ]


def test_analysis_does_not_claim_predicted_confusion_data_exists() -> None:
    analysis = MODULE.analyze_benchmark(_benchmark())
    assert all(item["predicted_fields_persisted"] is False for item in analysis["per_scenario"])
    assert analysis["tool_usage"]["semantic_role_analysis"]["status"] == "UNKNOWN"


def test_analysis_rejects_wrong_scenario_order() -> None:
    benchmark = _benchmark()
    benchmark["scenarios"] = list(reversed(benchmark["scenarios"]))
    try:
        MODULE.analyze_benchmark(benchmark)
    except ValueError as exc:
        assert "frozen dataset order" in str(exc)
    else:
        raise AssertionError("wrong scenario order was accepted")


def test_analysis_hashes_supplied_benchmark_path(tmp_path: Path) -> None:
    source = tmp_path / "custom-benchmark.json"
    payload = _benchmark()
    payload["custom_marker"] = "different source"
    source.write_text(json.dumps(payload, sort_keys=True))

    analysis = MODULE.analyze_benchmark(payload, benchmark_path=source)

    assert analysis["source"]["benchmark_file_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert (
        analysis["source"]["benchmark_file_sha256"]
        != sha256((ROOT / "docs/benchmarks/v0.2.0-single-agent-live.json").read_bytes()).hexdigest()
    )


def test_analysis_rejects_unknown_fixture_and_alert_mismatch() -> None:
    benchmark = _benchmark()
    benchmark["scenarios"][0]["fixture"] = "not-a-frozen-fixture"
    try:
        MODULE.analyze_benchmark(benchmark)
    except ValueError as exc:
        assert "unknown frozen fixture" in str(exc)
    else:
        raise AssertionError("unknown fixture was accepted")

    benchmark = _benchmark()
    benchmark["scenarios"][0]["alert_name"] = "WrongAlert"
    try:
        MODULE.analyze_benchmark(benchmark)
    except ValueError as exc:
        assert "does not match fixture definition" in str(exc)
    else:
        raise AssertionError("alert/fixture mismatch was accepted")
