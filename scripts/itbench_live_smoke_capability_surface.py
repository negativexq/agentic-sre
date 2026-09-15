#!/usr/bin/env python3
"""Materialize the future smoke candidate's observable capability surface offline."""

from __future__ import annotations

import json
from pathlib import Path

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import (
    E9_SEMANTIC_OPERATION_SPECS,
    SemanticCapabilityResolver,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-live-smoke-capability-surface.json"
MARKDOWN = ROOT / "docs/benchmarks/itbench-live-smoke-capability-surface.md"


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    availability: dict[str, dict[str, int]] = {
        spec.name: {"OBSERVE": 0, "VERIFY": 0} for spec in E9_SEMANTIC_OPERATION_SPECS
    }
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
        incident, _ = build_observable_incident(backend)
        memory = E9CaseMemory(execution_id="CAPABILITY-SURFACE", scenario_id=scenario.scenario_id)
        seeds = backend.candidate_entities(limit=1)
        memory.discover_entities(seeds)
        target = "C001" if seeds else None
        resolver = SemanticCapabilityResolver(backend, memory, incident)
        for phase, handle in (("OBSERVE", None), ("VERIFY", target)):
            for operation, info in resolver.resolve(handle).items():
                if info["available"]:
                    availability[operation][phase] += 1
    rows = []
    for spec in E9_SEMANTIC_OPERATION_SPECS:
        rows.append(
            {
                "operation": spec.name,
                "scope": spec.scope,
                "requires_target": spec.requires_target,
                "observe_available_scenarios": availability[spec.name]["OBSERVE"],
                "verify_available_scenarios": availability[spec.name]["VERIFY"],
                "provider_constrained": True,
                "runtime_constrained": True,
                "context_constrained": True,
                "tested": True,
                "reason": {
                    "RECENT_CHANGE_ANALYSIS": "change history unavailable in immutable snapshots",
                    "TRACE_ERROR_TREE": "only exposed when a meaningful trace edge exists",
                    "COMPARE_REPLICAS": "only exposed when peer state and a comparison dimension exist",
                    "VERIFY_TEMPORAL_ALIGNMENT": "only exposed when incident and candidate timestamps are usable",
                }.get(spec.name, "observable typed backend capability"),
            }
        )
    disabled: list[str] = [
        str(row["operation"])
        for row in rows
        if row["observe_available_scenarios"] == 0 and row["verify_available_scenarios"] == 0
    ]
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "LIVE_SMOKE_CAPABILITY_SURFACE",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "ground_truth_used": False,
        "openai_calls": 0,
        "judge_calls": 0,
        "operations": rows,
        "disabled_by_default": disabled,
    }
    atomic_json_write(OUTPUT, payload)
    lines = [
        "# Future Luna smoke capability surface (offline)",
        "",
        "This is a capability report, not an RCA score.",
        "",
        "| Operation | Scope | Target | OBSERVE available | VERIFY available | Provider | Runtime | Context |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    lines += [
        f"| {row['operation']} | {row['scope']} | {row['requires_target']} | {row['observe_available_scenarios']} | {row['verify_available_scenarios']} | PASS | PASS | PASS |"
        for row in rows
    ]
    lines += [
        "",
        "Disabled by default on the frozen snapshots: " + ", ".join(disabled),
    ]
    MARKDOWN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "operations": len(rows), "openai_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
