"""Observable-only candidate retrieval audit for ITB-E7.

Candidate lists are frozen in memory before any evaluator ground truth is
loaded.  Ground truth is used only to score the already-produced lists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from packages.evals.itbench.dataset import ITBenchLiteDataset

DATASET = Path(".local/itbench-lite")
OUT_JSON = Path("docs/benchmarks/itbench-e7-candidate-retrieval-audit.json")
OUT_MD = Path("docs/benchmarks/itbench-e7-candidate-retrieval-audit.md")


def _matches(canonical: str, group: Any) -> bool:
    namespace, kind, name = canonical.split("/", 2)
    if kind.casefold() != group.kind.casefold():
        return False
    if group.namespace and namespace != group.namespace:
        return False
    if group.name and name == group.name:
        return True
    for expression in group.filters:
        try:
            if re.search(expression, name):
                return True
        except re.error:
            if expression == name:
                return True
    return False


def main() -> None:
    dataset = ITBenchLiteDataset.open(DATASET)
    scenarios = dataset.scenarios()
    # This loop is intentionally GT-free.  No ground_truth.yaml is opened.
    frozen: list[dict[str, Any]] = []
    for scenario in scenarios:
        from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
        ranked = list(backend.candidate_entities(limit=10))
        frozen.append({"scenario": scenario.scenario_id, "candidates": ranked})

    rows: list[dict[str, Any]] = []
    recalls = {1: 0, 3: 0, 5: 0, 10: 0}
    observable_group_counts = {1: 0, 3: 0, 5: 0, 10: 0}
    observable_root_groups = 0
    for item in frozen:
        gt = dataset.load_ground_truth(item["scenario"])
        roots = [group for group in gt.root_cause_groups if group.root_cause]
        candidates = [row["canonical"] for row in item["candidates"]]
        hits = {
            k: sum(
                any(_matches(candidate, group) for candidate in candidates[:k]) for group in roots
            )
            for k in recalls
        }
        observable = [
            group for group in roots if any(_matches(candidate, group) for candidate in candidates)
        ]
        observable_root_groups += len(observable)
        for k in observable_group_counts:
            observable_group_counts[k] += sum(
                any(_matches(candidate, group) for candidate in candidates[:k])
                for group in observable
            )
        for k, hit in hits.items():
            recalls[k] += int(hit == len(roots))
        rows.append(
            {
                "scenario": item["scenario"],
                "candidate_count": len(candidates),
                "candidates": item["candidates"],
                "root_group_count": len(roots),
                "observable_root_group_count": len(observable),
                "root_groups_observable_in_top_k": hits,
                "root_groups": [
                    {
                        "id": group.group_id,
                        "kind": group.kind,
                        "namespace": group.namespace,
                        "name": group.name,
                        "filters": list(group.filters),
                    }
                    for group in roots
                ],
            }
        )
    payload = {
        "execution": "ITB-E7-pre",
        "method": "observable candidate ranking frozen before post-hoc GT load",
        "scenario_count": len(rows),
        "recall_at_k": {str(k): recalls[k] / len(rows) for k in recalls},
        "root_group_recall_at_k": {
            str(k): observable_group_counts[k] / observable_root_groups
            if observable_root_groups
            else 0.0
            for k in observable_group_counts
        },
        "observable_root_groups": observable_root_groups,
        "scenarios": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# ITB-E7 candidate retrieval audit",
        "",
        "Candidate ranking used observable alerts/objects/events/topology only.",
        "The candidate lists were frozen before evaluator ground truth was loaded.",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for k in recalls:
        lines.append(f"| Recall@{k} | {recalls[k] / len(rows):.3f} |")
    lines.append(f"| Observable-root groups | {observable_root_groups} |")
    for k in observable_group_counts:
        value = (
            observable_group_counts[k] / observable_root_groups if observable_root_groups else 0.0
        )
        lines.append(f"| Conditional root-group Recall@{k} | {value:.3f} |")
    lines += ["", "| scenario | top candidates |", "|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['scenario']} | {', '.join(item['canonical'] for item in row['candidates'][:5])} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
