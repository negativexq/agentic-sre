#!/usr/bin/env python3
"""Audit GT-blind manifestation subsumption over the FULL DEV RCA cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.unresolved_subsumption import (
    RULE_IDS,
    UnresolvedSubsumptionBlindAudit,
    blind_audit_from_dict,
    build_blind_audit,
    forbidden_blind_keys,
)

DEV_SCENARIOS = (
    "Scenario-4",
    "Scenario-7",
    "Scenario-8",
    "Scenario-11",
    "Scenario-12",
    "Scenario-17",
    "Scenario-21",
    "Scenario-29",
    "Scenario-34",
    "Scenario-91",
)
OUT_DIR = Path(".local/diagnostics/unresolved-subsumption")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


def _entity(value: str) -> Any:
    from packages.evals.itbench.contracts import ITBenchEntity

    namespace, kind, name = value.split("/", 2)
    return ITBenchEntity(
        namespace=None if namespace == "_cluster" else namespace, kind=kind, name=name
    )


def _root_ids(gt: Any) -> tuple[str, ...]:
    return tuple(sorted(group.group_id for group in gt.root_cause_groups if group.root_cause))


def _alias_to_root(gt: Any) -> dict[str, str]:
    root_ids = set(_root_ids(gt))
    aliases: dict[str, str] = {}
    for alias_group in gt.aliases:
        matching = sorted(root_ids.intersection(alias_group))
        if matching:
            for group_id in alias_group:
                aliases[group_id] = matching[0]
    return aliases


def _matches_group(entity: str, group: Any) -> bool:
    from packages.evals.itbench.grader import entity_matches_group

    return entity_matches_group(_entity(entity), group)


def _matching_roots(entity: str, gt: Any) -> tuple[str, ...]:
    groups = {group.group_id: group for group in gt.root_cause_groups}
    aliases = _alias_to_root(gt)
    matches: set[str] = set()
    for group in gt.root_cause_groups:
        if not _matches_group(entity, group):
            continue
        root_id = group.group_id if group.root_cause else aliases.get(group.group_id)
        if root_id is not None:
            matches.add(root_id)
    return tuple(sorted(root_id for root_id in matches if root_id in groups))


def _supported_matches(
    audit: UnresolvedSubsumptionBlindAudit, identifier: str, gt: Any
) -> tuple[str, ...]:
    item = next(item for item in audit.supported_episodes if item.hypothesis_id == identifier)
    values = (item.actor, *item.members)
    return tuple(sorted({root for value in values for root in _matching_roots(value, gt)}))


def _unresolved_matches(
    audit: UnresolvedSubsumptionBlindAudit, identifier: str, gt: Any
) -> tuple[str, ...]:
    item = next(item for item in audit.unresolved_blockers if item.hypothesis_id == identifier)
    return _matching_roots(item.actor, gt)


def _pair_safety(
    audit: UnresolvedSubsumptionBlindAudit, pair_key: str, gt: Any
) -> dict[str, object]:
    supported_id, unresolved_id = pair_key.split("->", 1)
    supported_roots = _supported_matches(audit, supported_id, gt)
    unresolved_roots = _unresolved_matches(audit, unresolved_id, gt)
    if unresolved_roots and not supported_roots:
        classification = "GT_ROOT_SUBSUMED_BY_NON_GT"
    elif unresolved_roots and not set(unresolved_roots).intersection(supported_roots):
        classification = "GT_ROOT_SUBSUMED_IN_MULTI_CAUSE"
    else:
        classification = "SAFE_OR_NON_GT_PAIR"
    return {
        "pair": pair_key,
        "supported_actor": next(
            item.actor for item in audit.supported_episodes if item.hypothesis_id == supported_id
        ),
        "unresolved_actor": next(
            item.actor for item in audit.unresolved_blockers if item.hypothesis_id == unresolved_id
        ),
        "supported_gt": supported_roots,
        "unresolved_gt": unresolved_roots,
        "classification": classification,
    }


def _counterfactual_safety(
    audit: UnresolvedSubsumptionBlindAudit, rule: Any, gt: Any
) -> str | None:
    if rule.counterfactual.counterfactual_resolution != "RESOLVED_COUNTERFACTUAL":
        return None
    selected = rule.counterfactual.counterfactual_selected_id
    if selected is None:
        return "FALSE_CONFIDENT_RESOLUTION"
    roots = _supported_matches(audit, selected, gt)
    return None if roots else "FALSE_CONFIDENT_RESOLUTION"


def _blind_payload(dataset: ITBenchLiteDataset) -> dict[str, object]:
    audits: dict[str, object] = {}
    for scenario_id in DEV_SCENARIOS:
        from packages.evals.itbench.source import SnapshotSource

        audit = build_blind_audit(SnapshotSource(dataset.scenario(scenario_id)))
        audits[scenario_id] = audit.as_dict()
    payload: dict[str, object] = {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "rule_ids": RULE_IDS,
        "ground_truth_loaded": False,
        "audits": audits,
    }
    forbidden = forbidden_blind_keys(payload)
    if forbidden:
        raise RuntimeError(f"forbidden blind fields: {forbidden}")
    return payload


def _overlay(dataset: ITBenchLiteDataset, blind: Mapping[str, Any]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    safety_rows: list[dict[str, object]] = []
    unsafe_counts = {
        rule_id: {"non_gt": 0, "multi": 0, "false_confident": 0} for rule_id in RULE_IDS
    }
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], blind["audits"][scenario_id]))
        gt = dataset.load_ground_truth(scenario_id)
        for result in audit.rule_results:
            for pair_key in result.pair_keys:
                row = _pair_safety(audit, pair_key, gt)
                row["scenario_id"] = scenario_id
                row["rule_id"] = result.rule_id
                safety_rows.append(row)
                if row["classification"] == "GT_ROOT_SUBSUMED_BY_NON_GT":
                    unsafe_counts[result.rule_id]["non_gt"] += 1
                elif row["classification"] == "GT_ROOT_SUBSUMED_IN_MULTI_CAUSE":
                    unsafe_counts[result.rule_id]["multi"] += 1
            false_confident = _counterfactual_safety(audit, result, gt)
            if false_confident:
                unsafe_counts[result.rule_id]["false_confident"] += 1
        rows.append(
            {
                "scenario_id": scenario_id,
                "production_resolution": audit.production_resolution,
                "supported_count": len(audit.supported_episodes),
                "unresolved_count": len(audit.unresolved_blockers),
                "rules": [
                    {
                        "rule_id": result.rule_id,
                        "subsumed_ids": result.subsumed_ids,
                        "counterfactual_resolution": result.counterfactual.counterfactual_resolution,
                        "selected_id": result.counterfactual.counterfactual_selected_id,
                    }
                    for result in audit.rule_results
                ],
            }
        )
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "ground_truth_loaded_after_seal": True,
        "blind_artifact_sha256": _sha(OUT_DIR / "dev-blind.json"),
        "rows": rows,
        "gt_safety": safety_rows,
        "unsafe_counts": unsafe_counts,
    }


def _print_blind(payload: Mapping[str, Any]) -> None:
    print(
        "Scenario | Production | Supported count | Unresolved blockers | S1 subsumed | S2 subsumed | S3 subsumed"
    )
    print("---|---|---:|---:|---:|---:|---:")
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], payload["audits"][scenario_id]))
        counts = {result.rule_id: len(result.subsumed_ids) for result in audit.rule_results}
        print(
            f"{scenario_id} | {audit.production_resolution} | {len(audit.supported_episodes)} | "
            f"{len(audit.unresolved_blockers)} | {counts[RULE_IDS[0]]} | {counts[RULE_IDS[1]]} | {counts[RULE_IDS[2]]}"
        )
    print("\nScenario | Unresolved actor | Class | S1 | S2 | S3 | Supported actor")
    print("---|---|---|---|---|---|---")
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], payload["audits"][scenario_id]))
        for blocker in audit.unresolved_blockers:
            pairs = [
                item
                for item in audit.pairwise_relations
                if item.unresolved_id == blocker.hypothesis_id
            ]
            if not pairs:
                print(
                    f"{scenario_id} | {blocker.actor} | {blocker.evidence_class} | "
                    "False | False | False | —"
                )
            for pair in pairs:
                print(
                    f"{scenario_id} | {blocker.actor} | {blocker.evidence_class} | "
                    f"{getattr(pair, 's1_eligible', False)} | {getattr(pair, 's2_eligible', False)} | "
                    f"{getattr(pair, 's3_eligible', False)} | {getattr(pair, 'supported_actor', '—')}"
                )


def _print_overlay(payload: Mapping[str, Any]) -> None:
    print(
        "Scenario | Rule | Subsumed actor | Upstream supported actor | Subsumed GT? | Upstream GT? | Safety classification"
    )
    print("---|---|---|---|---|---|---")
    for row in cast(list[dict[str, Any]], payload["gt_safety"]):
        print(
            f"{row['scenario_id']} | {row['rule_id']} | {row['unresolved_actor']} | "
            f"{row['supported_actor']} | {row['unresolved_gt']} | {row['supported_gt']} | {row['classification']}"
        )
    print("\nScenario | Production | Supported | Unresolved | S1 | S2 | S3")
    print("---|---|---:|---:|---|---|---")
    for row in cast(list[dict[str, Any]], payload["rows"]):
        values = {item["rule_id"]: item for item in row["rules"]}
        print(
            f"{row['scenario_id']} | {row['production_resolution']} | {row['supported_count']} | "
            f"{row['unresolved_count']} | {values[RULE_IDS[0]]['counterfactual_resolution']} | "
            f"{values[RULE_IDS[1]]['counterfactual_resolution']} | {values[RULE_IDS[2]]['counterfactual_resolution']}"
        )
    print("\nRule | Non-GT root subsumed | Multi-cause root subsumed | False confident")
    print("---|---:|---:|---:")
    for rule_id, counts in cast(dict[str, dict[str, int]], payload["unsafe_counts"]).items():
        print(f"{rule_id} | {counts['non_gt']} | {counts['multi']} | {counts['false_confident']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".local/itbench-lite"))
    parser.add_argument("--split", choices=("dev",), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--blind", action="store_true")
    mode.add_argument("--with-ground-truth", action="store_true")
    args = parser.parse_args()
    dataset = ITBenchLiteDataset.open(args.root)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    blind_path = OUT_DIR / "dev-blind.json"
    seal_path = OUT_DIR / "dev-blind.seal.json"
    overlay_path = OUT_DIR / "dev-gt-overlay.json"
    if args.blind:
        payload = _blind_payload(dataset)
        blind_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        seal = {
            "dataset_revision": ITBENCH_DATASET_REVISION,
            "scenario_ids": DEV_SCENARIOS,
            "rule_ids": RULE_IDS,
            "blind_artifact_sha256": _sha(blind_path),
            "repository_commit": _repo_sha(),
            "ground_truth_loaded": False,
        }
        seal_path.write_text(json.dumps(seal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _print_blind(payload)
        print(f"blind audit SHA256: {_sha(blind_path)}")
        return 0
    if not blind_path.exists() or not seal_path.exists():
        raise SystemExit("blind audit and seal must exist before loading ground truth")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if (
        seal.get("ground_truth_loaded") is not False
        or seal.get("blind_artifact_sha256") != _sha(blind_path)
        or tuple(seal.get("scenario_ids", ())) != DEV_SCENARIOS
        or tuple(seal.get("rule_ids", ())) != RULE_IDS
        or seal.get("dataset_revision") != ITBENCH_DATASET_REVISION
        or seal.get("repository_commit") != _repo_sha()
    ):
        raise SystemExit("blind subsumption seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if (
        tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS
        or tuple(blind.get("rule_ids", ())) != RULE_IDS
    ):
        raise SystemExit("blind artifact rule/scenario set is invalid")
    if blind.get("ground_truth_loaded") is not False:
        raise SystemExit("blind artifact is not marked ground_truth_loaded=false")
    forbidden = forbidden_blind_keys(blind)
    if forbidden:
        raise SystemExit(f"forbidden blind fields: {forbidden}")
    overlay = _overlay(dataset, cast(dict[str, Any], blind))
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_overlay(overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
