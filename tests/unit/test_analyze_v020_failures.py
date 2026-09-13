"""Offline tests for the frozen v0.2.0 forensic analysis."""

from __future__ import annotations

import importlib.util
import json
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
    assert analysis["turn_usage"]["tool_budget_saturated_scenarios"] == [
        "V020-003",
        "V020-007",
        "V020-009",
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
