"""Provider-free E11 retrieval qualification and post-hoc evaluation.

The catalog/ranking phase has no ground-truth dependency.  This module keeps
the freeze boundary explicit: ``build_frozen_outputs`` returns serializable
observable outputs first; ``evaluate_frozen_outputs`` is the only function
that loads evaluator truth.
"""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, cast

from packages.evals.itbench.contracts import ITBenchEntity, ITBenchEvidenceCategory
from packages.evals.itbench.dataset import ITBENCH_SCENARIO_IDS, ITBenchLiteDataset
from packages.evals.itbench.e11_observability import (
    E11_B1_CONFIG,
    E11_RETRIEVAL_VERSION,
    E11RetrievalConfig,
    _fast_source_records,
    build_observed_entity_catalog,
    rank_observed_candidates,
)
from packages.evals.itbench.grader import _entity_matches_group
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend


class _CachedBackend:
    """Memoize immutable snapshot reads while comparing several ablations."""

    def __init__(self, backend: ITBenchSnapshotBackend) -> None:
        self._backend = backend
        self.scenario = backend.scenario
        self._records: dict[ITBenchEvidenceCategory, tuple[dict[str, Any], ...]] = {}

    def complete_source_records(
        self, category: ITBenchEvidenceCategory
    ) -> tuple[dict[str, Any], ...]:
        if category not in self._records:
            self._records[category] = tuple(self._backend.complete_source_records(category))
        return self._records[category]

    def records(self, category: ITBenchEvidenceCategory) -> tuple[dict[str, Any], ...]:
        return self.complete_source_records(category)

    def topology(self, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        return self._backend.topology(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


def build_frozen_outputs(
    dataset: ITBenchLiteDataset,
    *,
    include_telemetry: bool = True,
    max_telemetry_records: int = 5_000,
    shortlist_size: int = 10,
) -> dict[str, Any]:
    """Build all scenario catalogs and rankings without opening ground truth."""
    started = monotonic()
    scenarios: list[dict[str, Any]] = []
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
        catalog = build_observed_entity_catalog(
            backend,
            include_telemetry=include_telemetry,
            max_telemetry_records=max_telemetry_records,
        )
        ranked = rank_observed_candidates(
            backend,
            catalog,
            limit=shortlist_size,
            include_telemetry=include_telemetry,
            max_telemetry_records=max_telemetry_records,
        )
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "catalog": catalog.as_dict(),
                "active_shortlist": [item.as_dict() for item in ranked],
                "backend_performance": backend.performance_snapshot(),
            }
        )
    payload = {
        "execution": "ITB-E11-offline-retrieval",
        "retrieval_version": E11_RETRIEVAL_VERSION,
        "dataset_revision": _dataset_revision(dataset),
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "ground_truth_access": 0,
        "provider_invocations": 0,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "build_wall_time_ms": int((monotonic() - started) * 1000),
    }
    return payload


def evaluate_frozen_outputs(dataset: ITBenchLiteDataset, frozen: dict[str, Any]) -> dict[str, Any]:
    """Evaluate only after the observable output has been serialized/frozen."""
    if frozen.get("ground_truth_access") != 0:
        raise ValueError("frozen observable output already accessed ground truth")
    scenario_map = {str(item["scenario_id"]): item for item in frozen.get("scenarios", ())}
    rows: list[dict[str, Any]] = []
    for scenario_id in ITBENCH_SCENARIO_IDS:
        item = scenario_map[scenario_id]
        ground_truth = dataset.load_ground_truth(scenario_id)
        root_groups = [group for group in ground_truth.root_cause_groups if group.root_cause]
        entities = item["catalog"]["entities"]
        shortlist = item["active_shortlist"]
        root_catalog = sum(_entity_matches_any(entity, root_groups) for entity in entities)
        ranks = {}
        for rank in (1, 3, 5, 10, 20):
            candidates = shortlist[:rank]
            ranks[str(rank)] = int(
                any(_entity_matches_any(item, root_groups) for item in candidates)
            )
        rows.append(
            {
                "scenario_id": scenario_id,
                "root_catalog_count": root_catalog,
                "root_catalog_covered": bool(root_catalog),
                "shortlist_size": len(shortlist),
                "root_reachable_at": ranks,
                "catalog_size": len(entities),
                "shortlist_families": dict(
                    Counter(str(item.get("family", "")) for item in shortlist)
                ),
                "shortlist": shortlist,
            }
        )
    count = len(rows)
    return {
        "scenario_count": count,
        "catalog_root_coverage": sum(int(row["root_catalog_covered"]) for row in rows) / count,
        "recall_at_k": {
            key: sum(int(row["root_reachable_at"].get(key, 0)) for row in rows) / count
            for key in ("1", "3", "5", "10", "20")
        },
        "conditional_recall_at_k": _conditional_recall(rows),
        "catalog_size": _distribution([int(row["catalog_size"]) for row in rows]),
        "shortlist_size": _distribution([int(row["shortlist_size"]) for row in rows]),
        "candidate_concentration": _candidate_concentration(rows),
        "scenarios": rows,
        "ground_truth_access": count,
        "provider_invocations": 0,
    }


