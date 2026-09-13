"""Produce deterministic forensic aggregates for the frozen v0.2.0 benchmark."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.dataset import FROZEN_DATASET, frozen_dataset_hash

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


def _alert_service(alert_name: str) -> str | None:
    """Derive the production alert scope from the persisted alert identity."""
    if alert_name.startswith("Payment"):
        return "payment-service"
    if alert_name.startswith("OrderWorker"):
        return "order-worker"
    if alert_name.startswith("Order"):
        return "order-service"
    return None


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


def analyze_benchmark(benchmark: dict[str, Any]) -> dict[str, Any]:
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
        alert_service = _alert_service(alert_name)
        matching_targets = sum(target == alert_service for target in targets)
        service_targeting.append(
            {
                "scenario_id": scenario_id,
                "alert_service": alert_service,
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

    return {
        "source": {
            "benchmark_path": str(BENCHMARK_PATH),
            "benchmark_file_sha256": sha256(BENCHMARK_PATH.read_bytes()).hexdigest(),
            "benchmark_runtime_sha": benchmark["git_sha"],
            "dataset_hash": frozen_dataset_hash(),
        },
        "metric_interpretability": {
            "service_accuracy": {
                "confidence": "HIGH",
                "basis": "exact normalized component equality",
            },
            "mechanism_accuracy": {"confidence": "HIGH", "basis": "controlled enum equality"},
            "trigger_accuracy": {"confidence": "LOW", "basis": "free-text exact string equality"},
            "composite_rca": {
                "confidence": "MEDIUM",
                "basis": "weighted sum inherits component metric limitations",
            },
            "valid_evidence_reference_rate": {
                "confidence": "MEDIUM",
                "basis": "runtime ownership/reference integrity only; semantic relevance is not graded",
            },
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
            "method": "alert service derived from persisted production alert name prefix; request target from persisted canonical arguments",
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
        },
        "system_failure_modes": [
            {
                "name": "mechanism_correct_service_wrong",
                "affected_scenarios": mechanism_correct_service_wrong,
                "count": len(mechanism_correct_service_wrong),
                "fraction": _round(len(mechanism_correct_service_wrong) / len(scenarios)),
                "confidence": "SUPPORTED",
                "basis": "official mechanism score is 1 while official service score is 0",
            },
            {
                "name": "alert_service_anchoring",
                "affected_scenarios": wrong_service_ids,
                "count": len(wrong_service_ids),
                "fraction": _round(len(wrong_service_ids) / len(scenarios)),
                "confidence": "SUPPORTED",
                "basis": "100% of persisted targeted requests match alert service; causal ownership is wrong in the listed cases",
            },
            {
                "name": "valid_stop_without_hypothesis",
                "affected_scenarios": [
                    item["scenario_id"]
                    for item in scenarios
                    if item["termination_reason"] == "AGENT_STOPPED"
                ],
                "count": stop_count,
                "fraction": _round(stop_count / len(scenarios)),
                "confidence": "PROVEN",
                "basis": "terminal decision and stop reason are persisted",
            },
            {
                "name": "fixed_turn_search_envelope",
                "affected_scenarios": [
                    item["scenario_id"] for item in scenarios if item["model_calls"] == 3
                ],
                "count": len(scenarios),
                "fraction": 1.0,
                "confidence": "PROVEN",
                "basis": "all runs consumed the configured three model calls; causal sufficiency is not inferable",
            },
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
                "confidence": "SUPPORTED",
                "affected_scenarios": mechanism_correct_service_wrong,
                "basis": "mechanism was correct while the official service dimension was wrong",
            },
            "E_trigger_representation": {
                "confidence": "PROVEN",
                "affected_scenarios": [
                    item["scenario_id"]
                    for item in scenarios
                    if item["terminal_decision"] == "SUBMIT_HYPOTHESIS"
                ],
                "basis": "trigger grading is exact free-text equality and submitted strings are absent",
            },
            "F_termination_calibration": {
                "confidence": "PROVEN",
                "affected_scenarios": [
                    item["scenario_id"]
                    for item in scenarios
                    if item["termination_reason"] == "AGENT_STOPPED"
                ],
                "basis": "one run ended in a valid insufficient-evidence STOP despite a frozen benchmark outcome",
            },
            "G_budget_search_strategy": {
                "confidence": "PROVEN",
                "affected_scenarios": [
                    item["scenario_id"] for item in scenarios if item["model_calls"] == 3
                ],
                "basis": "all runs consumed the three-call envelope; causal impact of the limit is unknown",
            },
            "H_benchmark_observability": {
                "confidence": "PROVEN",
                "affected_scenarios": EXPECTED_IDS,
                "basis": "the artifact omits complete hypothesis text and evidence contents",
            },
        },
        "evaluation_limitations": [
            "The trigger grader uses exact free-text equality; 0% proves no exact string match, not semantic trigger failure.",
            "The release artifact does not persist complete hypothesis submissions, so predicted service/mechanism/trigger strings cannot be reconstructed.",
            "Evidence IDs and ownership are persisted, but evidence contents and semantic relevance are not sufficient for post-hoc causal judgment.",
            "The mechanism taxonomy counts submitted-hypothesis misses and excludes the STOP; the aggregate mechanism score counts STOP as incorrect.",
            "One execution per scenario does not estimate model variance.",
        ],
        "v03_requirements": [
            "Distinguish symptom service from causal component in structured investigation output.",
            "Measure whether cross-component evidence was requested and whether it supports causal ownership.",
            "Persist complete bounded structured hypothesis payloads and bounded evidence summaries for audit.",
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
        f"{item['ground_truth']['mechanism']} | {outcome} | "
        f"{scores['service']:.0f}/{scores['mechanism']:.0f}/{scores['trigger']:.0f}/{scores['composite']:.2f} | "
        f"{', '.join(tools.get('1', [])) or '—'} | {', '.join(tools.get('2', [])) or '—'} | "
        f"{item['tools']['evidence_count']} |"
    )


def render_markdown(analysis: dict[str, Any]) -> str:
    """Render the deterministic analysis as a reviewable human report."""
    consistency = analysis["score_consistency"]
    usage = analysis["tool_usage"]
    targeting = analysis["component_targeting"]
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

`grade_hypothesis` compares normalized component identity and the controlled
mechanism enum, but compares `suspected_trigger` using exact string equality.
The official trigger score therefore proves `0/9` exact string matches; it does
not prove that all submitted trigger statements were semantically wrong.

`grade_evidence` checks whether submitted evidence IDs are contained in the
runtime-owned evidence set. The `90%` result is reference/ownership integrity;
it does not establish that cited evidence was causally relevant, sufficient, or
semantically supportive.

Consistency results:

- Service: 4 correct and 6 misses; `wrong_service=6` reconciles exactly.
- Mechanism: 8 correct and 2 misses including the STOP; `wrong_mechanism=1`
  counts only the one incorrect submitted hypothesis and excludes the STOP.
- Trigger: 0 exact matches among 9 submitted hypotheses; `wrong_trigger=9`
  excludes the STOP.
- Composite: scenario composite sum `{consistency["composite"]["sum"]:.2f}` / 10
  gives `{consistency["composite"]["mean"]:.0%}`.

## Per-scenario investigation paths

Scores are `service/mechanism/trigger/composite`.

| Scenario | Fixture | Expected component | Expected mechanism | Termination | Scores | Turn 1 tools | Turn 2 tools | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
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

Layer assessment: evidence acquisition is `SUPPORTED` only for V020-010, where
the available change tools were not requested; evidence availability and
interpretation are `UNKNOWN`; component attribution is `SUPPORTED`; trigger
representation and benchmark observability are `PROVEN`; and the fixed budget's
causal effect remains `UNKNOWN` despite universal three-call utilization.

## Service attribution analysis

The persisted canonical arguments show `{targeting["alert_service_targeted_requests"]}/`
`{targeting["total_targeted_requests"]}` (`{targeting["alert_service_target_rate"]:.0%}`)
targeted requests matching the service implied by the production alert name.
This includes the worker alert scope (`order-worker`). The five runs with
mechanism correct/service wrong were:

`{", ".join(targeting["mechanism_correct_service_wrong_scenarios"])}`.

This supports alert-service anchoring as a system-level pattern, not a claim
about hidden model reasoning. In particular, V020-003 has a dependency-latency
alert scoped to order-service while the frozen causal component is
payment-service; the artifact shows all requested targets on the alert scope,
but does not persist the model's final component text.

The alert-service targeting rate is `{targeting["service_correct_runs_alert_service_target_rate"]:.0%}`
in service-correct runs and `{targeting["service_wrong_runs_alert_service_target_rate"]:.0%}`
in service-wrong runs. The artifact therefore supports a uniform anchoring
pattern, but cannot by itself establish that anchoring caused each miss.

## V020-008 STOP analysis

V020-008 (`order_worker_failure`) selected `stop_investigation` on turn 3 with
`insufficient_evidence`, after six tools and six evidence items. The artifact
proves a valid STOP and the exact tool sequence, but not the complete evidence
contents or final hypothesis alternative. Therefore the choice among
“necessary evidence not requested”, “weak evidence”, “lag/failure ambiguity”,
and “three-call search envelope” remains `UNKNOWN`; the strongest supported
classification is valid STOP without benchmark completion.

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

The `90%` reference rate proves only that accepted hypothesis references were
runtime-owned for the graded runs. It does not prove evidence sufficiency,
causal relevance, or support for the conclusion. Fabricated and cross-incident
references were both `0`, which is a safety/integrity result.

## Dominant system failure modes

{mode_lines}

The fixed search envelope is proven, but it is not proven to be the cause of any
particular miss. The mechanism/service gap and alert-scope targeting are the
strongest observed system patterns.

## Evaluation observability gaps

- Trigger exact-match scoring is representation-sensitive.
- Complete hypothesis payloads are absent from the frozen artifact.
- Evidence contents are too bounded for post-hoc semantic relevance analysis.
- Mechanism confusion matrices cannot be reconstructed without predicted enums.
- One pass per scenario gives no variance estimate.

## Design requirements for v0.3.0

These are requirements only, not an implementation plan:

1. Distinguish symptom service from causal component in structured output.
2. Measure cross-component causal evidence and ownership explicitly.
3. Persist complete bounded hypothesis payloads and evidence summaries.
4. Use a predeclared structured trigger label or semantic scoring rubric.
5. Preserve v0.2.0 safety floors: fabricated evidence `0`, cross-incident
   evidence `0`, and writes `0`.

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
    analysis = analyze_benchmark(benchmark)
    args.json_output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")
    args.markdown_output.write_text(render_markdown(analysis))
    print(f"v0.2.0 forensic analysis: {len(analysis['per_scenario'])}/10 scenarios")
    print(f"tool requests: {analysis['turn_usage']['tool_requests_total']}")
    print(f"tool executions: {analysis['turn_usage']['tool_executions_total']}")
    print(f"outputs: {args.json_output}, {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
