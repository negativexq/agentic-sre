#!/usr/bin/env python3
"""Audit where DEV causal episodes are lost in the deterministic RCA funnel."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, cast

from packages.evals.causal_completeness import (
    CausalCompletenessBlindAudit,
    RootCauseJourney,
    ScenarioCompletenessOverlay,
    blind_audit_from_dict,
    build_blind_audit,
    classify_first_loss,
    extraction_diagnostic,
    forbidden_blind_keys,
    plausibility_diagnostic,
    snapshot_entity_inventory,
)
from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION, ITBenchLiteDataset

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
OUT_DIR = Path(".local/diagnostics/causal-completeness")
_STAGE_ORDER = {
    "SNAPSHOT_ENTITY_NOT_OBSERVABLE": 0,
    "SOURCE_ADAPTER_ENTITY_GAP": 1,
    "FINDING_EXTRACTION_GAP": 2,
    "FINDING_RELATED_ONLY": 2,
    "CANDIDATE_CREATION_GAP": 3,
    "HYPOTHESIS_GROUPING_GAP": 4,
    "PLAUSIBILITY_ELIMINATION": 5,
    "PLAUSIBLE_NOT_LEADING": 6,
    "LEADING_AMBIGUOUS": 7,
    "UNIQUELY_RESOLVED": 8,
    "MULTIPLE_MATCHING_HYPOTHESIS_EPISODES": 9,
}
_EXTRACTION_STAGES = {"FINDING_EXTRACTION_GAP", "FINDING_RELATED_ONLY"}


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
    roots = [
        group
        for group in ground_truth.root_cause_groups
        if group.root_cause and group.group_id == root_group_id
    ]
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


def _root_group_ids(ground_truth: Any) -> tuple[str, ...]:
    return tuple(
        sorted(group.group_id for group in ground_truth.root_cause_groups if group.root_cause)
    )


def _matching_entities(
    values: tuple[str, ...], ground_truth: Any, root_group_id: str
) -> tuple[str, ...]:
    return tuple(
        sorted(value for value in values if _matches_root(value, ground_truth, root_group_id))
    )


def _journeys_for_audit(
    audit: CausalCompletenessBlindAudit,
    ground_truth: Any,
) -> tuple[RootCauseJourney, ...]:
    snapshot_by_entity = {item.canonical: item for item in audit.snapshot_entities}
    findings = audit.findings
    candidates = audit.candidates
    hypotheses = audit.hypotheses
    eliminations = {item.hypothesis_id: item for item in audit.elimination_mechanics}
    journeys: list[RootCauseJourney] = []
    for root_group_id in _root_group_ids(ground_truth):
        snapshot_matches = _matching_entities(
            tuple(snapshot_by_entity), ground_truth, root_group_id
        )
        history_matches = _matching_entities(
            audit.source_exposure.history_entities, ground_truth, root_group_id
        )
        event_matches = _matching_entities(
            audit.source_exposure.event_entities, ground_truth, root_group_id
        )
        traffic_matches = _matching_entities(
            audit.source_exposure.traffic_entities, ground_truth, root_group_id
        )
        alert_matches = _matching_entities(
            tuple(
                f"_cluster/Service/{service}" for service in audit.source_exposure.alert_services
            ),
            ground_truth,
            root_group_id,
        )
        source_matches = tuple(
            sorted(
                set(history_matches)
                | set(event_matches)
                | set(traffic_matches)
                | set(alert_matches)
            )
        )
        source_origins = tuple(
            origin
            for origin, matches in (
                ("HISTORY", history_matches),
                ("EVENT", event_matches),
                ("TRAFFIC", traffic_matches),
                ("ALERT_SERVICE", alert_matches),
            )
            if matches
        )
        if set(source_origins).intersection({"HISTORY", "EVENT"}) and not snapshot_matches:
            raise RuntimeError(
                "AUDIT_INCONSISTENCY: structured SnapshotSource entity is absent "
                f"from the observable catalog for {root_group_id}"
            )
        direct = tuple(
            sorted(
                item.entity
                for item in findings
                if _matches_root(item.entity, ground_truth, root_group_id)
            )
        )
        related = tuple(
            sorted(
                item.finding_key
                for item in findings
                if any(
                    _matches_root(entity, ground_truth, root_group_id)
                    for entity in item.related_entities
                )
            )
        )
        candidate_matches = tuple(
            sorted(
                item.entity
                for item in candidates
                if _matches_root(item.entity, ground_truth, root_group_id)
            )
        )
        hypothesis_matches = tuple(
            sorted(
                item.hypothesis_id
                for item in hypotheses
                if _matches_root(item.causal_actor, ground_truth, root_group_id)
                or any(
                    _matches_root(member, ground_truth, root_group_id) for member in item.members
                )
            )
        )
        manifestation_matches = tuple(
            sorted(
                item.hypothesis_id
                for item in hypotheses
                if any(
                    _matches_root(entity, ground_truth, root_group_id)
                    for entity in item.manifestations
                )
            )
        )
        plausible = tuple(
            item.hypothesis_id
            for item in hypotheses
            if item.hypothesis_id in hypothesis_matches and item.plausible
        )
        leading = tuple(
            item.hypothesis_id
            for item in hypotheses
            if item.hypothesis_id in hypothesis_matches and item.leading
        )
        stage = classify_first_loss(
            snapshot_matches=snapshot_matches,
            source_matches=source_matches,
            direct_findings=direct,
            related_findings=related,
            candidate_matches=candidate_matches,
            hypothesis_matches=hypothesis_matches,
            plausible_matches=plausible,
            leading_matches=leading,
            resolution=audit.resolution,
        )
        matching_eliminations = tuple(
            eliminations[item] for item in hypothesis_matches if item in eliminations
        )
        extraction = None
        if stage in _EXTRACTION_STAGES:
            matched_snapshot = next(
                (
                    snapshot_by_entity[item]
                    for item in snapshot_matches
                    if item in snapshot_by_entity
                ),
                None,
            )
            extraction = extraction_diagnostic(matched_snapshot, audit.source_exposure)
        if stage == "FINDING_RELATED_ONLY":
            assert not direct
            assert related
        if stage == "SOURCE_ADAPTER_ENTITY_GAP":
            assert snapshot_matches
            assert not source_matches
        if stage == "FINDING_EXTRACTION_GAP":
            assert source_matches
            assert not direct
            assert not related
        if stage == "PLAUSIBILITY_ELIMINATION":
            assert hypothesis_matches
            assert not plausible
            assert matching_eliminations
        if stage == "LEADING_AMBIGUOUS":
            assert leading
            assert audit.resolution == "AMBIGUOUS"
        if stage == "UNIQUELY_RESOLVED":
            assert leading
            assert audit.resolution == "RESOLVED"
        journeys.append(
            RootCauseJourney(
                root_group_id=root_group_id,
                observable_snapshot_matches=snapshot_matches,
                source_exposed_matches=source_matches,
                direct_finding_matches=direct,
                related_finding_matches=related,
                candidate_matches=candidate_matches,
                hypothesis_matches=hypothesis_matches,
                plausible_hypothesis_matches=tuple(sorted(plausible)),
                leading_hypothesis_matches=tuple(sorted(leading)),
                first_loss_stage=stage,
                final_stage=stage,
                elimination_details=matching_eliminations,
                extraction_diagnostic=extraction,
                elimination_diagnostic=(
                    plausibility_diagnostic(
                        tuple(code for item in matching_eliminations for code in item.reason_codes)
                    )
                    if stage == "PLAUSIBILITY_ELIMINATION"
                    else None
                ),
                manifestation_matches=manifestation_matches,
                source_match_origins=source_origins,
            )
        )
    return tuple(journeys)


def _overlay(dataset: ITBenchLiteDataset, audits: dict[str, Any]) -> dict[str, object]:
    overlays: list[dict[str, object]] = []
    for scenario_id in DEV_SCENARIOS:
        ground_truth = dataset.load_ground_truth(scenario_id)
        audit = blind_audit_from_dict(audits[scenario_id])
        if not audit.catalog_consistency.consistent:
            raise RuntimeError(f"catalog consistency failed for {scenario_id}")
        journeys = _journeys_for_audit(audit, ground_truth)
        primary = min(
            journeys, key=lambda item: _STAGE_ORDER[item.first_loss_stage]
        ).first_loss_stage
        overlay = ScenarioCompletenessOverlay(
            scenario_id=scenario_id,
            production_resolution=audit.resolution,
            root_journeys=journeys,
            scenario_primary_loss_stage=primary,
        )
        overlays.append(overlay.as_dict())
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "ground_truth_loaded_after_seal": True,
        "catalog_consistency": {
            scenario_id: {
                **blind_audit_from_dict(audits[scenario_id]).catalog_consistency.as_dict(),
                "history_entity_count": len(
                    blind_audit_from_dict(audits[scenario_id]).source_exposure.history_entities
                ),
                "event_entity_count": len(
                    blind_audit_from_dict(audits[scenario_id]).source_exposure.event_entities
                ),
            }
            for scenario_id in DEV_SCENARIOS
        },
        "scenarios": overlays,
    }


def _blind_payload(dataset: ITBenchLiteDataset) -> dict[str, object]:
    from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
    from packages.evals.itbench.source import SnapshotSource

    audits: dict[str, object] = {}
    for scenario_id in DEV_SCENARIOS:
        scenario = dataset.scenario(scenario_id)
        backend = ITBenchSnapshotBackend(dataset, scenario)
        snapshot_entities = snapshot_entity_inventory(backend)
        if not snapshot_entities and backend.observable_entities():
            raise RuntimeError(f"observable entity inventory unexpectedly empty: {scenario_id}")
        source = SnapshotSource(scenario)
        audits[scenario_id] = build_blind_audit(source, snapshot_entities).as_dict()
    catalog_complete = all(
        bool(cast(dict[str, Any], audit)["catalog_consistency"]["consistent"])
        and not cast(dict[str, Any], audit)["catalog_consistency"]["missing_from_catalog"]
        for audit in audits.values()
    )
    if not catalog_complete:
        raise RuntimeError("one or more DEV catalog/source consistency checks failed")
    return {
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_ids": DEV_SCENARIOS,
        "observable_entity_catalog_complete": catalog_complete,
        "audits": audits,
    }


def _print_blind(payload: dict[str, object]) -> None:
    print("Scenario | Snapshot entities | Findings | Candidates | Hypotheses | Resolution")
    print("---|---:|---:|---:|---:|---")
    for scenario_id in DEV_SCENARIOS:
        audit = cast(dict[str, Any], payload["audits"])[scenario_id]
        print(
            f"{scenario_id} | {len(audit['snapshot_entities'])} | {len(audit['findings'])} | "
            f"{len(audit['candidates'])} | {len(audit['hypotheses'])} | {audit['resolution']}"
        )


def _print_overlay(payload: dict[str, object]) -> None:
    print("Scenario | Catalog | History | Events | Missing K8s source entities")
    print("---|---:|---:|---:|---:|")
    consistency = cast(dict[str, dict[str, Any]], payload["catalog_consistency"])
    for scenario_id in DEV_SCENARIOS:
        item = consistency[scenario_id]
        print(
            f"{scenario_id} | {item['observable_catalog_count']} | "
            f"{item['history_entity_count']} | {item['event_entity_count']} | "
            f"{len(item['missing_from_catalog'])}"
        )
    print(
        "Scenario | Root group | Snapshot | Source | Finding | Candidate | Hypothesis | Plausible | Leading | Final stage"
    )
    print("---|---|---|---|---|---|---|---|---|---")
    for scenario in cast(list[dict[str, Any]], payload["scenarios"]):
        for journey in scenario["root_journeys"]:
            finding = (
                "DIRECT"
                if journey["direct_finding_matches"]
                else "RELATED_ONLY"
                if journey["related_finding_matches"]
                else "—"
            )
            print(
                f"{scenario['scenario_id']} | {journey['root_group_id']} | "
                f"{'YES' if journey['observable_snapshot_matches'] else 'NO'} | "
                f"{'YES' if journey['source_exposed_matches'] else 'NO'} | {finding} | "
                f"{'YES' if journey['candidate_matches'] else 'NO'} | "
                f"{'YES' if journey['hypothesis_matches'] else 'NO'} | "
                f"{'YES' if journey['plausible_hypothesis_matches'] else 'NO'} | "
                f"{'YES' if journey['leading_hypothesis_matches'] else 'NO'} | {journey['final_stage']}"
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
        forbidden = forbidden_blind_keys(payload)
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
            "observable_entity_catalog_complete": payload["observable_entity_catalog_complete"],
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
        or seal.get("observable_entity_catalog_complete") is not True
    ):
        raise SystemExit("blind audit seal verification failed")
    blind = json.loads(blind_path.read_text(encoding="utf-8"))
    if tuple(blind.get("scenario_ids", ())) != DEV_SCENARIOS:
        raise SystemExit("blind audit scenario set is not the DEV set")
    if blind.get("observable_entity_catalog_complete") is not True:
        raise SystemExit("blind observable entity catalog is incomplete")
    for scenario_id, audit in cast(dict[str, Any], blind["audits"]).items():
        consistency = audit.get("catalog_consistency", {})
        if consistency.get("consistent") is not True or consistency.get("missing_from_catalog"):
            raise SystemExit(f"blind catalog/source consistency failed: {scenario_id}")
    forbidden = forbidden_blind_keys(blind)
    if forbidden:
        raise SystemExit(f"blind artifact contains forbidden fields: {forbidden}")
    audits = cast(dict[str, Any], blind["audits"])
    overlay = _overlay(dataset, audits)
    overlay["blind_artifact_sha256"] = _sha(blind_path)
    overlay_path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_overlay(overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