def write_frozen_outputs(path: Path, payload: dict[str, Any]) -> str:
    """Persist observable outputs before any evaluator call and return its hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(encoded, encoding="utf-8")
    return sha256(encoded.encode("utf-8")).hexdigest()


def _entity_matches_any(entity: dict[str, Any], groups: list[Any]) -> bool:
    canonical = entity.get("canonical")
    if (
        isinstance(canonical, str)
        and not all(entity.get(field) for field in ("namespace", "kind", "name"))
        and canonical.count("/") == 2
    ):
        namespace, kind, name = canonical.split("/", 2)
        entity = {**entity, "namespace": namespace, "kind": kind, "name": name}
    try:
        candidate = ITBenchEntity(
            namespace=entity.get("namespace") if entity.get("namespace") != "_cluster" else None,
            kind=str(entity["kind"]),
            name=str(entity["name"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    return any(_entity_matches_group(candidate, group) for group in groups)


def _conditional_recall(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    observable = [row for row in rows if row["root_catalog_covered"]]
    if not observable:
        return {key: None for key in ("1", "3", "5", "10", "20")}
    return {
        key: sum(int(row["root_reachable_at"].get(key, 0)) for row in observable) / len(observable)
        for key in ("1", "3", "5", "10", "20")
    }


def _distribution(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "min": ordered[0] if ordered else 0,
        "median": ordered[len(ordered) // 2] if ordered else 0,
        "max": ordered[-1] if ordered else 0,
        "mean": sum(ordered) / len(ordered) if ordered else 0.0,
    }


def _candidate_concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    top1: Counter[str] = Counter()
    top10: Counter[str] = Counter()
    for row in rows:
        shortlist = row.get("shortlist", row.get("active_shortlist", ()))
        if shortlist:
            top1[str(shortlist[0].get("family", ""))] += 1
        top10.update(str(item.get("family", "")) for item in shortlist[:10])
    return {"top1_family_counts": dict(top1), "top10_family_counts": dict(top10)}


def _dataset_revision(dataset: ITBenchLiteDataset) -> str:
    manifest = json.loads(dataset.manifest_path.read_text(encoding="utf-8"))
    return str(manifest["revision"])


def _retrieval_config_dict(config: E11RetrievalConfig) -> dict[str, Any]:
    return {
        "include_telemetry_in_catalog": config.include_telemetry_in_catalog,
        "include_telemetry_in_ranking": config.include_telemetry_in_ranking,
        "use_direct_topology": config.use_direct_topology,
        "use_causal_propagation": config.use_causal_propagation,
        "use_namespace_context": config.use_namespace_context,
        "use_temporal": config.use_temporal,
        "diversity": config.diversity,
        "shortlist_size": config.shortlist_size,
    }


def build_retrieval_ablations(
    dataset: ITBenchLiteDataset,
    *,
    shortlist_size: int = 10,
    max_telemetry_records: int = 5_000,
) -> dict[str, dict[str, Any]]:
    """Build R0-R6 observable outputs before any ground-truth access."""
    configurations: dict[str, dict[str, Any]] = {
        "R0": {"legacy": True},
        "R1": {"telemetry": False, "topology": False, "temporal": False, "diversity": False},
        "R2": {"telemetry": True, "topology": False, "temporal": False, "diversity": False},
        "R3": {"telemetry": True, "topology": True, "temporal": False, "diversity": False},
        "R4": {"telemetry": True, "topology": True, "temporal": True, "diversity": False},
        "R5": {"telemetry": True, "topology": True, "temporal": True, "diversity": True},
        # R6 keeps the R5 evidence policy but measures a wider bounded active
        # shortlist.  This isolates reachability from the top-K presentation
        # limit without changing any scoring signal.
        "R6": {
            "telemetry": True,
            "topology": True,
            "temporal": True,
            "diversity": True,
            "shortlist_size": max(20, shortlist_size),
        },
    }
    result: dict[str, dict[str, Any]] = {}
    scenario_cache: dict[str, dict[str, Any]] = {}
    for label, config in configurations.items():
        scenarios: list[dict[str, Any]] = []
        for scenario in dataset.scenarios():
            cached = scenario_cache.setdefault(
                scenario.scenario_id,
                {
                    "backend": _CachedBackend(
                        ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
                    )
                },
            )
            backend = cached["backend"]
            if config.get("legacy"):
                candidates = backend.candidate_entities(limit=shortlist_size)
                shortlist = [
                    {
                        "canonical": item["canonical"],
                        "kind": item["kind"],
                        "name": item["name"],
                        "namespace": item["namespace"],
                        "family": f"{item['namespace']}/{item['kind']}",
                    }
                    for item in candidates
                ]
                catalog = {"entities": list(backend.observable_entities())}
            else:
                telemetry = cached.get("telemetry")
                if config["telemetry"] and telemetry is None:
                    telemetry = {
                        category: tuple(
                            _fast_source_records(backend, category, limit=max_telemetry_records)
                        )
                        for category in (
                            ITBenchEvidenceCategory.METRICS,
                            ITBenchEvidenceCategory.LOGS,
                            ITBenchEvidenceCategory.TRACES,
                        )
                    }
                    cached["telemetry"] = telemetry
                catalog_key = "full_catalog" if config["telemetry"] else "k8s_catalog"
                catalog_object = cached.get(catalog_key)
                if catalog_object is None:
                    catalog_object = build_observed_entity_catalog(
                        backend,
                        include_telemetry=bool(config["telemetry"]),
                        max_telemetry_records=max_telemetry_records,
                        telemetry_records=telemetry,
                    )
                    cached[catalog_key] = catalog_object
                ranked = rank_observed_candidates(
                    backend,
                    catalog_object,
                    limit=int(config.get("shortlist_size", shortlist_size)),
                    diversity=bool(config["diversity"]),
                    include_telemetry=bool(config["telemetry"]),
                    max_telemetry_records=max_telemetry_records,
                    use_topology=bool(config["topology"]),
                    use_temporal=bool(config["temporal"]),
                    telemetry_records=telemetry if config["telemetry"] else None,
                )
                catalog = {"entities": catalog_object.as_dict()["entities"]}
                shortlist = [item.as_dict() for item in ranked]
            scenarios.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "catalog": catalog,
                    "active_shortlist": shortlist,
                }
            )
        result[label] = {
            "configuration": config,
            "dataset_revision": _dataset_revision(dataset),
            "scenario_order": list(ITBENCH_SCENARIO_IDS),
            "ground_truth_access": 0,
            "provider_invocations": 0,
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        }
    return result


def build_clean_retrieval_ablations(
    dataset: ITBenchLiteDataset,
    *,
    shortlist_size: int = 10,
    max_telemetry_records: int = 100,
) -> dict[str, dict[str, Any]]:
    """Build independently-controlled B0-B6 outputs without GT access.

    These labels intentionally supersede the historical R-series, whose
    topology flag did not disable causal propagation.
    """
    configuration_objects: dict[str, E11RetrievalConfig | None] = {
        "B0": None,
        "B1": E11_B1_CONFIG,
        "B2": E11RetrievalConfig(
            include_telemetry_in_catalog=True, include_telemetry_in_ranking=True
        ),
        "B3": E11RetrievalConfig(
            include_telemetry_in_catalog=True,
            include_telemetry_in_ranking=True,
            use_direct_topology=True,
        ),
        "B4": E11RetrievalConfig(
            include_telemetry_in_catalog=True,
            include_telemetry_in_ranking=True,
            use_direct_topology=True,
            use_causal_propagation=True,
        ),
        "B5": E11RetrievalConfig(
            include_telemetry_in_catalog=True,
            include_telemetry_in_ranking=True,
            use_direct_topology=True,
            use_causal_propagation=True,
            use_temporal=True,
        ),
        "B6": E11RetrievalConfig(
            include_telemetry_in_catalog=True,
            include_telemetry_in_ranking=True,
            use_direct_topology=True,
            use_causal_propagation=True,
            use_temporal=True,
            diversity=True,
        ),
    }
    configurations: dict[str, dict[str, Any]] = {
        label: ({"legacy": True} if config is None else {"retrieval_config": config})
        for label, config in configuration_objects.items()
    }
    outputs: dict[str, dict[str, Any]] = {}
    scenario_cache: dict[str, dict[str, Any]] = {}
    for label, config in configurations.items():
        scenarios: list[dict[str, Any]] = []
        for scenario in dataset.scenarios():
            cached = scenario_cache.setdefault(
                scenario.scenario_id,
                {
                    "backend": _CachedBackend(
                        ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
                    )
                },
            )
            backend = cached["backend"]
            retrieval_config = config.get("retrieval_config")
            telemetry = cached.get("telemetry")
            if (
                retrieval_config is not None
                and retrieval_config.include_telemetry_in_catalog
                and telemetry is None
            ):
                telemetry = {
                    category: tuple(
                        _fast_source_records(backend, category, limit=max_telemetry_records)
                    )
                    for category in (
                        ITBenchEvidenceCategory.METRICS,
                        ITBenchEvidenceCategory.LOGS,
                        ITBenchEvidenceCategory.TRACES,
                    )
                }
                cached["telemetry"] = telemetry
            if config.get("legacy"):
                shortlist = [
                    {
                        "canonical": item["canonical"],
                        "kind": item["kind"],
                        "name": item["name"],
                        "namespace": item["namespace"],
                        "family": f"{item['namespace']}/{item['kind']}",
                    }
                    for item in backend.candidate_entities(limit=shortlist_size)
                ]
                catalog = {"entities": list(backend.observable_entities())}
            else:
                assert isinstance(retrieval_config, E11RetrievalConfig)
                catalog_key = (
                    "full_catalog"
                    if retrieval_config.include_telemetry_in_catalog
                    else "k8s_catalog"
                )
                built = cached.get(catalog_key)
                if built is None:
                    built = build_observed_entity_catalog(
                        backend,
                        include_telemetry=retrieval_config.include_telemetry_in_catalog,
                        max_telemetry_records=max_telemetry_records,
                        telemetry_records=telemetry,
                    )
                    cached[catalog_key] = built
                ranked = rank_observed_candidates(
                    backend,
                    built,
                    limit=retrieval_config.shortlist_size,
                    diversity=retrieval_config.diversity,
                    include_telemetry=retrieval_config.include_telemetry_in_ranking,
                    use_direct_topology=retrieval_config.use_direct_topology,
                    use_causal_propagation=retrieval_config.use_causal_propagation,
                    use_temporal=retrieval_config.use_temporal,
                    use_namespace_context=retrieval_config.use_namespace_context,
                    telemetry_records=telemetry,
                )
                shortlist = [item.as_dict() for item in ranked]
                catalog = {"entities": built.as_dict()["entities"]}
            scenarios.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "catalog": catalog,
                    "active_shortlist": shortlist,
                }
            )
        outputs[label] = {
            "configuration": (
                {"legacy": True}
                if config.get("legacy")
                else _retrieval_config_dict(cast(E11RetrievalConfig, config["retrieval_config"]))
            ),
            "dataset_revision": _dataset_revision(dataset),
            "scenario_order": list(ITBENCH_SCENARIO_IDS),
            "ground_truth_access": 0,
            "provider_invocations": 0,
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
        }
    return outputs


def evaluate_retrieval_ablations(
    dataset: ITBenchLiteDataset, ablations: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Evaluate frozen R0-R5 outputs only after they have been persisted."""
    return {label: evaluate_frozen_outputs(dataset, frozen) for label, frozen in ablations.items()}


__all__ = [
    "build_frozen_outputs",
    "build_retrieval_ablations",
    "build_clean_retrieval_ablations",
    "evaluate_retrieval_ablations",
    "evaluate_frozen_outputs",
    "write_frozen_outputs",
]
