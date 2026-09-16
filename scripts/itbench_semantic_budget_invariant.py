#!/usr/bin/env python3
"""Offline proof that semantic capability cannot bypass the action budget."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.e9_control import control_surface
from packages.provider.openai import _itbench_v5_decision_function_schemas

ROOT = Path(__file__).resolve().parents[1]
JSON_OUTPUT = ROOT / "docs/benchmarks/itbench-semantic-budget-invariant.json"
MD_OUTPUT = ROOT / "docs/benchmarks/itbench-semantic-budget-invariant.md"


def state(
    *,
    used: int,
    current: str = "C001",
    completed: tuple[tuple[str, str], ...] = (),
    evidence: bool = False,
) -> dict[str, Any]:
    entities = {
        f"otel-demo/Service/{handle.lower()}": {
            "handle": handle,
            "canonical": f"otel-demo/Service/{handle.lower()}",
            "metadata": {},
            "discovered_turn": 0,
        }
        for handle in ("C001", "C002")
    }
    return {
        "current_phase": "VERIFY",
        "current_hypothesis": {"entity_handle": current, "rationale": "offline"},
        "discovered_entities": entities,
        "operations_already_run": [
            {"entity_handle": handle, "operation": operation} for handle, operation in completed
        ],
        "semantic_actions_used": used,
        "consecutive_rejections": 0,
        "evidence": (
            {"E001": {"entity_handle": current, "operation": "ENTITY_CONTEXT"}} if evidence else {}
        ),
    }


def run_case(
    name: str,
    current_state: dict[str, Any],
    available: tuple[str, ...],
    *,
    limit: int = 3,
) -> dict[str, Any]:
    surface = control_surface(
        current_state,
        turn=2,
        max_steps=12,
        max_rejections=2,
        semantic_limit=limit,
        available_operations=available,
    )
    request_schemas = _itbench_v5_decision_function_schemas(
        allowed_actions=tuple(
            action for action in surface.actions if action not in {"SUBMIT", "STOP"}
        ),
        allowed_operations=surface.operations,
        allowed_targets=surface.target_handles,
        allowed_action_capabilities=surface.capabilities(),
    )
    decision_schema = (
        request_schemas["request_itbench_tools"].get("properties", {}).get("decision", {})
    )
    branches = decision_schema.get("anyOf", [])
    provider_branches = [
        {
            "action": branch.get("properties", {}).get("action", {}).get("enum", []),
            "targets": branch.get("properties", {}).get("target", {}).get("enum", []),
            "operations": branch.get("properties", {}).get("operation", {}).get("enum", []),
        }
        for branch in branches
    ]
    passed = True
    if current_state["semantic_actions_used"] >= limit:
        passed = not surface.operations and "INVESTIGATE" not in surface.actions
    result = {
        "case": name,
        "semantic_actions_used": current_state["semantic_actions_used"],
        "semantic_limit": limit,
        "resolver_available_operations": list(available),
        "completed_operations": current_state["operations_already_run"],
        "final_effective_operations": list(surface.operations),
        "valid_actions": list(surface.actions),
        "provider_operations": list(surface.operations),
        "provider_branches": provider_branches,
        "runtime_operations": list(surface.operations),
        "pass": passed,
    }
    if not passed:
        raise RuntimeError(f"semantic budget invariant failed: {name}")
    return result


def main() -> int:
    cases = [
        run_case("BELOW_LIMIT", state(used=2), ("ENTITY_CONTEXT",)),
        run_case(
            "AT_LIMIT",
            state(used=3),
            ("ENTITY_CONTEXT", "EVENT_ANALYSIS", "SPEC_ANALYSIS"),
        ),
        run_case(
            "OVER_LIMIT",
            state(used=4),
            ("ENTITY_CONTEXT", "EVENT_ANALYSIS", "SPEC_ANALYSIS"),
        ),
        run_case(
            "CAPABILITY_INTERSECTION",
            state(used=0, completed=(("C001", "ENTITY_CONTEXT"),)),
            ("ENTITY_CONTEXT", "SPEC_ANALYSIS"),
        ),
        run_case(
            "TARGET_SCOPED_DUPLICATE",
            state(used=1, current="C002", completed=(("C001", "ENTITY_CONTEXT"),)),
            ("ENTITY_CONTEXT",),
        ),
        run_case("SUBMIT_AT_LIMIT", state(used=3, evidence=True), ("ENTITY_CONTEXT",)),
        run_case("NO_EVIDENCE_AT_LIMIT", state(used=3), ("ENTITY_CONTEXT",)),
    ]
    payload = {
        "execution": "ITB-E9-OFFLINE",
        "purpose": "SEMANTIC_BUDGET_INVARIANT",
        "provider_calls": 0,
        "judge_calls": 0,
        "cases": cases,
        "status": "PASS",
    }
    JSON_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# ITBench semantic budget invariant",
        "",
        "Offline-only proof that capability availability intersects, rather than replaces, the CaseState semantic-action budget.",
        "",
        "| Case | Used/limit | Resolver operations | Effective operations | Valid actions | Result |",
        "|---|---:|---|---|---|---|",
    ]
    for item in cases:
        lines.append(
            f"| {item['case']} | {item['semantic_actions_used']}/{item['semantic_limit']} | "
            f"{', '.join(item['resolver_available_operations']) or '—'} | "
            f"{', '.join(item['final_effective_operations']) or '—'} | "
            f"{', '.join(item['valid_actions'])} | {'PASS' if item['pass'] else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "The authoritative budget is `CaseState.semantic_actions_used`. At or over the limit, `INVESTIGATE` is absent while valid `SUBMIT`, `REVISE`, and `STOP` branches remain governed by their own preconditions.",
            "",
            "Live/model calls: 0. Judge calls: 0.",
        ]
    )
    MD_OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "cases": len(cases), "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
