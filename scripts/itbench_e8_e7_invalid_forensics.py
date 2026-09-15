#!/usr/bin/env python3
"""Build a bounded, post-hoc audit of frozen E7 invalid decisions.

This script reads only durable E7 artifacts.  It deliberately does not try to
reconstruct malformed provider payloads that the E7 runtime did not persist.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from packages.evals.itbench.persistence import atomic_json_write

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / ".local/itbench-lite-e7-runs/official"
SMOKE = RUN_ROOT.parent / "ITB-E7-SMOKE-002/smoke/native_artifact.json"
JSON_REPORT = ROOT / "docs/benchmarks/itbench-e7-decision-invalid-forensics.json"
MARKDOWN_REPORT = ROOT / "docs/benchmarks/itbench-e7-decision-invalid-forensics.md"

NOT_RECORDED = "NOT_DURABLY_RECORDED"


def _invalid_record(path: Path, *, smoke: bool = False) -> dict[str, Any]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    turns = artifact.get("turns", [])
    invalid_turns = [
        turn
        for turn in turns
        if turn.get("decision") in {"MODEL_DECISION_INVALID", "INVALID_TOOL_REQUEST"}
    ]
    if not invalid_turns:
        raise ValueError(f"no invalid decision in {path}")
    invalid = invalid_turns[-1]
    prior = [turn for turn in turns if turn.get("decision") == "CALL_TOOLS"]
    last_prior = prior[-1] if prior else {}
    usage = artifact.get("usage", {})
    validation_stage = invalid.get("validation_stage", NOT_RECORDED)
    validation_type = invalid.get("validation_type", NOT_RECORDED)
    if validation_stage == "TOOL_ARGUMENTS":
        failure_class = "tool_argument_schema"
    elif validation_stage == "DECISION_SCHEMA":
        failure_class = "unknown"
    else:
        failure_class = "other"
    root_cause = (
        "TOOL_ARGUMENTS_INVALID" if validation_stage == "TOOL_ARGUMENTS" else "NOT_DURABLY_RECORDED"
    )
    reason = (
        "The runtime durably recorded a model-generated tool argument validation failure; no "
        "tool execution occurred for that request."
        if validation_stage == "TOOL_ARGUMENTS"
        else "The runtime persisted only a bounded validation diagnostic and did not persist the "
        "malformed provider payload or function name."
    )
    return {
        "scenario_id": "Scenario-999" if smoke else artifact.get("scenario_id", NOT_RECORDED),
        "source_artifact": str(path.relative_to(ROOT)),
        "turn": invalid.get("turn", NOT_RECORDED),
        "validation_stage": validation_stage,
        "validation_path": invalid.get("validation_path", NOT_RECORDED),
        "validation_type": validation_type,
        "provider_response_received": invalid.get("provider_response_received", NOT_RECORDED),
        "decision_function_name": NOT_RECORDED,
        "normalized_decision_shape": NOT_RECORDED,
        "requests_cardinality": NOT_RECORDED,
        "root_causes_cardinality": NOT_RECORDED,
        "evidence_refs_cardinality": NOT_RECORDED,
        "last_durable_call_tools_request_count": len(last_prior.get("requested_tools", []))
        if prior
        else NOT_RECORDED,
        "model_calls": usage.get("model_calls", NOT_RECORDED),
        "failure_class": failure_class,
        "root_cause": root_cause,
        "reason": reason,
    }


def build_report() -> dict[str, Any]:
    records = []
    for path in sorted(RUN_ROOT.glob("Scenario-*/1/native_artifact.json")):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if artifact.get("terminal") == "MODEL_DECISION_INVALID":
            records.append(_invalid_record(path))
    smoke_record = _invalid_record(SMOKE, smoke=True)
    mechanisms = Counter(record["failure_class"] for record in records)
    return {
        "execution": "ITB-E7",
        "purpose": "E8 pre-change forensic audit",
        "provider_calls": 0,
        "judge_calls": 0,
        "ground_truth_used": False,
        "official_invalid_count": len(records),
        "official_invalid_records": records,
        "smoke_invalid_record": smoke_record,
        "aggregate_failure_mechanisms": {
            key: mechanisms.get(key, 0)
            for key in (
                "empty_CALL_TOOLS_request_collection",
                "empty_SUBMIT_root_cause_collection",
                "mixed_decision_payload",
                "invalid_evidence_refs",
                "entity_syntax",
                "tool_argument_schema",
                "root_cause_cardinality",
                "other",
                "unknown",
            )
        },
        "conclusion": {
            "dominant_mechanism": "UNKNOWN",
            "wire_local_mismatch_proven": "NOT_DURABLY_PROVABLE_FROM_E7_ARTIFACTS",
            "genuine_model_semantic_mistake_proven": "NO",
            "tool_contract_mistake_proven": "NO",
            "several_unrelated_causes_proven": "NO",
            "explanation": (
                "All 13 official invalid decisions have the same bounded value_error at the "
                "root path, but E7 did not retain the malformed response shape. No more specific "
                "cause is asserted from these artifacts."
            ),
        },
    }


def main() -> None:
    report = build_report()
    atomic_json_write(JSON_REPORT, report)
    lines = [
        "# ITBench E7 invalid-decision forensic audit",
        "",
        "This is a post-hoc, ground-truth-free audit of immutable E7 artifacts.",
        "No provider or judge calls were made.",
        "",
        f"Official invalid decisions: **{report['official_invalid_count']}**.",
        "",
        "| mechanism | count |",
        "|---|---:|",
    ]
    for key, value in report["aggregate_failure_mechanisms"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(
        [
            "",
            "All official records durably contain `DECISION_SCHEMA`, path `$`, type "
            "`value_error`, and `provider_response_received=true`. The malformed structured "
            "payload, function name, and exact cardinalities were not persisted by E7.",
            "",
            "Therefore the dominant mechanism is recorded as `UNKNOWN`, not attributed to a "
            "model mistake or a protocol mismatch without evidence.",
            "",
            "The E7 smoke has the same bounded diagnostic. Its exact root cause is also "
            "`NOT_DURABLY_RECORDED`.",
        ]
    )
    MARKDOWN_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
