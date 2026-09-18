#!/usr/bin/env python3
"""Audit unresolved-episode reachability without using a model or TEST."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.unresolved_reachability import (
    UnresolvedReachabilityBlindAudit,
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
OUT_DIR = Path(".local/diagnostics/unresolved-reachability")
QUERY_TEMPLATES = ("full-history", "onset+-5m", "onset+-15m", "incident-window")
MAX_ROUNDS = 16
MAX_OBSERVATIONS = 4096


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


def _entity(value: str) -> Any:
    from packages.evals.itbench.contracts import ITBenchEntity

    namespace, kind, name = value.split("/", 2)
    return ITBenchEntity(namespace=namespace, kind=kind, name=name)


def _matches_root(entity: str, ground_truth: Any, root_group_id: str) -> bool:
    from packages.evals.itbench.grader import entity_matches_group

    candidate = _entity(entity)
    groups = {group.group_id: group for group in ground_truth.root_cause_groups}
    root = groups.get(root_group_id)
    if root is None or not root.root_cause:
        return False
    if entity_matches_group(candidate, root):
        return True
    root_ids = {group.group_id for group in ground_truth.root_cause_groups if group.root_cause}
    for alias in ground_truth.aliases:
        matches = sorted(root_ids.intersection(alias))
        if root_group_id not in matches:
            continue
        for alias_id in alias:
            if alias_id != root_group_id and alias_id in groups:
                if entity_matches_group(candidate, groups[alias_id]):
                    return True
    return False


def _root_ids(ground_truth: Any) -> tuple[str, ...]:
    return tuple(
        sorted(item.group_id for item in ground_truth.root_cause_groups if item.root_cause)
    )


def _matching_episodes(
    audit: UnresolvedReachabilityBlindAudit, root: str, gt: Any, which: str
) -> tuple[str, ...]:
    snapshot = getattr(audit, which)
    return tuple(
        sorted(
            item.hypothesis_id
            for item in snapshot.episodes
            if _matches_root(item.actor, gt, root)
            or any(_matches_root(member, gt, root) for member in item.members)
        )
    )


def _state_for(snapshot: Any, ids: tuple[str, ...]) -> str:
    if len(ids) > 1:
        return "GT_FULL_MULTI_EPISODE"
    if not ids:
        return "GT_FULL_NOT_REPRESENTED"
    item = next(item for item in snapshot.episodes if item.hypothesis_id == ids[0])
    return {
        "SUPPORTED": "GT_FULL_SUPPORTED",
        "UNRESOLVED": "GT_FULL_UNRESOLVED",
        "CONTRADICTED": "GT_FULL_CONTRADICTED",
    }.get(item.epistemic_state, "GT_FULL_NOT_REPRESENTED")


def _scenario_potential(audit: UnresolvedReachabilityBlindAudit, gt: Any) -> str:
    root_ids = _root_ids(gt)
    full_ids = tuple(
        identifier
        for root_id in root_ids
        for identifier in _matching_episodes(audit, root_id, gt, "full")
    )
    if not full_ids:
        return "CAUSAL_MODEL_ABSENCE"
    full_gt_states = {
        next(
            item for item in audit.full.episodes if item.hypothesis_id == identifier
        ).epistemic_state
        for identifier in full_ids
    }
    if "UNRESOLVED" in full_gt_states:
        return "CURRENT_FULL_SEMANTIC_CEILING"
    for root_id in root_ids:
        full_root_ids = _matching_episodes(audit, root_id, gt, "full")
        active_root_ids = _matching_episodes(audit, root_id, gt, "exhaustive_active")
        full_states = {
            next(
                item for item in audit.full.episodes if item.hypothesis_id == identifier
            ).epistemic_state
            for identifier in full_root_ids
        }
        active_states = {
            next(
                item
                for item in audit.exhaustive_active.episodes
                if item.hypothesis_id == identifier
            ).epistemic_state
            for identifier in active_root_ids
        }
        if full_states.intersection({"SUPPORTED", "CONTRADICTED"}) and active_states != full_states:
            return "ACTIVE_PARITY_BLOCKS_REACHABILITY"
    if audit.full.resolution == "RESOLVED":
        return "ACQUISITION_CAN_ENABLE_SAFE_RESOLUTION"
    parity = [
        item
        for item in audit.episode_reachability
        if item.full_state in {"SUPPORTED", "CONTRADICTED"}
    ]
    if any(item.active_state != item.full_state for item in parity):
        return "ACTIVE_PARITY_BLOCKS_REACHABILITY"
    if any(
        item.reachability_class.startswith("ACTIVE_REACHES_FULL")
        for item in audit.episode_reachability
    ):
        return "ACQUISITION_CAN_IMPROVE_STATE_BUT_NOT_RESOLVE"
    return "ACQUISITION_CAN_IMPROVE_STATE_BUT_NOT_RESOLVE"


def _blind_payload(dataset: ITBenchLiteDataset) -> dict[str, object]:
    audits: dict[str, object] = {}
    for scenario_id in DEV_SCENARIOS:
        from packages.evals.itbench.source import SnapshotSource

        audit = build_blind_audit(
            SnapshotSource(dataset.scenario(scenario_id)),
            max_rounds=MAX_ROUNDS,
            max_observations=MAX_OBSERVATIONS,
        )
        if not audit.exhaustive_fixed_point:
            raise RuntimeError(
                f"exhaustive active closure did not reach a true fixed point: {scenario_id}"
            )
        unexplained = tuple(
            item
            for item in audit.episode_reachability
            if item.reachability_class == "ACTIVE_EXCEEDS_FULL_CONTROL"
        )
        if unexplained:
            raise RuntimeError(
                f"active control exceeds full control without an explicit divergence: {scenario_id}"
            )
        audits[scenario_id] = audit.as_dict()
    payload: dict[str, object] = {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "query_template_ids": QUERY_TEMPLATES,
        "fixed_point_bounds": {"max_rounds": MAX_ROUNDS, "max_observations": MAX_OBSERVATIONS},
        "ground_truth_loaded": False,
        "audits": audits,
    }
    forbidden = forbidden_blind_keys(payload)
    if forbidden:
        raise RuntimeError(f"forbidden blind fields: {forbidden}")
    return payload


def _overlay(dataset: ITBenchLiteDataset, blind: dict[str, Any]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(blind["audits"][scenario_id])
        gt = dataset.load_ground_truth(scenario_id)
        journeys: list[dict[str, object]] = []
        for root_id in _root_ids(gt):
            seed_ids = _matching_episodes(audit, root_id, gt, "seed")
            active_ids = _matching_episodes(audit, root_id, gt, "exhaustive_active")
            full_ids = _matching_episodes(audit, root_id, gt, "full")
            journeys.append(
                {
                    "root_group_id": root_id,
                    "seed_state": _state_for(audit.seed, seed_ids),
                    "active_state": _state_for(audit.exhaustive_active, active_ids),
                    "full_state": _state_for(audit.full, full_ids),
                    "seed_episode_ids": seed_ids,
                    "active_episode_ids": active_ids,
                    "full_episode_ids": full_ids,
                }
            )
        rows.append(
            {
                "scenario_id": scenario_id,
                "full_resolution": audit.full.resolution,
                "potential": _scenario_potential(audit, gt),
                "journeys": journeys,
            }
        )
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "ground_truth_loaded_after_seal": True,
        "blind_artifact_sha256": _sha(OUT_DIR / "dev-blind.json"),
        "rows": rows,
    }


def _print_blind(payload: dict[str, object]) -> None:
    print(
        "Scenario | Full resolution | Supported | Unresolved | Contradicted | Seed unresolved | Active unresolved | Fixed point"
    )
    print("---|---|---|---|---|---:|---:|---")
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], payload["audits"])[scenario_id])
        print(
            f"{scenario_id} | {audit.full.resolution} | {audit.full.supported_actors} | "
            f"{audit.full.unresolved_actors} | {audit.full.contradicted_actors} | "
            f"{len(audit.seed.unresolved_actors)} | {len(audit.exhaustive_active.unresolved_actors)} | "
            f"{audit.exhaustive_fixed_point}"
        )
    print("\nCapability | Queries | Novel raw | Findings | U→S | U→C | No state change")
    print("---|---:|---:|---:|---:|---:|---:|")
    effects = [
        effect
        for raw in cast(dict[str, Any], payload["audits"]).values()
        for effect in blind_audit_from_dict(raw).query_effects
    ]
    for capability in sorted({item.capability for item in effects}):
        capability_values = [item for item in effects if item.capability == capability]
        print(
            f"{capability} | {len(capability_values)} | {sum(bool(item.new_refs) for item in capability_values)} | "
            f"{sum(bool(item.new_findings) for item in capability_values)} | "
            f"{sum('UNRESOLVED_TO_SUPPORTED' in item.transitions for item in capability_values)} | "
            f"{sum('UNRESOLVED_TO_CONTRADICTED' in item.transitions for item in capability_values)} | "
            f"{sum(not item.transitions or item.effect_class == 'FINDING_NO_EPISTEMIC_EFFECT' for item in capability_values)}"
        )
    print("\nScenario | Actor | Seed | Active fixed point | Full | Reachability class")
    print("---|---|---|---|---|---")
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], payload["audits"])[scenario_id])
        for item in audit.episode_reachability:
            print(
                f"{scenario_id} | {item.actor} | {item.seed_state or '—'} | "
                f"{item.active_state or '—'} | {item.full_state or '—'} | {item.reachability_class}"
            )
    dimensions: dict[str, list[Any]] = {}
    for effect in effects:
        dimension_values = dimensions.setdefault(effect.dimension, [0, 0, 0, 0])
        dimension_values[0] += 1
        dimension_values[1] += bool(effect.new_refs)
        dimension_values[2] += "UNRESOLVED_TO_SUPPORTED" in effect.transitions
        dimension_values[3] += "UNRESOLVED_TO_CONTRADICTED" in effect.transitions
    print("\nDimension | Queries | Novel raw | U→S | U→C")
    print("---|---:|---:|---:|---:|")
    for dimension in sorted(dimensions):
        queries, novel, to_supported, to_contradicted = dimensions[dimension]
        print(f"{dimension} | {queries} | {novel} | {to_supported} | {to_contradicted}")
    print("\nScenario | Policy context | Missing gap hypotheses | Full-only Findings")
    print("---|---|---:|---:|")
    for scenario_id in DEV_SCENARIOS:
        audit = blind_audit_from_dict(cast(dict[str, Any], payload["audits"])[scenario_id])
        print(
            f"{scenario_id} | {audit.policy_context_audit.classification} | "
            f"{len(audit.policy_context_audit.missing_hypothesis_ids)} | "
            f"{len(audit.parity_audit.full_only_findings)}"
        )


def _print_overlay(payload: dict[str, object]) -> None:
    print(
        "Scenario | GT representation in FULL | GT FULL state | GT ACTIVE state | GT SEED state | Potential"
    )
    print("---|---|---|---|---|---")
    for row in cast(list[dict[str, Any]], payload["rows"]):
        for journey in row["journeys"]:
            print(
                f"{row['scenario_id']} | {journey['root_group_id']} | {journey['full_state']} | "
                f"{journey['active_state']} | {journey['seed_state']} | {row['potential']}"
            )


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
            "query_template_ids": QUERY_TEMPLATES,
            "fixed_point_bounds": {"max_rounds": MAX_ROUNDS, "max_observations": MAX_OBSERVATIONS},
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
        or tuple(seal.get("query_template_ids", ())) != QUERY_TEMPLATES
        or seal.get("dataset_revision") != ITBENCH_DATASET_REVISION
        or seal.get("repository_commit") != _repo_sha()
    ):
        raise SystemExit("blind reachability seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS:
        raise SystemExit("blind scenario set is not DEV")
    if blind.get("ground_truth_loaded") is not False:
        raise SystemExit("blind artifact is not marked ground_truth_loaded=false")
    if forbidden_blind_keys(blind):
        raise SystemExit(f"forbidden blind fields: {forbidden_blind_keys(blind)}")
    overlay = _overlay(dataset, cast(dict[str, Any], blind))
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_overlay(overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
