#!/usr/bin/env python3
"""Run the ground-truth-separated P2D-0 temporal plausibility audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset
from packages.evals.temporal_plausibility import (
    MODEL_IDS,
    TemporalPlausibilityBlindAudit,
    blind_audit_from_dict,
    build_temporal_audit,
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
OUT_DIR = Path(".local/diagnostics/temporal-plausibility")
_FORBIDDEN = frozenset({"ground_truth", "expected", "correct", "root_cause", "answer", "label"})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


def _forbidden(value: Any, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            text = str(key).casefold()
            if text in _FORBIDDEN or text.startswith("gt_"):
                found.append(".".join((*path, str(key))))
            found.extend(_forbidden(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden(child, (*path, str(index))))
    return tuple(sorted(found))


def _blind_payload(dataset: ITBenchLiteDataset) -> dict[str, object]:
    from packages.evals.itbench.source import SnapshotSource
    from packages.rca.engine import build_case, diagnose_case

    audits: list[dict[str, object]] = []
    grace_values: set[float] = set()
    for scenario_id in DEV_SCENARIOS:
        source = SnapshotSource(dataset.scenario(scenario_id))
        case = build_case(source)
        diagnosis = diagnose_case(case)
        audit = build_temporal_audit(case, diagnosis)
        audits.append(audit.as_dict())
        grace_values.add(audit.verification_onset_grace_seconds)
    if len(grace_values) != 1:
        raise RuntimeError("DEV temporal audits used inconsistent onset grace values")
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "counterfactual_model_ids": MODEL_IDS,
        "production_onset_grace_seconds": next(iter(grace_values)),
        "audits": audits,
    }


def _entity(value: str) -> Any:
    from packages.evals.itbench.contracts import ITBenchEntity

    namespace, kind, name = value.split("/", 2)
    return ITBenchEntity(namespace=namespace, kind=kind, name=name)


def _matches_root(entity: str, ground_truth: Any) -> bool:
    from packages.evals.itbench.grader import entity_matches_group

    candidate = _entity(entity)
    roots = [group for group in ground_truth.root_cause_groups if group.root_cause]
    groups = {group.group_id: group for group in ground_truth.root_cause_groups}
    root_ids = {group.group_id for group in roots}
    aliases: dict[str, str] = {}
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


def _matching_hypotheses(audit: TemporalPlausibilityBlindAudit, ground_truth: Any) -> set[str]:
    return {
        item.hypothesis_id
        for item in audit.hypothesis_temporal_audits
        if _matches_root(item.causal_actor, ground_truth)
        or any(_matches_root(member, ground_truth) for member in item.members)
    }


def _model(audit: TemporalPlausibilityBlindAudit, model_id: str) -> Any:
    return next(item for item in audit.counterfactuals if item.model_id == model_id)


def _gt_status(audit: TemporalPlausibilityBlindAudit, model_id: str, matching: set[str]) -> str:
    if len(matching) > 1:
        return "GT_MATCHES_MULTIPLE_EPISODES"
    if not matching:
        return "GT_NOT_REPRESENTED"
    result = _model(audit, model_id)
    if next(iter(matching)) in result.supported_hypothesis_ids:
        return "GT_SUPPORTED"
    if next(iter(matching)) in result.unresolved_hypothesis_ids:
        return "GT_UNRESOLVED"
    return "GT_CONTRADICTED"


def _safety_effect(
    audit: TemporalPlausibilityBlindAudit, model_id: str, matching: set[str]
) -> tuple[str, ...]:
    model = _model(audit, model_id)
    effects: list[str] = []
    current_gt = _gt_status(audit, "E0_CURRENT", matching)
    model_gt = _gt_status(audit, model_id, matching)
    if current_gt == "GT_CONTRADICTED" and audit.production_resolution == "RESOLVED":
        if model.terminal_state != "RESOLVED":
            effects.append("WRONG_RESOLUTION_PREVENTED")
        if model_gt == "GT_UNRESOLVED":
            effects.append("GT_EPISODE_RESTORED_TO_UNRESOLVED")
        elif model_gt == "GT_SUPPORTED":
            effects.append("GT_EPISODE_RESTORED_TO_SUPPORTED")
        elif model_gt == "GT_CONTRADICTED":
            effects.append("WRONG_RESOLUTION_STILL_PRESENT")
    if not matching:
        effects.append("GT_NOT_REPRESENTED_NO_EFFECT")
    if current_gt == "GT_SUPPORTED" and audit.production_resolution == "RESOLVED":
        if model.terminal_state == "RESOLVED":
            effects.append("CORRECT_RESOLUTION_PRESERVED")
        elif model.terminal_state == "AMBIGUOUS":
            effects.append("NEW_AMBIGUITY_FROM_PREVIOUSLY_CORRECT_RESOLUTION")
        else:
            effects.append("NEW_INSUFFICIENT_FROM_PREVIOUSLY_CORRECT_RESOLUTION")
    if not effects:
        effects.append("NO_CHANGE")
    return tuple(effects)


def _p2c_reconciliation() -> tuple[bool | None, dict[str, int]]:
    path = Path(".local/diagnostics/causal-completeness/dev-gt-overlay.json")
    if not path.exists():
        return None, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts: Counter[str] = Counter(
        journey["final_stage"]
        for scenario in payload["scenarios"]
        for journey in scenario["root_journeys"]
    )
    expected = {
        "LEADING_AMBIGUOUS": 5,
        "PLAUSIBILITY_ELIMINATION": 3,
        "UNIQUELY_RESOLVED": 1,
        "SNAPSHOT_ENTITY_NOT_OBSERVABLE": 1,
    }
    return dict(counts) == expected, dict(sorted(counts.items()))


def _overlay(dataset: ITBenchLiteDataset, blind: dict[str, Any]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for raw in blind["audits"]:
        audit = blind_audit_from_dict(raw)
        ground_truth = dataset.load_ground_truth(audit.incident_id)
        matching = _matching_hypotheses(audit, ground_truth)
        rows.append(
            {
                "scenario_id": audit.incident_id,
                "current_resolution": audit.production_resolution,
                "gt_current": _gt_status(audit, "E0_CURRENT", matching),
                "gt_representation": (
                    "GT_MATCHES_MULTIPLE_EPISODES"
                    if len(matching) > 1
                    else "GT_NOT_REPRESENTED"
                    if not matching
                    else "GT_REPRESENTED"
                ),
                "models": {
                    model_id: {
                        "terminal_state": _model(audit, model_id).terminal_state,
                        "gt_status": _gt_status(audit, model_id, matching),
                        "effects": _safety_effect(audit, model_id, matching),
                    }
                    for model_id in MODEL_IDS
                },
            }
        )
    p2c_reconciled, p2c_counts = _p2c_reconciliation()
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "ground_truth_loaded_after_seal": True,
        "rows": rows,
        "p2c1_stage_distribution_reconciled": p2c_reconciled,
        "p2c1_stage_distribution": p2c_counts,
    }


def _print_blind(payload: dict[str, object]) -> None:
    print(
        "Scenario | Production | Hypotheses | Temporal contradictions | Interval uncertain | Missing initiating | E0 | E1 | E2 | E3"
    )
    print("---|---|---:|---:|---:|---:|---|---|---|---")
    for raw in cast(list[dict[str, Any]], payload["audits"]):
        audit = blind_audit_from_dict(raw)
        contradictions = sum(
            bool(item.temporal_contradiction_certainties)
            for item in audit.hypothesis_temporal_audits
        )
        uncertain = sum(
            "OBSERVATION_INTERVAL_UNCERTAIN" in item.temporal_contradiction_certainties
            for item in audit.hypothesis_temporal_audits
        )
        missing = sum(
            item.initiating_gap_category is not None for item in audit.hypothesis_temporal_audits
        )
        states = [_model(audit, model).terminal_state for model in MODEL_IDS]
        print(
            f"{audit.incident_id} | {audit.production_resolution} | {len(audit.hypothesis_temporal_audits)} | {contradictions} | {uncertain} | {missing} | "
            + " | ".join(states)
        )
        for item in audit.hypothesis_temporal_audits:
            if item.production_reason_codes:
                print(
                    f"  {item.causal_actor} | {item.production_reason_codes} | "
                    f"{item.initiating_gap_category} | {item.temporal_contradiction_certainties}"
                )


def _print_overlay(payload: dict[str, object]) -> None:
    print("Scenario | Current | E1 | E2 | E3 | GT current | GT E1 | GT E2 | GT E3 | Safety effect")
    print("---|---|---|---|---|---|---|---|---|---")
    for row in cast(list[dict[str, Any]], payload["rows"]):
        models = row["models"]
        print(
            f"{row['scenario_id']} | {row['current_resolution']} | "
            + " | ".join(models[model]["terminal_state"] for model in MODEL_IDS[1:])
            + f" | {row['gt_current']} | "
            + " | ".join(models[model]["gt_status"] for model in MODEL_IDS[1:])
            + " | "
            + "; ".join(f"{model}:{','.join(models[model]['effects'])}" for model in MODEL_IDS[1:])
        )
    print(f"P2C.1 stage distribution reconciled: {payload['p2c1_stage_distribution_reconciled']}")


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
        forbidden = _forbidden(payload)
        if forbidden:
            raise SystemExit(f"forbidden blind fields: {forbidden}")
        blind_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        seal = {
            "dataset_revision": ITBENCH_DATASET_REVISION,
            "scenario_ids": DEV_SCENARIOS,
            "blind_artifact_sha256": _sha(blind_path),
            "repository_commit": _repo_sha(),
            "ground_truth_loaded": False,
            "counterfactual_model_ids": MODEL_IDS,
            "production_onset_grace_seconds": payload["production_onset_grace_seconds"],
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
        or seal.get("dataset_revision") != ITBENCH_DATASET_REVISION
        or seal.get("repository_commit") != _repo_sha()
        or tuple(seal.get("counterfactual_model_ids", ())) != MODEL_IDS
    ):
        raise SystemExit("blind temporal audit seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS:
        raise SystemExit("blind temporal audit scenario set is not DEV")
    if tuple(blind.get("counterfactual_model_ids", ())) != MODEL_IDS:
        raise SystemExit("blind temporal audit model set is not frozen")
    if _forbidden(blind):
        raise SystemExit("blind temporal audit contains forbidden fields")
    overlay = _overlay(dataset, blind)
    overlay["blind_artifact_sha256"] = _sha(blind_path)
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_overlay(overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
