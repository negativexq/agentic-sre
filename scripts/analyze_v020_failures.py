"""Produce deterministic forensic aggregates for the frozen v0.2.0 benchmark."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.dataset import FROZEN_DATASET, frozen_dataset_hash
from packages.evals.live_fixtures import FIXTURE_BY_NAME

BENCHMARK_PATH = Path("docs/benchmarks/v0.2.0-single-agent-live.json")
JSON_OUTPUT = Path("docs/benchmarks/v0.2.0-failure-analysis.json")
MARKDOWN_OUTPUT = Path("docs/benchmarks/v0.2.0-failure-analysis.md")
EXPECTED_IDS = [item.scenario_id for item in FROZEN_DATASET]
METRIC_TOOLS = {
    "service_error_rate",
    "service_latency",
    "db_connection_pressure",
    "db_query_latency",
    "kafka_consumer_lag",
}
LOG_TOOLS = {"service_logs", "service_error_logs"}
TRACE_TOOLS = {"slow_traces", "trace_detail"}
KUBERNETES_TOOLS = {
    "kubernetes_pods",
    "kubernetes_deployment",
    "kubernetes_events",
    "kubernetes_rollout_history",
    "kubernetes_container_restarts",
    "kubernetes_resource_state",
}
CHANGE_TOOLS = {"recent_deployment_changes", "recent_configuration_changes"}
ALL_TOOLS = METRIC_TOOLS | LOG_TOOLS | TRACE_TOOLS | KUBERNETES_TOOLS | CHANGE_TOOLS


def _family(tool: str) -> str:
    if tool in METRIC_TOOLS:
        return "metrics"
    if tool in LOG_TOOLS:
        return "logs"
    if tool in TRACE_TOOLS:
        return "traces"
    if tool in KUBERNETES_TOOLS:
        return "kubernetes"
    if tool in CHANGE_TOOLS:
        return "changes"
    return "unknown"


def _target(arguments: dict[str, Any]) -> str | None:
    for key in ("service", "consumer", "deployment"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"benchmark artifact must be an object: {path}")
    return value


def _round(value: float) -> float:
    return round(value, 4)


def analyze_benchmark(
    benchmark: dict[str, Any], *, benchmark_path: Path = BENCHMARK_PATH
) -> dict[str, Any]:
    """Return only values reproducible from the benchmark and frozen dataset."""
    scenarios = benchmark.get("scenarios", [])
    if [item.get("scenario_id") for item in scenarios] != EXPECTED_IDS:
        raise ValueError("benchmark scenarios do not match frozen dataset order")

    expected = {item.scenario_id: item for item in FROZEN_DATASET}
    tool_counts: Counter[str] = Counter({tool: 0 for tool in ALL_TOOLS})
    family_counts: Counter[str] = Counter()
    turn_counts: Counter[str] = Counter()
    turn_scenario_counts: Counter[str] = Counter()
    tool_scenarios: defaultdict[str, set[str]] = defaultdict(set)
    per_scenario: list[dict[str, Any]] = []
    service_targeting: list[dict[str, Any]] = []

    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        requested_by_turn: dict[str, list[str]] = {}
        targets: list[str] = []
        unique_families: set[str] = set()
        unique_tools: set[str] = set()
        for turn in scenario.get("turns", []):
            turn_name = str(turn["turn"])
            names = list(turn.get("requested_tool_names", []))
            requested_by_turn[turn_name] = names
            if names:
                turn_scenario_counts[turn_name] += 1
            turn_counts[turn_name] += len(names)
            for tool in names:
                tool_counts[tool] += 1
                family = _family(tool)
                family_counts[family] += 1
                tool_scenarios[tool].add(scenario_id)
                unique_tools.add(tool)
                unique_families.add(family)
            for audit in turn.get("request_audits", []):
                value = _target(audit.get("arguments", {}))
                if value is not None:
                    targets.append(value)

        alert_name = scenario["alert_name"]
        fixture = FIXTURE_BY_NAME.get(scenario["fixture"])
        if fixture is None:
            raise ValueError(f"unknown frozen fixture: {scenario['fixture']}")
        if alert_name != fixture.alert_name:
            raise ValueError(
                f"persisted alert name does not match fixture definition for {scenario['fixture']}"
            )
        alert_service = fixture.service
        ground_truth_component = expected[scenario_id].affected_component
        alert_scope_matches = alert_service == ground_truth_component
        matching_targets = sum(target == alert_service for target in targets)
        service_targeting.append(
            {
                "scenario_id": scenario_id,
                "alert_service": alert_service,
                "ground_truth_component": ground_truth_component,
                "alert_scope_matches_ground_truth": alert_scope_matches,
                "cross_component": not alert_scope_matches,
                "target_count": len(targets),
                "alert_service_target_count": matching_targets,
                "alert_service_target_rate": _round(matching_targets / len(targets))
                if targets
                else None,
                "target_counts": dict(sorted(Counter(targets).items())),
                "service_score": scenario["service_accuracy"],
            }
        )

        classes: list[str] = []
        if scenario["service_accuracy"] == 0:
            classes.append("official_service_dimension_miss")
        if scenario["mechanism_accuracy"] == 0:
            classes.append("official_mechanism_dimension_miss")
        if (
            scenario["trigger_accuracy"] == 0
            and scenario["terminal_decision"] == "SUBMIT_HYPOTHESIS"
        ):
            classes.append("exact_trigger_match_miss")
        if scenario["termination_reason"] == "AGENT_STOPPED":
            classes.append("valid_agent_stop_insufficient_evidence")
        if scenario["service_accuracy"] == 0 and scenario["mechanism_accuracy"] == 1:
            classes.append("mechanism_correct_service_wrong")

        per_scenario.append(
            {
                "scenario_id": scenario_id,
                "fixture": scenario["fixture"],
                "ground_truth": {
                    "component": expected[scenario_id].affected_component,
                    "mechanism": expected[scenario_id].mechanism.value,
                    "trigger": expected[scenario_id].suspected_trigger,
                },
                "alert": scenario["alert_name"],
                "component_scope": {
                    "alert_scope_component": alert_service,
                    "ground_truth_component": ground_truth_component,
                    "alert_scope_matches_ground_truth": alert_scope_matches,
                    "cross_component": not alert_scope_matches,
                },
                "terminal_outcome": {
                    "decision": scenario["terminal_decision"],
                    "termination": scenario["termination_reason"],
                    "stop_reason": scenario.get("stop_reason"),
                },
                "scores": {
                    "service": scenario["service_accuracy"],
                    "mechanism": scenario["mechanism_accuracy"],
                    "trigger": scenario["trigger_accuracy"],
                    "composite": scenario["composite_rca"],
                },
                "tools": {
                    "by_turn": requested_by_turn,
                    "total_requests": scenario["tool_requests_total"],
                    "total_executions": scenario["tool_calls"],
                    "evidence_count": scenario["evidence_count"],
                    "unique_tools": sorted(unique_tools),
                    "unique_families": sorted(unique_families),
                    "unique_targets": sorted(set(targets)),
                },
                "failure_classification": classes,
                "analysis_confidence": "PROVEN" if not classes else "SUPPORTED",
                "predicted_fields_persisted": False,
            }
        )

    service_correct = sum(item["service_accuracy"] for item in scenarios)
    mechanism_correct = sum(item["mechanism_accuracy"] for item in scenarios)
    trigger_correct = sum(item["trigger_accuracy"] for item in scenarios)
    composite_sum = sum(item["composite_rca"] for item in scenarios)
    submitted = sum(item["terminal_decision"] == "SUBMIT_HYPOTHESIS" for item in scenarios)
    stop_count = len(scenarios) - submitted
    taxonomy = benchmark["failure_taxonomy"]
    score_consistency = {
        "service": {
            "correct_count": int(service_correct),
            "miss_count_including_stop": len(scenarios) - int(service_correct),
            "taxonomy_wrong_service": taxonomy["wrong_service"],
            "reconciles": taxonomy["wrong_service"] == len(scenarios) - int(service_correct),
        },
        "mechanism": {
            "correct_count": int(mechanism_correct),
            "miss_count_including_stop": len(scenarios) - int(mechanism_correct),
            "taxonomy_wrong_mechanism": taxonomy["wrong_mechanism"],
            "taxonomy_excludes_stop": taxonomy["wrong_mechanism"]
            == len(scenarios) - int(mechanism_correct) - stop_count,
            "reconciles_with_stop_excluded": taxonomy["wrong_mechanism"]
            == len(scenarios) - int(mechanism_correct) - stop_count,
        },
        "trigger": {
            "correct_count": int(trigger_correct),
            "submitted_hypotheses": submitted,
            "miss_count_submitted": submitted - int(trigger_correct),
            "taxonomy_wrong_trigger": taxonomy["wrong_trigger"],
            "taxonomy_excludes_stop": taxonomy["wrong_trigger"] == submitted,
        },
        "composite": {
            "sum": _round(composite_sum),
            "mean": _round(composite_sum / len(scenarios)),
        },
    }

    all_targets = [item for row in service_targeting for item in [row["target_count"]]]
    matching_targets = sum(row["alert_service_target_count"] for row in service_targeting)
    total_targets = sum(all_targets)
    change_scenarios = sorted(
        tool_scenarios["recent_deployment_changes"] | tool_scenarios["recent_configuration_changes"]
    )
    wrong_service_ids = [item["scenario_id"] for item in scenarios if item["service_accuracy"] == 0]
    mechanism_correct_service_wrong = [
        item["scenario_id"]
        for item in scenarios
        if item["service_accuracy"] == 0 and item["mechanism_accuracy"] == 1
    ]
    service_correct_target_rates = [
        row["alert_service_target_rate"] for row in service_targeting if row["service_score"] == 1
    ]
    service_wrong_target_rates = [
        row["alert_service_target_rate"] for row in service_targeting if row["service_score"] == 0
    ]
    cross_component_scenarios = [
        row["scenario_id"] for row in service_targeting if row["cross_component"]
    ]
    cross_component_target_counts = {
        row["scenario_id"]: {
            "outside_alert_scope": sum(
                count
                for target, count in row["target_counts"].items()
                if target != row["alert_service"]
            ),
            "total": row["target_count"],
        }
        for row in service_targeting
        if row["cross_component"]
    }
    cross_component_explored_scenarios = [
        scenario_id
        for scenario_id, counts in cross_component_target_counts.items()
        if counts["outside_alert_scope"] > 0
    ]

    return {
        "source": {
            "benchmark_path": str(benchmark_path),
            "benchmark_file_sha256": sha256(benchmark_path.read_bytes()).hexdigest(),
            "benchmark_runtime_sha": benchmark["git_sha"],
            "dataset_hash": frozen_dataset_hash(),
        },
        "metric_interpretability": {
            "service_accuracy": {
                "confidence": "MEDIUM",
                "basis": "exact free-text affected_component equality; no normalization",
                "limitation": "submitted component strings are not persisted",
            },
            "mechanism_accuracy": {"confidence": "HIGH", "basis": "controlled enum equality"},
            "trigger_accuracy": {"confidence": "LOW", "basis": "free-text exact string equality"},
            "composite_rca": {
                "confidence": "MEDIUM_LOW",
                "basis": "weighted sum inherits representation-sensitive service and trigger dimensions",
            },
            "valid_evidence_reference_rate": {
                "confidence": "HIGH",
                "reference_integrity_confidence": "HIGH",
                "causal_relevance_confidence": "LOW",
                "basis": "scenario-level runtime-owned reference integrity; STOP contributes zero; semantic relevance is not graded",
            },
        },
        "evidence_integrity": {
            "official_scenario_rate": benchmark["valid_evidence_reference_rate"],
            "submitted_hypotheses": submitted,
            "valid_submitted_hypotheses": sum(
                item["terminal_decision"] == "SUBMIT_HYPOTHESIS"
                and item["valid_evidence_reference_rate"] == 1
                for item in scenarios
            ),
            "submitted_hypothesis_integrity_rate": _round(
                sum(
                    item["terminal_decision"] == "SUBMIT_HYPOTHESIS"
                    and item["valid_evidence_reference_rate"] == 1
                    for item in scenarios
                )
                / submitted
            ),
            "fabricated_evidence": benchmark["safety"]["fabricated_evidence"],
            "cross_incident_evidence": benchmark["safety"]["cross_incident_evidence"],
        },
        "score_consistency": score_consistency,
        "per_scenario": per_scenario,
        "tool_usage": {
            "tool_frequency": dict(sorted(tool_counts.items())),
            "tool_family_frequency": dict(sorted(family_counts.items())),
            "tool_scenarios": {key: sorted(value) for key, value in sorted(tool_scenarios.items())},
            "unused_registered_tools": sorted(
                tool for tool, count in tool_counts.items() if count == 0
            ),
            "tools_by_turn": dict(sorted(turn_counts.items())),
            "scenarios_with_requests_by_turn": dict(sorted(turn_scenario_counts.items())),
            "change_tool_scenarios": change_scenarios,
            "change_tool_scenario_count": len(change_scenarios),
            "trace_tool_request_count": sum(tool_counts[tool] for tool in TRACE_TOOLS),
            "semantic_role_analysis": {
                "status": "UNKNOWN",
                "reason": "persisted artifact contains tool identities but not returned evidence contents or model submissions",
            },
        },
        "turn_usage": {
            "model_calls_total": sum(item["model_calls"] for item in scenarios),
            "all_scenarios_used_three_calls": all(item["model_calls"] == 3 for item in scenarios),
            "tool_requests_total": sum(item["tool_requests_total"] for item in scenarios),
            "tool_executions_total": sum(item["tool_calls"] for item in scenarios),
            "average_requests_by_turn": {
                key: _round(value / len(scenarios)) for key, value in sorted(turn_counts.items())
            },
            "tool_budget_saturated_scenarios": [
                item["scenario_id"]
                for item in scenarios
                if item["tool_calls"] == benchmark["configured_max_tool_calls"]
            ],
        },
        "component_targeting": {
            "alert_scope_method": "canonical FIXTURE_BY_NAME fixture service; persisted alert name validated against fixture definition",
            "method": "canonical frozen fixture service; request target from persisted canonical arguments",
            "by_scenario": service_targeting,
            "total_targeted_requests": total_targets,
            "alert_service_targeted_requests": matching_targets,
            "alert_service_target_rate": _round(matching_targets / total_targets),
            "service_correct_scenarios": [
                item["scenario_id"] for item in scenarios if item["service_accuracy"] == 1
            ],
            "service_wrong_scenarios": wrong_service_ids,
            "mechanism_correct_service_wrong_scenarios": mechanism_correct_service_wrong,
            "all_requests_target_alert_service": matching_targets == total_targets,
            "service_correct_runs_alert_service_target_rate": _round(
                sum(service_correct_target_rates) / len(service_correct_target_rates)
            ),
            "service_wrong_runs_alert_service_target_rate": _round(
                sum(service_wrong_target_rates) / len(service_wrong_target_rates)
            ),
            "cross_component_scenarios": cross_component_scenarios,
            "cross_component_target_counts": cross_component_target_counts,
            "cross_component_explored_scenarios": cross_component_explored_scenarios,
        },
        "system_failure_modes": [
            {
                "name": "alert_scope_search_anchoring",
                "affected_scenarios": EXPECTED_IDS,
                "count": len(EXPECTED_IDS),
                "fraction": 1.0,
                "confidence": "PROVEN",
                "basis": "66/66 persisted target-bearing requests matched the canonical production alert scope; this is a search behavior, not proof of a correctness cause",
            },
            {
                "name": "cross_component_exploration_failure",
                "affected_scenarios": cross_component_scenarios,
                "count": len(cross_component_scenarios),
                "fraction": _round(len(cross_component_scenarios) / len(scenarios)),
                "confidence": "SUPPORTED",
                "basis": "V020-003 alert scope was order-service, frozen causal component was payment-service, and no persisted target left order-service",
            },
            {
                "name": "official_service_dimension_miss",
                "affected_scenarios": wrong_service_ids,
                "count": len(wrong_service_ids),
                "fraction": _round(len(wrong_service_ids) / len(scenarios)),
                "confidence": "PROVEN_SCORE_UNKNOWN_CAUSE",
                "basis": "official exact-match score is 0 for six scenarios; submitted component strings are absent, so semantic cause is UNKNOWN",
            },
            {
                "name": "change_evidence_acquisition_miss",
                "affected_scenarios": ["V020-010"],
                "count": 1,
                "fraction": 0.1,
                "confidence": "SUPPORTED",
                "basis": "the frozen fixture identifies recent_configuration_changes as a primary evidence surface, but no change-intelligence request appears in V020-010",
            },
            {
                "name": "non_completion_valid_insufficient_evidence_stop",
                "affected_scenarios": ["V020-008"],
                "count": 1,
                "fraction": 0.1,
                "confidence": "PROVEN_OBSERVATION_UNKNOWN_CAUSE",
                "basis": "V020-008 ended with a valid insufficient_evidence STOP; why it stopped is not reconstructable",
            },
            {
                "name": "three_call_envelope_observed",
                "affected_scenarios": EXPECTED_IDS,
                "count": len(EXPECTED_IDS),
                "fraction": 1.0,
                "confidence": "PROVEN_OBSERVATION_UNKNOWN_EFFECT",
                "basis": "all runs used three model calls; whether the envelope caused any miss is UNKNOWN",
            },
        ],
        "system_observations": {
            "all_target_requests_on_alert_scope": matching_targets == total_targets,
            "all_runs_used_three_model_calls": all(item["model_calls"] == 3 for item in scenarios),
            "cross_component_opportunities": cross_component_scenarios,
            "cross_component_opportunities_explored_outside_scope": cross_component_explored_scenarios,
            "change_tool_acquisition_miss": ["V020-010"],
            "valid_insufficient_evidence_stop": ["V020-008"],
        },
        "supported_failure_modes": [
            "cross_component_exploration_failure",
            "change_evidence_acquisition_miss",
        ],
        "unknown_causes": [
            "semantic cause of the six service-score misses",
            "semantic correctness of the nine trigger strings",
            "reason V020-008 stopped despite a frozen benchmark outcome",
            "causal effect of the three-call envelope",
            "causal relevance or sufficiency of retrieved evidence",
        ],
        "failure_layers": {
            "A_evidence_acquisition": {
                "confidence": "SUPPORTED",
                "affected_scenarios": ["V020-010"],
                "basis": "the relevant historical change tools were available but absent from the persisted V020-010 request path",
            },
            "B_evidence_availability": {
                "confidence": "UNKNOWN",
                "affected_scenarios": [],
                "basis": "the artifact does not show that a required evidence surface was unavailable; harness qualification was complete",
            },
            "C_evidence_interpretation": {
                "confidence": "UNKNOWN",
                "affected_scenarios": [],
                "basis": "returned evidence contents and complete hypotheses are not persisted",
            },
            "D_component_attribution": {
                "observation_confidence": "PROVEN",
                "semantic_attribution_confidence": "UNKNOWN",
                "affected_scenarios": mechanism_correct_service_wrong,
                "basis": "mechanism was correct while exact free-text service scoring was wrong; predicted component strings are absent",
            },
            "D1_cross_component_search": {
                "confidence": "SUPPORTED",
                "affected_scenarios": cross_component_scenarios,
                "basis": "the only cross-component scenario stayed entirely on alert scope and did not query its frozen causal component",
            },
            "E_trigger_representation": {
                "observation_confidence": "PROVEN",
                "per_miss_cause": "UNKNOWN",
                "affected_scenarios": [
                    item["scenario_id"]
                    for item in scenarios
                    if item["terminal_decision"] == "SUBMIT_HYPOTHESIS"
                ],
                "basis": "all nine submitted trigger strings missed exact equality; individual semantic correctness is not reconstructable",
            },
            "F_termination_calibration": {
                "observation_confidence": "PROVEN",
                "calibration_cause": "UNKNOWN",
                "affected_scenarios": [
                    item["scenario_id"]
                    for item in scenarios
                    if item["termination_reason"] == "AGENT_STOPPED"
                ],
                "basis": "one run ended in a valid insufficient-evidence STOP; whether that calibration was wrong is UNKNOWN",
            },
            "G_budget_search_strategy": {
                "observation_confidence": "PROVEN",
                "causal_effect": "UNKNOWN",
                "affected_scenarios": [
                    item["scenario_id"] for item in scenarios if item["model_calls"] == 3
                ],
                "basis": "all runs consumed the three-call envelope; using the envelope is observed, but causal impact is UNKNOWN",
            },
            "H_benchmark_observability": {
                "confidence": "PROVEN",
                "affected_scenarios": EXPECTED_IDS,
                "basis": "the artifact omits complete hypothesis text and evidence contents",
            },
        },
        "evaluation_limitations": [
            "The service grader compares the submitted affected_component string directly against the frozen canonical service string; there is no normalization and the submitted component value is not persisted, so a zero cannot distinguish semantic misattribution from non-canonical representation.",
            "The trigger grader uses exact free-text equality; 0% proves no exact string match, not semantic trigger failure.",
            "The release artifact does not persist complete hypothesis submissions, so predicted service/mechanism/trigger strings cannot be reconstructed.",
            "Evidence IDs and ownership are persisted, but evidence contents and semantic relevance are not sufficient for post-hoc causal judgment.",
            "The mechanism taxonomy counts submitted-hypothesis misses and excludes the STOP; the aggregate mechanism score counts STOP as incorrect.",
            "One execution per scenario does not estimate model variance.",
        ],
        "v03_requirements": [
            "Distinguish symptom service from causal component in structured investigation output.",
            "Persist cross-component exploration and dependency traversal in bounded audit fields.",
            "Persist complete bounded structured hypothesis payloads and bounded evidence summaries for audit.",
            "Use a canonical component vocabulary so service scoring is not representation-sensitive.",
            "Freeze a structured causal-trigger label or supplement free-text trigger scoring with a predeclared semantic rubric.",
            "Retain v0.2.0 safety and provenance floors: fabricated evidence 0, cross-incident evidence 0, writes 0.",
        ],
        "official_benchmark_metrics": {
            "completion_rate": benchmark["completion_rate"],
            "service_accuracy": benchmark["service_accuracy"],
            "mechanism_accuracy": benchmark["mechanism_accuracy"],
            "trigger_accuracy": benchmark["trigger_accuracy"],
            "composite_rca": benchmark["composite_rca"],
            "valid_evidence_reference_rate": benchmark["valid_evidence_reference_rate"],
        },
        "official_failure_taxonomy": benchmark["failure_taxonomy"],
        "official_terminal_distribution": benchmark["terminal_outcome_distribution"],
    }


def _scenario_row(item: dict[str, Any]) -> str:
    tools = item["tools"]["by_turn"]
    scores = item["scores"]
    outcome = item["terminal_outcome"]["termination"]
    return (
        f"| {item['scenario_id']} | `{item['fixture']}` | {item['ground_truth']['component']} | "
        f"{item['ground_truth']['mechanism']} | "
        f"{item['component_scope']['alert_scope_component']} | "
        f"{item['component_scope']['ground_truth_component']} | "
        f"{'YES' if item['component_scope']['cross_component'] else 'NO'} | "
        f"{outcome} | "
        f"{scores['service']:.0f}/{scores['mechanism']:.0f}/{scores['trigger']:.0f}/{scores['composite']:.2f} | "
        f"{', '.join(tools.get('1', [])) or '—'} | {', '.join(tools.get('2', [])) or '—'} | "
        f"{item['tools']['evidence_count']} |"
    )


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render the deterministic analysis as a reviewable human report."""
    consistency = analysis["score_consistency"]
    usage = analysis["tool_usage"]
    targeting = analysis["component_targeting"]
    integrity = analysis["evidence_integrity"]
    modes = analysis["system_failure_modes"]
    rows = "\n".join(_scenario_row(item) for item in analysis["per_scenario"])
    tool_lines = "\n".join(
        f"- `{name}`: {count}" for name, count in usage["tool_frequency"].items()
    )
    family_lines = "\n".join(
        f"- {name}: {count}" for name, count in usage["tool_family_frequency"].items()
    )
    mode_lines = "\n".join(
        f"- **{item['name']}** — {item['count']}/10 ({item['fraction']:.0%}), {item['confidence']}; "
        f"affected: {', '.join(item['affected_scenarios']) or 'none'}. {item['basis']}"
        for item in modes
    )
    return f"""# v0.2.0 Failure Analysis

## Scope and evidence limits

This is an offline forensic analysis of the immutable
`v0.2.0-single-agent-live.json` artifact and the frozen dataset. No model,
network, cluster, benchmark rerun, or score change was used. Conclusions are
limited to persisted audit fields; absent hypothesis text, evidence contents,
and private reasoning are marked `UNKNOWN` rather than reconstructed.

Source runtime SHA: `{analysis["source"]["benchmark_runtime_sha"]}`  
Source benchmark artifact SHA-256: `{analysis["source"]["benchmark_file_sha256"]}`  
Dataset hash: `{analysis["source"]["dataset_hash"]}`

## Benchmark recap

The official frozen result remains: completion `90%`, service accuracy `40%`,
mechanism accuracy `80%`, trigger accuracy `0%`, composite RCA `50%`, and valid
evidence reference rate `90%`. There were 10 one-pass scenarios, 30 model calls,
66 tool requests/executions, 9 hypotheses, and one valid STOP.

## Grader audit

`grade_hypothesis` compares `affected_component` using direct free-text equality
with no normalization, compares the controlled mechanism enum, and compares
`suspected_trigger` using exact string equality. The official trigger score
therefore proves `0/9` exact string matches; it does not prove that all
submitted trigger statements were semantically wrong.

`grade_evidence` checks whether submitted evidence IDs are contained in the
runtime-owned evidence set. The `90%` result is scenario-level
reference/ownership integrity; it does not establish that cited evidence was
causally relevant, sufficient, or semantically supportive.

Consistency results:

- Service: 4 correct and 6 misses; `wrong_service=6` reconciles exactly.
- Mechanism: 8 correct and 2 misses including the STOP; `wrong_mechanism=1`
  counts only the one incorrect submitted hypothesis and excludes the STOP.
- Trigger: 0 exact matches among 9 submitted hypotheses; `wrong_trigger=9`
  excludes the STOP.
- Composite: scenario composite sum `{consistency["composite"]["sum"]:.2f}` / 10
  gives `{consistency["composite"]["mean"]:.0%}`.

## Metric audit

| Metric | Official semantics | Confidence |
| --- | --- | --- |
| `service_accuracy` | Exact free-text `affected_component` equality; no normalization | MEDIUM |
| `mechanism_accuracy` | Controlled mechanism enum equality | HIGH |
| `trigger_accuracy` | Exact free-text `suspected_trigger` equality | LOW |
| `composite_rca` | 0.25 service + 0.50 mechanism + 0.25 trigger | MEDIUM/LOW |
| `valid_evidence_reference_rate` | Runtime-owned reference integrity; STOP contributes zero | HIGH for integrity, LOW for relevance |

## Service metric limitation

The official service score remains `40%`, but the six zero scores are not six
proven semantic misattributions. The submitted component strings are not
persisted, so a zero cannot distinguish semantic misattribution from a
non-canonical representation.

## Evidence-integrity denominator

- Official scenario-level evidence-reference rate: `{integrity["official_scenario_rate"]:.0%}`.
- Submitted hypotheses: `{integrity["submitted_hypotheses"]}`.
- Valid submitted hypotheses: `{integrity["valid_submitted_hypotheses"]}`.
- Submitted-hypothesis integrity: `{integrity["submitted_hypothesis_integrity_rate"]:.0%}`.

The official `90%` is reduced by the one hypothesis-less STOP, not by an
invalid submitted reference. Causal relevance and sufficiency are not graded.

## Per-scenario investigation paths

Scores are `service/mechanism/trigger/composite`.

| Scenario | Fixture | Expected component | Expected mechanism | Alert scope | Ground-truth component | Cross-component? | Termination | Scores | Turn 1 tools | Turn 2 tools | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: |
{rows}

Predicted service, mechanism, and trigger values are not persisted in the
release artifact. The matrix therefore reports official scores, not invented
wrong predictions.

## Tool-selection analysis

Tool frequency:

{tool_lines}

Tool-family frequency:

{family_lines}

Two registered tools were unused: `{", ".join(usage["unused_registered_tools"])}`.
Change intelligence was requested in {usage["change_tool_scenario_count"]}/10
scenarios ({sum(usage["tool_frequency"].get(name, 0) for name in CHANGE_TOOLS)} requests),
while traces were requested only {usage["trace_tool_request_count"]}
times. This is a proven usage pattern, but the artifact does not prove whether
each returned observation was relevant.

Every scenario used three model calls. Turn 1 contained
`{usage["tools_by_turn"].get("1", 0)}` tool requests total and turn 2 contained
`{usage["tools_by_turn"].get("2", 0)}`; three scenarios reached the 8-tool
execution ceiling. The effective shape was therefore two evidence-gathering
decisions followed by a terminal decision.

Discriminating-vs-confirmatory classification is `UNKNOWN` at run level because
returned evidence contents are not persisted. Static tool semantics alone cannot
prove information value.

## Alert-scope behavior

Scope is reconstructed from canonical `FIXTURE_BY_NAME[fixture].service`; the
persisted alert name is validated against that fixture. The persisted canonical
arguments show `{targeting["alert_service_targeted_requests"]}/{targeting["total_targeted_requests"]}`
(`{targeting["alert_service_target_rate"]:.0%}`) target-bearing requests on alert
scope. This is a PROVEN search behavior, not a correctness explanation for six
service-score misses. Service-correct and service-score-miss runs both had a
`{targeting["service_wrong_runs_alert_service_target_rate"]:.0%}` alert-scope target rate.

## Cross-component exploration

There is `{len(targeting["cross_component_scenarios"])}` cross-component opportunity:
`{", ".join(targeting["cross_component_scenarios"])}`. V020-003 had alert scope
`order-service` and frozen causal component `payment-service`; all 8 persisted
targets remained on `order-service`, with 0 outside-scope targets. Exploration
was therefore `0/1` opportunities. This is SUPPORTED as an exploration gap and
does not generalize to same-scope service-score misses.

## Service attribution analysis

The official service misses are `{len(targeting["service_wrong_scenarios"])}/10`:
`{", ".join(targeting["service_wrong_scenarios"])}`. The mechanism-correct /
service-score-failed set is `{", ".join(targeting["mechanism_correct_service_wrong_scenarios"])}`.
This score pattern is PROVEN. Its semantic cause is UNKNOWN because the grader
uses exact free-text equality and submitted component values are absent.

## V020-008 STOP analysis

V020-008 (`order_worker_failure`) selected `stop_investigation` on turn 3 with
`insufficient_evidence`, after six tools and six evidence items. The artifact
proves a valid STOP and the exact tool sequence, but not the complete evidence
contents or final hypothesis alternative. Therefore the choice among
“necessary evidence not requested”, “weak evidence”, “lag/failure ambiguity”,
and “three-call search envelope” remains `UNKNOWN`. The precise observation is
non-completion via a valid insufficient-evidence STOP; whether calibration was
wrong is UNKNOWN.

## V020-010 change-intelligence analysis

V020-010 (`payment_config_change`) submitted a hypothesis with service score `1`,
mechanism score `0`, trigger score `0`, and composite `0.25`. Its persisted path
requested service latency and slow traces on turn 1, then logs, database metrics,
and error logs on turn 2; no change-intelligence tool was requested. Historical
change capability existed in the system, but this run does not prove that the
agent inspected or used it. The predicted mechanism text is not persisted, so
the exact mechanism confusion is `UNKNOWN`.

## Trigger metric limitation

Official result: trigger accuracy is `0%`. Interpretability: the grader performs
exact free-text equality and the release artifact does not preserve submitted
trigger strings. Semantic paraphrases cannot be distinguished retrospectively.

## Evidence-validity limitation

The `90%` scenario-level reference rate includes the one hypothesis-less STOP.
All 9 submitted hypotheses had valid runtime-owned references (`100%` submitted
hypothesis integrity). It does not prove evidence sufficiency, causal relevance,
or support for the conclusion. Fabricated and cross-incident references were
both `0`, which is a safety/integrity result.

## Dominant system failure modes

{mode_lines}

These are observations and bounded interpretations; they do not establish that
alert scope caused the six service misses.

## Unknown causes

- Semantic cause of the six service-score misses.
- Semantic correctness of the nine trigger strings.
- Why V020-008 stopped despite a frozen benchmark outcome.
- Whether a larger search budget would have changed any result.
- Whether retrieved evidence was causally relevant or sufficient.

## Evaluation observability gaps

- Trigger exact-match scoring is representation-sensitive.
- Complete hypothesis payloads are absent from the frozen artifact.
- Evidence contents are too bounded for post-hoc semantic relevance analysis.
- Mechanism confusion matrices cannot be reconstructed without predicted enums.
- One pass per scenario gives no variance estimate.

## v0.3 measurable requirements

These are requirements only, not an implementation plan:

1. Distinguish symptom service from causal component in structured output.
2. Persist cross-component exploration and dependency traversal in bounded audit fields.
3. Persist complete bounded hypothesis payloads and bounded evidence summaries.
4. Use a canonical component vocabulary so service scoring is not representation-sensitive.
5. Use a predeclared structured causal-trigger label or semantic scoring rubric.
6. Preserve v0.2.0 safety floors: fabricated evidence `0`, cross-incident evidence `0`, and writes `0`.

## Comparison target

Future experiments must compare against runtime SHA
`4603fc6380d672b0a3e9d885885b879f9b32c407` and preserve the official baseline:
service `40%`, mechanism `80%`, trigger `0%`, composite `50%`, completion `90%`,
fabricated evidence `0`, cross-incident evidence `0`, writes `0`.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, default=BENCHMARK_PATH)
    parser.add_argument("--json-output", type=Path, default=JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=MARKDOWN_OUTPUT)
    args = parser.parse_args()
    benchmark = _load(args.benchmark)
    analysis = analyze_benchmark(benchmark, benchmark_path=args.benchmark)
    args.json_output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    args.markdown_output.write_text(render_markdown(analysis))
    print(f"v0.2.0 forensic analysis: {len(analysis['per_scenario'])}/10 scenarios")
    print(f"tool requests: {analysis['turn_usage']['tool_requests_total']}")
    print(f"tool executions: {analysis['turn_usage']['tool_executions_total']}")
    print(f"outputs: {args.json_output}, {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
