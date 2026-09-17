"""Diagnostic-only qualification of ITBench root-cause entity contracts.

This module intentionally runs after prediction and may read ground truth.  It
does not alter the official grader and never adds aliases to the RCA engine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import (
    ITBenchGroundTruthGroup,
    ITBenchScenario,
)
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.rca.model import EntityRef
from packages.rca.topology import Topology, derive_edges


def _matches(
    entity: EntityRef, group: ITBenchGroundTruthGroup, expressions: Sequence[re.Pattern[str]]
) -> bool:
    if entity.kind.casefold() != group.kind.casefold():
        return False
    if group.namespace and entity.namespace != group.namespace:
        return False
    if group.name and entity.name == group.name:
        return True
    return any(expression.search(entity.name) for expression in expressions)


def _compile_filters(
    group: ITBenchGroundTruthGroup,
) -> tuple[tuple[str, ...], tuple[re.Pattern[str], ...]]:
    invalid: list[str] = []
    compiled: list[re.Pattern[str]] = []
    for expression in group.filters:
        try:
            compiled.append(re.compile(expression))
        except re.error:
            invalid.append(expression)
    return tuple(invalid), tuple(compiled)


def _relation_for(root: EntityRef, candidate: EntityRef, topology: Topology) -> str | None:
    for edge in topology.edges:
        if {edge.source, edge.target} == {root, candidate}:
            return edge.relation
    if candidate in topology.reachable(root, max_depth=3):
        if topology.workload_of(candidate) == root:
            return "owned_by_chain"
    if root.kind == "Namespace" and root.name == candidate.namespace:
        return "namespace_contains"
    if candidate.kind == "Namespace" and candidate.name == root.namespace:
        return "namespace_contains"
    # The production topology deliberately does not create namespace edges;
    # this qualification-only relation is still deterministic and explicit.
    return None


def qualify_group(
    group: ITBenchGroundTruthGroup,
    entities: Iterable[EntityRef],
    topology: Topology,
) -> dict[str, Any]:
    """Qualify one root-cause group against observable entities and relations."""
    observable = sorted(set(entities), key=lambda item: item.canonical)
    invalid, compiled = _compile_filters(group)
    exact = [entity for entity in observable if not invalid and _matches(entity, group, compiled)]
    related: list[dict[str, str]] = []
    exact_set = set(exact)
    for entity in observable:
        if entity in exact_set:
            continue
        relation = None
        for root in exact:
            relation = _relation_for(root, entity, topology)
            if relation is not None:
                related.append(
                    {
                        "entity": entity.canonical,
                        "relation": relation,
                        "root_entity": root.canonical,
                    }
                )
                break
    return {
        "group_id": group.group_id,
        "kind": group.kind,
        "namespace": group.namespace,
        "name": group.name,
        "filters": list(group.filters),
        "root_cause": group.root_cause,
        "compiled_successfully": not invalid,
        "invalid_filters": list(invalid),
        "exact_matches": [entity.canonical for entity in exact],
        "related_entities": related,
        "ambiguous": len(exact) + len(related) > 1,
    }


def qualify_scenario(dataset: ITBenchLiteDataset, scenario: ITBenchScenario) -> dict[str, Any]:
    """Build one qualification record; ground truth is read only in this diagnostic path."""
    source = _source(dataset, scenario)
    history = source.object_history()
    events = source.events()
    latest = {ref: versions[-1] for ref, versions in history.items() if versions}
    topology = Topology(derive_edges(latest, events), latest)
    entities = set(history) | {event.entity for event in events}
    truth = dataset.load_ground_truth(scenario.scenario_id)
    return {
        "scenario_id": scenario.scenario_id,
        "root_cause_groups": [
            qualify_group(group, entities, topology)
            for group in truth.root_cause_groups
            if group.root_cause
        ],
    }


def _source(dataset: ITBenchLiteDataset, scenario: ITBenchScenario) -> Any:
    from packages.evals.itbench.source import SnapshotSource

    return SnapshotSource(scenario)


def qualify_dataset(
    dataset: ITBenchLiteDataset, scenario_ids: Sequence[str], out_dir: Path
) -> dict[str, Any]:
    """Write a qualification report without touching prediction or release artifacts."""
    records = [
        qualify_scenario(dataset, dataset.scenario(scenario_id)) for scenario_id in scenario_ids
    ]
    report: dict[str, Any] = {
        "benchmark": "ITBench-Lite entity contract qualification",
        "scenarios": len(records),
        "records": records,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "qualification.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# ITBench-Lite entity contract qualification",
        "",
        "Diagnostic-only output; official benchmark matching is unchanged.",
        "",
        "| Scenario | Group | Compiles | Exact matches | Related entities | Ambiguous |",
        "| --- | --- | :---: | --- | --- | :---: |",
    ]
    for record in records:
        for group in record["root_cause_groups"]:
            lines.append(
                f"| {record['scenario_id']} | `{group['group_id']}` | "
                f"{'yes' if group['compiled_successfully'] else 'no'} | "
                f"{', '.join(group['exact_matches']) or '-'} | "
                f"{', '.join(item['entity'] for item in group['related_entities']) or '-'} | "
                f"{'yes' if group['ambiguous'] else 'no'} |"
            )
    (out_dir / "qualification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


__all__ = ["qualify_dataset", "qualify_group", "qualify_scenario"]
