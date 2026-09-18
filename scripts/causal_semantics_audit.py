#!/usr/bin/env python3
"""Run the ground-truth-separated P2A causal semantics lab."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from packages.evals.causal_semantics import audit_source
from packages.evals.itbench.contracts import ITBenchEntity
from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.itbench.grader import entity_matches_group
from packages.evals.itbench.source import SnapshotSource

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
OUT_DIR = Path(".local/diagnostics/causal-semantics")


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


def _gt_stage(audit: dict[str, Any], ground_truth: Any) -> tuple[str, int]:
    finding_present = any(_matches_root(item, ground_truth) for item in audit["finding_entities"])
    candidate_ids = {
        item["entity"]
        for item in audit["candidate_snapshots"]
        if _matches_root(item["entity"], ground_truth)
    }
    matching_hypotheses = [
        item
        for item in audit["hypothesis_snapshots"]
        if _matches_root(item["causal_actor"], ground_truth)
        or any(_matches_root(member, ground_truth) for member in item["members"])
    ]
    if not finding_present:
        return "GT_NOT_REPRESENTED_IN_FINDINGS", 0
    if not candidate_ids:
        return "GT_FINDING_PRESENT_CANDIDATE_ABSENT", 0
    if not matching_hypotheses:
        return "GT_CANDIDATE_PRESENT_HYPOTHESIS_ABSENT", 0
    if len(matching_hypotheses) > 1:
        return "GT_MATCHES_MULTIPLE_HYPOTHESIS_EPISODES", len(matching_hypotheses)
    match = matching_hypotheses[0]
    if match["is_leading"]:
        if audit["resolution"] == "RESOLVED":
            return "GT_HYPOTHESIS_UNIQUELY_RESOLVED", 1
        return "GT_HYPOTHESIS_LEADING_AMBIGUOUS", 1
    if match["plausible"]:
        return "GT_HYPOTHESIS_PLAUSIBLE_NOT_LEADING", 1
    return "GT_HYPOTHESIS_ELIMINATED", 1


def _gt_matching_hypotheses(audit: dict[str, Any], ground_truth: Any) -> set[str]:
    return {
        item["hypothesis_id"]
        for item in audit["hypothesis_snapshots"]
        if _matches_root(item["causal_actor"], ground_truth)
        or any(_matches_root(member, ground_truth) for member in item["members"])
    }


def _signal_usefulness(audit: dict[str, Any], ground_truth: Any) -> dict[str, dict[str, int | str]]:
    matching = _gt_matching_hypotheses(audit, ground_truth)
    stats: dict[str, dict[str, int | str]] = {}
    for pair in audit["competitions"]:
        if not pair["both_leading"]:
            continue
        left_match = pair["left_hypothesis_id"] in matching
        right_match = pair["right_hypothesis_id"] in matching
        if not left_match and not right_match:
            continue
        snapshots = {item["hypothesis_id"]: item for item in audit["hypothesis_snapshots"]}
        for signal in (
            "EXACT_TEMPORAL_ORDER_AVAILABLE",
            "VERIFICATION_DECISION_DIFFERENCE",
            "CAUSAL_PATH_SHAPE_DIFFERENCE",
            "PROVENANCE_CLASS_DIFFERENCE",
        ):
            if signal not in pair["diagnostic_signals"]:
                continue
            entry = stats.setdefault(
                signal,
                {"present": 0, "aligned": 0, "misaligned": 0, "neutral": 0},
            )
            entry["present"] = int(entry["present"]) + 1
            if left_match != right_match:
                if signal == "EXACT_TEMPORAL_ORDER_AVAILABLE":
                    left = pair["left_earliest_initiating_delta"]
                    right = pair["right_earliest_initiating_delta"]
                    gt_delta = left if left_match else right
                    other_delta = right if left_match else left
                    aligned = (
                        gt_delta is not None and other_delta is not None and gt_delta < other_delta
                    )
                elif signal == "VERIFICATION_DECISION_DIFFERENCE":
                    gt_id = (
                        pair["left_hypothesis_id"] if left_match else pair["right_hypothesis_id"]
                    )
                    other_id = (
                        pair["right_hypothesis_id"] if left_match else pair["left_hypothesis_id"]
                    )
                    aligned = (
                        snapshots[gt_id]["verification_decision"] == "VERIFIED"
                        and snapshots[other_id]["verification_decision"] != "VERIFIED"
                    )
                else:
                    entry["neutral"] = int(entry["neutral"]) + 1
                    continue
                entry["aligned" if aligned else "misaligned"] = (
                    int(entry["aligned" if aligned else "misaligned"]) + 1
                )
            else:
                entry["neutral"] = int(entry["neutral"]) + 1
    for entry in stats.values():
        entry["support_status"] = (
            "SUPPORTED_FOR_P2B"
            if int(entry["aligned"]) > int(entry["misaligned"])
            else "INCONSISTENT"
            if int(entry["misaligned"]) > int(entry["aligned"])
            else "INSUFFICIENT_DEV_SUPPORT"
        )
    return dict(sorted(stats.items()))


def _print_blind(audits: list[dict[str, Any]]) -> None:
    print(
        "Scenario | Resolution | Findings | Candidates | Hypotheses | Plausible | Leading | Primary ambiguity"
    )
    print("---|---|---:|---:|---:|---:|---:|---")
    for item in audits:
        print(
            f"{item['incident_id']} | {item['resolution']} | {item['finding_count']} | "
            f"{item['candidate_count']} | {item['hypothesis_count']} | "
            f"{len(item['plausible_hypothesis_ids'])} | {len(item['leading_hypothesis_ids'])} | "
            f"{item['primary_ambiguity_mechanism'] or '-'}"
        )
        for pair in item["competitions"]:
            if not pair["both_leading"]:
                continue
            left = next(
                h
                for h in item["hypothesis_snapshots"]
                if h["hypothesis_id"] == pair["left_hypothesis_id"]
            )
            right = next(
                h
                for h in item["hypothesis_snapshots"]
                if h["hypothesis_id"] == pair["right_hypothesis_id"]
            )
            print(
                f"  leading pair: {left['causal_actor']} ↔ {right['causal_actor']}; "
                f"mechanism={pair['primary_mechanism']}; signals={pair['diagnostic_signals']}; "
                f"signature_equal={pair['exact_signature_equal']}; "
                f"projection={pair['actor_projection_relation']}; "
                f"deltas={pair['left_earliest_initiating_delta']}/{pair['right_earliest_initiating_delta']}"
            )


def _run_blind(dataset: ITBenchLiteDataset, scenarios: tuple[str, ...]) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for scenario_id in scenarios:
        audit = audit_source(SnapshotSource(dataset.scenario(scenario_id)))
        audits.append(audit.as_dict())
    return audits


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
        audits = _run_blind(dataset, DEV_SCENARIOS)
        payload = {
            "dataset_revision": ITBENCH_DATASET_REVISION,
            "scenario_ids": DEV_SCENARIOS,
            "audits": audits,
        }
        blind_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        seal = {
            "dataset_revision": ITBENCH_DATASET_REVISION,
            "scenario_ids": DEV_SCENARIOS,
            "audit_file_sha256": _sha(blind_path),
            "repository_commit": _repo_sha(),
            "ground_truth_loaded": False,
        }
        seal_path.write_text(json.dumps(seal, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _print_blind(audits)
        print(f"blind audit SHA256: {_sha(blind_path)}")
        return 0

    if not blind_path.exists() or not seal_path.exists():
        raise SystemExit("blind audit and seal must exist before loading ground truth")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if seal.get("ground_truth_loaded") is not False or seal.get("audit_file_sha256") != _sha(
        blind_path
    ):
        raise SystemExit("blind audit seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS:
        raise SystemExit("blind audit scenario set is not the DEV set")
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    signal_counts: dict[str, dict[str, int | str]] = {}
    for audit in blind["audits"]:
        ground_truth = dataset.load_ground_truth(audit["incident_id"])
        stage, episodes = _gt_stage(audit, ground_truth)
        for signal, values in _signal_usefulness(audit, ground_truth).items():
            target = signal_counts.setdefault(
                signal,
                {"present": 0, "aligned": 0, "misaligned": 0, "neutral": 0},
            )
            for key in ("present", "aligned", "misaligned", "neutral"):
                target[key] = int(target[key]) + int(values[key])
        counts[stage] = counts.get(stage, 0) + 1
        rows.append(
            {
                "scenario_id": audit["incident_id"],
                "blind_primary_ambiguity_mechanism": audit["primary_ambiguity_mechanism"],
                "gt_stage": stage,
                "matching_hypothesis_episodes": episodes,
            }
        )
    overlay = {
        "blind_audit_sha256": _sha(blind_path),
        "ground_truth_loaded_after_seal": True,
        "rows": rows,
        "stage_counts": dict(sorted(counts.items())),
        "signal_usefulness": signal_counts,
    }
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Scenario | Blind mechanism | GT stage | Matching hypothesis episodes")
    print("---|---|---|---:")
    for row in rows:
        print(
            f"{row['scenario_id']} | {row['blind_primary_ambiguity_mechanism'] or '-'} | "
            f"{row['gt_stage']} | {row['matching_hypothesis_episodes']}"
        )
    print(f"GT stage counts: {dict(sorted(counts.items()))}")
    print(f"Signal usefulness: {dict(sorted(signal_counts.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
