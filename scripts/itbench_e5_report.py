"""Build a post-run, checkpoint-derived ITBench E4/E5 report.

This script is reporting-only.  It never constructs an investigator request,
loads a provider, or reads ground truth.  Grades are read from the durable
checkpoint files produced after each prediction was persisted.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
ORDER = json.loads((ROOT / "docs/benchmarks/itbench-lite-e5-manifest.json").read_text())[
    "scenario_order"
]
E4_RESULT = ROOT / "docs/benchmarks/itbench-lite-e4-results.json"
E5_RESULT = ROOT / "docs/benchmarks/itbench-lite-e5-results.json"
E4_RUNS = ROOT / ".local/itbench-lite-e4-runs"
E5_RUNS = ROOT / ".local/itbench-lite-e5-runs/official"
OUT_JSON = ROOT / "docs/benchmarks/itbench-e4-e5-comparison.json"
OUT_MD = ROOT / "docs/benchmarks/itbench-e4-e5-comparison.md"


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def native(root: Path, scenario: str) -> dict[str, Any]:
    return read_json(root / scenario / "1" / "native_artifact.json")


def grade(root: Path, scenario: str) -> dict[str, Any]:
    return read_json(root / scenario / "1" / "grade.json")


def usage(root: Path, scenario: str) -> dict[str, Any]:
    return read_json(root / scenario / "1" / "usage.json")


def summarize(root: Path) -> dict[str, Any]:
    terminals: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    validation: Counter[str] = Counter()
    request_lengths: list[int] = []
    total: Counter[str] = Counter()
    per_scenario: list[dict[str, Any]] = []
    for scenario in ORDER:
        art = native(root, scenario)
        grd = grade(root, scenario)
        use = usage(root, scenario)
        terminals[art["terminal"]] += 1
        total["provider_calls"] += int(use.get("model_calls", 0))
        total["outbound_attempts"] += int(use.get("outbound_api_attempts", 0))
        total["input_tokens"] += int(use.get("input_tokens_total", use.get("input_tokens", 0)))
        total["output_tokens"] += int(use.get("output_tokens_total", use.get("output_tokens", 0)))
        total["provider_latency_ms"] += int(
            use.get("provider_latency_ms_total", use.get("latency_ms", 0))
        )
        total["duration_ms"] += int(use.get("duration_ms", 0))
        total["tool_requests"] += int(use.get("tool_requests_total", 0))
        total["tool_executions"] += int(use.get("tool_calls", 0))
        total["evidence"] += len(art.get("evidence", []))
        for turn in art.get("turns", []):
            requested = turn.get("requested_tools", [])
            if requested:
                request_lengths.append(len(requested))
            categories.update(requested)
            for summary in turn.get("summaries", []):
                statuses[summary.get("status", "UNKNOWN")] += 1
                if summary.get("validation_stage"):
                    validation[summary["validation_stage"]] += 1
        use_stage = use.get("validation_stage")
        if use_stage:
            validation[use_stage] += 1
        predicted_count = int(grd.get("predicted_count", 0))
        per_scenario.append(
            {
                "scenario": scenario,
                "terminal": art["terminal"],
                "model_calls": int(use.get("model_calls", 0)),
                "tool_requests": int(use.get("tool_requests_total", 0)),
                "tool_executions": int(use.get("tool_calls", 0)),
                "predicted_entities": predicted_count,
                "true_positive": int(grd.get("true_positive", 0)),
                "precision": grd.get("precision", 0.0),
                "recall": grd.get("recall", 0.0),
                "f1": grd.get("f1", 0.0),
            }
        )
    total["diagnoses"] = terminals["SUBMIT_DIAGNOSIS"]
    total["entity_hits"] = sum(item["true_positive"] > 0 for item in per_scenario)
    total["zero_entity"] = sum(item["predicted_entities"] == 0 for item in per_scenario)
    total["evidence_producing_executions"] = statuses["SUCCESS"]
    total["duplicates"] = statuses["SKIPPED_DUPLICATE"]
    total["typed_failures"] = statuses["TOOL_EXECUTION_FAILURE"]
    total["timeouts"] = sum(
        1
        for scenario in ORDER
        for turn in native(root, scenario).get("turns", [])
        for item in turn.get("summaries", [])
        if item.get("error_code") == "TOOL_TIMEOUT"
    )
    return {
        "terminals": dict(terminals),
        "categories": dict(categories),
        "summary_statuses": dict(statuses),
        "validation_stages": dict(validation),
        "request_lengths": {
            "count": len(request_lengths),
            "mean": statistics.mean(request_lengths) if request_lengths else 0.0,
            "median": statistics.median(request_lengths) if request_lengths else 0.0,
            "max": max(request_lengths, default=0),
        },
        "totals": dict(total),
        "per_scenario": per_scenario,
    }


def main() -> None:
    e4 = summarize(E4_RUNS)
    e5 = summarize(E5_RUNS)
    e4_result = read_json(E4_RESULT)
    e5_result = read_json(E5_RESULT)
    macro4 = e4_result["macro_entity_metrics"]
    macro5 = e5_result["macro"]
    comparison_rows = []
    e4_by = {row["scenario"]: row for row in e4["per_scenario"]}
    e5_by = {row["scenario"]: row for row in e5["per_scenario"]}
    for scenario in ORDER:
        comparison_rows.append({"scenario": scenario, "e4": e4_by[scenario], "e5": e5_by[scenario]})
    conditional = {
        "e4": {
            "p_diagnosis": e4["totals"]["diagnoses"] / 35,
            "p_correct_given_diagnosis": e4["totals"]["entity_hits"] / e4["totals"]["diagnoses"],
            "p_correct_overall": e4["totals"]["entity_hits"] / 35,
        },
        "e5": {
            "p_diagnosis": e5["totals"]["diagnoses"] / 35,
            "p_correct_given_diagnosis": e5["totals"]["entity_hits"] / e5["totals"]["diagnoses"],
            "p_correct_overall": e5["totals"]["entity_hits"] / 35,
        },
    }
    payload = {
        "artifact_type": "ITBENCH_E4_E5_COMPARISON",
        "note": "Post-run report derived from durable checkpoints; no investigator or provider calls.",
        "e4": {"result_sha256": hashlib.sha256(E4_RESULT.read_bytes()).hexdigest(), "summary": e4},
        "e5": {"result_sha256": hashlib.sha256(E5_RESULT.read_bytes()).hexdigest(), "summary": e5},
        "macro": {"e4": macro4, "e5": macro5},
        "conditional_metrics": conditional,
        "scenario_comparison": comparison_rows,
        "official_evaluator": "NOT RUN; this report uses the corrected deterministic local scorer",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    lines = [
        "# ITBench E4 → E5 post-run comparison",
        "",
        "This report is derived from durable checkpoints. It does not rerun the investigator or provider.",
        "",
        "| Metric | E4 | E5 | Delta |",
        "|---|---:|---:|---:|",
    ]
    metrics = [
        ("Macro precision", macro4["precision"], macro5["precision"]),
        ("Macro recall", macro4["recall"], macro5["recall"]),
        ("Macro F1", macro4["f1"], macro5["f1"]),
        ("Entity hits / 35", e4["totals"]["entity_hits"], e5["totals"]["entity_hits"]),
        ("Diagnosis submissions", e4["totals"]["diagnoses"], e5["totals"]["diagnoses"]),
        ("Zero-entity scenarios", e4["totals"]["zero_entity"], e5["totals"]["zero_entity"]),
        (
            "TOOL_CALL_LIMIT",
            e4["terminals"].get("TOOL_CALL_LIMIT", 0),
            e5["terminals"].get("TOOL_CALL_LIMIT", 0),
        ),
        (
            "MODEL_DECISION_INVALID",
            e4["terminals"].get("MODEL_DECISION_INVALID", 0),
            e5["terminals"].get("MODEL_DECISION_INVALID", 0),
        ),
        ("STOP", e4["terminals"].get("STOP", 0), e5["terminals"].get("STOP", 0)),
        ("Tool requests", e4["totals"]["tool_requests"], e5["totals"]["tool_requests"]),
        ("Tool executions", e4["totals"]["tool_executions"], e5["totals"]["tool_executions"]),
        ("Duplicate requests", e4["totals"]["duplicates"], e5["totals"]["duplicates"]),
        ("Input tokens", e4["totals"]["input_tokens"], e5["totals"]["input_tokens"]),
    ]
    for name, old, new in metrics:
        lines.append(
            f"| {name} | {old:.5f} | {new:.5f} | {new - old:+.5f} |"
            if isinstance(old, float) or isinstance(new, float)
            else f"| {name} | {old} | {new} | {new - old:+} |"
        )
    lines += [
        "",
        "## Conditional diagnosis metrics",
        "",
        f"- E4 P(diagnosis)={conditional['e4']['p_diagnosis']:.5f}; P(correct | diagnosis)={conditional['e4']['p_correct_given_diagnosis']:.5f}; P(correct overall)={conditional['e4']['p_correct_overall']:.5f}",
        f"- E5 P(diagnosis)={conditional['e5']['p_diagnosis']:.5f}; P(correct | diagnosis)={conditional['e5']['p_correct_given_diagnosis']:.5f}; P(correct overall)={conditional['e5']['p_correct_overall']:.5f}",
        "",
        "## E5 terminal/validation observations",
        "",
        f"- terminals: {e5['terminals']}",
        f"- decision validation stages: {e5['validation_stages']}",
        f"- tool summary statuses: {e5['summary_statuses']}",
        f"- request batch stats: {e5['request_lengths']}",
        "",
        "Official ITBench judge: NOT RUN.",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "json": str(OUT_JSON),
                "markdown": str(OUT_MD),
                "macro": payload["macro"],
                "terminals": e5["terminals"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
