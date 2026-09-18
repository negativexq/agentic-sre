#!/usr/bin/env python3
"""Search legal deterministic investigation sequences without an LLM."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.environment import SeedPolicy
from packages.rca.investigation.multi_step_search import (
    MultiStepSearchResult,
    SearchStep,
    search_incident,
)
from packages.rca.model import GapResolvability, InformationGap

SCENARIOS = (
    "Scenario-14",
    "Scenario-19",
    "Scenario-25",
    "Scenario-35",
    "Scenario-80",
    "Scenario-81",
    "Scenario-83",
)


def _gaps(result: MultiStepSearchResult) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in result.initial_diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
    )


def _print_cardinality(result: MultiStepSearchResult) -> None:
    diagnosis = result.initial_diagnosis
    trace = diagnosis.resolution_trace
    gaps = _gaps(result)
    alternatives = result.initial_case.structural_alternatives
    targets = {
        target.canonical
        for alternative in alternatives
        for target in alternative.observation_targets
    }
    open_alternatives = sum(item.status.value == "UNEXPLORED" for item in alternatives)
    promoted = sum(item.status.value == "PROMOTED" for item in alternatives)
    print(
        "  space: "
        f"evidence-backed-hypotheses={len(result.initial_case.hypotheses)} "
        f"plausible={len(trace.plausible_hypotheses) if trace else 0} "
        f"leading={len(trace.leading_hypothesis_ids) if trace else 0} "
        f"structural-alternatives={len(alternatives)} "
        f"open={open_alternatives} promoted={promoted} "
        f"gaps={len(gaps)} targets={len(targets)}"
    )
    print(
        f"  completeness: resolver={diagnosis.resolution.value} "
        f"investigation={diagnosis.investigation_status.value}"
    )


def _step_summary(step: SearchStep) -> str:
    return (
        f"{step.capability}[{step.query_template}]/{step.target} raw={step.raw_records} "
        f"new_refs={len(step.new_refs)} findings={','.join(step.new_findings) or '-'} "
        f"hyp={step.hypotheses_before}->{step.hypotheses_after} "
        f"resolution={step.resolution_before}->{step.resolution_after}"
    )


def _print_normalization_blockers(name: str, result: MultiStepSearchResult) -> None:
    if name not in {"Scenario-19", "Scenario-25"}:
        return
    blockers = [step for step in result.attempts if step.new_refs and not step.normalized_findings]
    grouped: Counter[tuple[str, tuple[str, ...]]] = Counter(
        (step.capability, step.payload_fields) for step in blockers
    )
    print("  normalization blockers:")
    for (capability, fields), count in sorted(grouped.items()):
        raw = sum(
            step.raw_records
            for step in blockers
            if step.capability == capability and step.payload_fields == fields
        )
        refs = sum(
            len(step.new_refs)
            for step in blockers
            if step.capability == capability and step.payload_fields == fields
        )
        print(
            f"    {capability} fields={','.join(fields) or '-'} "
            f"queries={count} raw={raw} novel_refs={refs}: no existing signal extractor output"
        )


def _print_cumulative_detail(name: str, result: MultiStepSearchResult) -> None:
    if name not in {"Scenario-35", "Scenario-80", "Scenario-81", "Scenario-83"}:
        return
    q1 = [step for step in result.attempts if step.depth == 1 and step.new_findings]
    q2 = [step for step in result.attempts if step.depth == 2 and step.new_findings]
    print(
        f"  cumulative evidence: Q1 hypothesis-changing queries="
        f"{sum(step.hypotheses_changed for step in q1)}; "
        f"Q2 new-Finding queries={len(q2)}; Q2 resolution changes="
        f"{sum(step.resolution_after == 'RESOLVED' for step in q2)}"
    )


def _print_result(name: str, result: MultiStepSearchResult) -> None:
    solution = result.minimum_solution
    print(name)
    print(f"  initial: {result.initial_diagnosis.resolution.value}")
    _print_cardinality(result)
    print(
        f"  search: states={result.explored_states} attempts={len(result.attempts)} "
        f"truncated={result.truncated}"
    )
    if solution is None:
        print("  minimum queries: -")
    else:
        print(
            f"  minimum queries: {len(solution.steps)} "
            f"final={solution.diagnosis.resolution.value} "
            f"investigation={solution.diagnosis.investigation_status.value}"
        )
        for step in solution.steps:
            print(f"    Q{step.depth}: {_step_summary(step)}")
    _print_normalization_blockers(name, result)
    _print_cumulative_detail(name, result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--max-depth", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--max-states", type=int, default=256)
    args = parser.parse_args()
    scenarios = tuple(args.scenarios or SCENARIOS)
    dataset = ITBenchLiteDataset.open(args.root)
    policy = SeedPolicy()
    results: list[MultiStepSearchResult] = []
    for name in scenarios:
        result = search_incident(
            SnapshotSource(dataset.scenario(name)),
            seed_policy=policy,
            max_depth=args.max_depth,
            max_states=args.max_states,
        )
        results.append(result)
        _print_result(name, result)

    one_step = sum(any(len(path.steps) == 1 for path in item.solutions) for item in results)
    two_step = sum(any(len(path.steps) == 2 for path in item.solutions) for item in results)
    three_step = sum(any(len(path.steps) == 3 for path in item.solutions) for item in results)
    completed = sum(
        any(
            path.diagnosis.resolution.value == "RESOLVED"
            and path.diagnosis.investigation_status.value != "OPEN"
            for path in item.solutions
        )
        for item in results
    )
    print(
        "SUMMARY\n"
        f"  1-step resolution ceiling: {one_step}/{len(results)}\n"
        f"  2-step resolution ceiling: {two_step}/{len(results)}\n"
        f"  3-step resolution ceiling: {three_step}/{len(results)}\n"
        f"  resolver-resolved and frontier-exhausted: {completed}/{len(results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
