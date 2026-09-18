#!/usr/bin/env python3
"""Run the ground-truth-separated P2B-0 verification safety audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.contracts import ITBenchEntity
from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.itbench.grader import entity_matches_group
from packages.evals.itbench.source import SnapshotSource
from packages.evals.verification_discriminator import (
    CANDIDATE_RULE_IDS,
    audit_case,
)
from packages.rca.engine import build_case, diagnose_case

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
OUT_DIR = Path(".local/diagnostics/verification-discriminator")
_UNSAFE = {"MISALIGNED_GT_EPISODE", "GT_NOT_REPRESENTED_IN_ANY_HYPOTHESIS"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


def _entity(value: str) -> ITBenchEntity:
    namespace, kind, name = value.split("/", 2)
    return ITBenchEntity(namespace=namespace, kind=kind, name=name)


def _matches_root(entity: str, ground_truth: Any) -> bool:
    candidate = _entity(entity)
    roots = [group for group in ground_truth.root_cause_groups if group.root_cause]
    groups = {group.group_id: group for group in ground_truth.root_cause_groups}
    aliases: dict[str, str] = {}
    root_ids = {group.group_id for group in roots}
    for alias in ground_truth.aliases:
        matching = sorted(root_ids.intersection(alias))
        for group_id in alias:
            if matching:
                aliases[group_id] = matching[0]
    for root in roots:
        if entity_matches_group(candidate, root):
            return True
        for alias_id, root_id in aliases.items():
            if root_id == root.group_id and alias_id in groups:
                if entity_matches_group(candidate, groups[alias_id]):
                    return True
    return False


def _gt_hypothesis_ids(audit: dict[str, Any], ground_truth: Any) -> set[str]:
    return {
        item["hypothesis_id"]
        for item in audit["hypothesis_verifications"]
        if _matches_root(item["causal_actor"], ground_truth)
        or any(_matches_root(member, ground_truth) for member in item["members"])
    }


def _gt_status(
    selected_id: str | None,
    matching_ids: set[str],
) -> str:
    if len(matching_ids) > 1:
        return "GT_MATCHES_MULTIPLE_HYPOTHESES"
    if not matching_ids:
        return "GT_NOT_REPRESENTED_IN_ANY_HYPOTHESIS"
    if selected_id in matching_ids:
        return "ALIGNED_GT_EPISODE"
    return "MISALIGNED_GT_EPISODE"


def _audit_payload(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    audits: list[dict[str, object]] = []
    for scenario_id in DEV_SCENARIOS:
        source = SnapshotSource(dataset.scenario(scenario_id))
        case = build_case(source)
        audit = audit_case(case, diagnose_case(case))
        audits.append(audit.as_dict())
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "candidate_rule_ids": CANDIDATE_RULE_IDS,
        "audits": audits,
    }


def _rule_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {
        rule_id: {
            "triggered": 0,
            "aligned": 0,
            "misaligned": 0,
            "gt_not_represented_trigger": 0,
            "gt_multiple_episode_trigger": 0,
            "unsafe": 0,
            "abstained": 0,
        }
        for rule_id in CANDIDATE_RULE_IDS
    }
    for row in rows:
        for result in row["rule_results"]:
            values = counts[result["rule_id"]]
            if not result["triggered"]:
                values["abstained"] += 1
                continue
            values["triggered"] += 1
            status = result["gt_status"]
            if status == "ALIGNED_GT_EPISODE":
                values["aligned"] += 1
            elif status == "MISALIGNED_GT_EPISODE":
                values["misaligned"] += 1
            elif status == "GT_NOT_REPRESENTED_IN_ANY_HYPOTHESIS":
                values["gt_not_represented_trigger"] += 1
            elif status == "GT_MATCHES_MULTIPLE_HYPOTHESES":
                values["gt_multiple_episode_trigger"] += 1
            if status in _UNSAFE:
                values["unsafe"] += 1
    return counts


def _overlay(dataset: ITBenchLiteDataset, blind: dict[str, Any]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for audit in blind["audits"]:
        ground_truth = dataset.load_ground_truth(audit["incident_id"])
        matching_ids = _gt_hypothesis_ids(audit, ground_truth)
        representation = (
            "GT_MATCHES_MULTIPLE_HYPOTHESES"
            if len(matching_ids) > 1
            else "GT_NOT_REPRESENTED_IN_ANY_HYPOTHESIS"
            if not matching_ids
            else "GT_REPRESENTED_IN_ONE_HYPOTHESIS"
        )
        rule_results: list[dict[str, object]] = []
        for result in audit["rule_results"]:
            status = _gt_status(result["selected_hypothesis_id"], matching_ids)
            item = dict(result)
            item["gt_status"] = status if result["triggered"] else None
            item["unsafe"] = bool(result["triggered"] and status in _UNSAFE)
            rule_results.append(item)
        rows.append(
            {
                "incident_id": audit["incident_id"],
                "current_resolution": audit["current_resolution"],
                "gt_representation_state": representation,
                "rule_results": rule_results,
            }
        )
    return {
        "blind_artifact_sha256": None,
        "ground_truth_loaded_after_seal": True,
        "candidate_rule_ids": CANDIDATE_RULE_IDS,
        "rows": rows,
        "rule_counts": _rule_counts(rows),
    }


def _print_blind(audits: list[dict[str, Any]]) -> None:
    print("Scenario | Current | V1 | V2 | V3")
    print("---|---|---|---|---")
    for audit in audits:
        results = {item["rule_id"]: item for item in audit["rule_results"]}
        values = []
        for rule_id in CANDIDATE_RULE_IDS:
            result = results[rule_id]
            values.append(
                f"TRIGGER:{result['selected_actor']}"
                if result["triggered"]
                else f"ABSTAIN:{result['abstention_reason']}"
            )
        print(f"{audit['incident_id']} | {audit['current_resolution']} | " + " | ".join(values))
        for snapshot in audit["hypothesis_verifications"]:
            profile = ", ".join(
                f"{item['name']}={item['status']}" for item in snapshot["predicates"]
            )
            print(f"  {snapshot['causal_actor']} | {snapshot['decision']} | {profile}")


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
        payload = _audit_payload(dataset)
        blind_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        seal = {
            "dataset_revision": ITBENCH_DATASET_REVISION,
            "scenario_ids": DEV_SCENARIOS,
            "audit_file_sha256": _sha(blind_path),
            "repository_commit": _repo_sha(),
            "ground_truth_loaded": False,
            "candidate_rule_ids": CANDIDATE_RULE_IDS,
        }
        seal_path.write_text(json.dumps(seal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _print_blind(payload["audits"])
        print(f"blind audit SHA256: {_sha(blind_path)}")
        return 0

    if not blind_path.exists() or not seal_path.exists():
        raise SystemExit("blind audit and seal must exist before loading ground truth")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if (
        seal.get("ground_truth_loaded") is not False
        or seal.get("audit_file_sha256") != _sha(blind_path)
        or tuple(seal.get("scenario_ids", ())) != DEV_SCENARIOS
        or tuple(seal.get("candidate_rule_ids", ())) != CANDIDATE_RULE_IDS
        or seal.get("repository_commit") != _repo_sha()
    ):
        raise SystemExit("blind audit seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS:
        raise SystemExit("blind audit scenario set is not the DEV set")
    if tuple(blind.get("candidate_rule_ids", ())) != CANDIDATE_RULE_IDS:
        raise SystemExit("blind audit rule set is not frozen")
    if any(
        key.casefold() in {"ground_truth", "expected", "correct", "root_cause_label"}
        or key.casefold().startswith("gt_")
        for audit in blind["audits"]
        for key in audit
    ):
        raise SystemExit("blind audit contains forbidden ground-truth fields")
    overlay = _overlay(dataset, blind)
    overlay["blind_artifact_sha256"] = _sha(blind_path)
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Scenario | Current | V1 | V2 | V3 | GT representation")
    print("---|---|---|---|---|---")
    for row in cast(list[dict[str, Any]], overlay["rows"]):
        values = []
        for result in row["rule_results"]:
            if result["triggered"]:
                values.append(
                    f"{result['rule_id']}:{result['gt_status']}:{result['selected_actor']}"
                )
            else:
                values.append(f"{result['rule_id']}:ABSTAIN:{result['abstention_reason']}")
        print(
            f"{row['incident_id']} | {row['current_resolution']} | "
            + " | ".join(values)
            + f" | {row['gt_representation_state']}"
        )
    print(f"rule counts: {overlay['rule_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
