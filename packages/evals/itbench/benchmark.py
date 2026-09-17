"""Predict, seal, and grade the diagnosis engine on ITBench-Lite snapshots.

Prediction never opens ground truth. Grading refuses to run on an unsealed or
modified prediction set.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntityPrediction,
    ITBenchGroundTruth,
    parse_canonical_entity,
)
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.grader import grade_root_cause_entities
from packages.evals.itbench.io import atomic_json_write
from packages.evals.itbench.source import SnapshotSource
from packages.rca.engine import EngineConfig, Investigator, diagnose
from packages.rca.model import Confidence, Diagnosis

SPLIT_FILE = Path(__file__).resolve().parents[3] / "evals" / "itbench_split.json"
BASELINES = {"retired E10 agent (archive/experiments-2026-09)": 0.0}


class BenchmarkError(RuntimeError):
    """The run cannot be graded or reproduced."""


def load_split(name: str, path: Path = SPLIT_FILE) -> tuple[str, ...]:
    split = json.loads(path.read_text(encoding="utf-8"))
    if name == "all":
        return tuple(sorted([*split["dev"], *split["test"]], key=lambda s: int(s.split("-")[1])))
    if name not in {"dev", "test"}:
        raise BenchmarkError(f"unknown split {name!r}")
    return tuple(split[name])


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_dirty() -> bool:
    try:
        output = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return True
    return bool(output.strip())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def agent_output(diagnosis: Diagnosis) -> ITBenchAgentOutput:
    """Map a diagnosis to the ITBench format: the chosen root cause only."""
    factors: tuple[ITBenchEntityPrediction, ...] = ()
    if diagnosis.root_cause is not None:
        factors = (
            ITBenchEntityPrediction(
                entity=parse_canonical_entity(diagnosis.root_cause.canonical),
                rank=1,
                condition=f"{diagnosis.confidence.value}: {diagnosis.summary}"[:1000],
            ),
        )
    return ITBenchAgentOutput(
        incident_id=diagnosis.incident_id,
        scenario_id=diagnosis.incident_id,
        contributing_factor=factors,
        reasoning=diagnosis.summary[:1000],
        native_terminal=diagnosis.confidence.value,
    )


def predict(
    dataset: ITBenchLiteDataset,
    scenario_ids: Sequence[str],
    out_dir: Path,
    *,
    split: str,
    investigator: Investigator | None = None,
    config: EngineConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Diagnose every scenario, write predictions, and seal them."""
    if (out_dir / "seal.json").exists():
        raise BenchmarkError(f"{out_dir} already holds a sealed run; use a new directory")
    if split == "test" and _git_dirty():
        raise BenchmarkError("test predictions require a clean repository")
    config = config or EngineConfig()
    predictions = out_dir / "predictions"
    records: list[dict[str, Any]] = []
    started = time.monotonic()
    for scenario_id in scenario_ids:
        tick = time.monotonic()
        diagnosis = diagnose(
            SnapshotSource(dataset.scenario(scenario_id)),
            investigator=investigator,
            config=config,
        )
        output = agent_output(diagnosis)
        atomic_json_write(
            predictions / f"{scenario_id}.json",
            {
                "diagnosis": diagnosis.model_dump(mode="json"),
                "agent_output": output.model_dump(mode="json"),
            },
        )
        records.append(
            {
                "scenario_id": scenario_id,
                "seconds": round(time.monotonic() - tick, 3),
                "model_calls": diagnosis.model_calls,
                "investigator_error": next(
                    (
                        s.detail
                        for s in diagnosis.steps
                        if s.actor != "engine" and s.action == "error"
                    ),
                    None,
                ),
            }
        )
        if progress:
            progress(f"{scenario_id}: {diagnosis.root_cause} ({diagnosis.confidence.value})")
    manifest = {
        "benchmark": "ITBench-Lite SRE",
        "split": split,
        "scenario_ids": list(scenario_ids),
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": _git_head(),
        "git_dirty": _git_dirty(),
        "mode": investigator.name if investigator else "deterministic",
        "model": getattr(getattr(investigator, "client", None), "model", None),
        "engine_config": json.loads(json.dumps(asdict(config), default=str)),
        "ground_truth_read_during_prediction": False,
        "seconds_total": round(time.monotonic() - started, 3),
        "model_calls_total": sum(r["model_calls"] for r in records),
        "investigator_errors": sum(1 for r in records if r["investigator_error"]),
        "records": records,
    }
    atomic_json_write(out_dir / "manifest.json", manifest)
    files = sorted(predictions.glob("*.json"))
    seal = {
        "manifest_sha256": _sha(out_dir / "manifest.json"),
        "predictions": {path.name: _sha(path) for path in files},
    }
    atomic_json_write(out_dir / "seal.json", seal)
    return manifest


def _verify_seal(out_dir: Path) -> dict[str, Any]:
    seal_path = out_dir / "seal.json"
    if not seal_path.exists():
        raise BenchmarkError("predictions are not sealed")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if seal["manifest_sha256"] != _sha(out_dir / "manifest.json"):
        raise BenchmarkError("manifest changed after sealing")
    for name, digest in seal["predictions"].items():
        if _sha(out_dir / "predictions" / name) != digest:
            raise BenchmarkError(f"prediction {name} changed after sealing")
    manifest: dict[str, Any] = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    if len(seal["predictions"]) != len(manifest["scenario_ids"]):
        raise BenchmarkError("sealed prediction count does not match the manifest")
    return manifest


def _matches(entity_canonical: str, truth: ITBenchGroundTruth) -> bool:
    output = ITBenchAgentOutput(
        incident_id="grade",
        scenario_id=truth.scenario_id,
        contributing_factor=(
            ITBenchEntityPrediction(
                entity=parse_canonical_entity(entity_canonical), rank=1, condition="grade"
            ),
        ),
        native_terminal="grade",
    )
    return grade_root_cause_entities(output, truth).true_positive > 0


def _observable(dataset: ITBenchLiteDataset, scenario_id: str, truth: ITBenchGroundTruth) -> bool:
    """Whether any observed object or event subject matches a root-cause group."""
    source = SnapshotSource(dataset.scenario(scenario_id))
    entities = {ref.canonical for ref in source.object_history()}
    entities.update(event.entity.canonical for event in source.events())
    return any(_matches(canonical, truth) for canonical in sorted(entities))


def grade(dataset: ITBenchLiteDataset, out_dir: Path) -> dict[str, Any]:
    """Grade a sealed run and write report.json and report.md."""
    manifest = _verify_seal(out_dir)
    rows: list[dict[str, Any]] = []
    for scenario_id in manifest["scenario_ids"]:
        payload = json.loads(
            (out_dir / "predictions" / f"{scenario_id}.json").read_text(encoding="utf-8")
        )
        diagnosis = Diagnosis.model_validate(payload["diagnosis"])
        output = ITBenchAgentOutput.model_validate_json(json.dumps(payload["agent_output"]))
        truth = dataset.load_ground_truth(scenario_id)
        ranked = [c.canonical for c in [diagnosis.root_cause] if c] + [
            c.entity.canonical for c in diagnosis.alternatives
        ]
        position = next((i + 1 for i, ref in enumerate(ranked) if _matches(ref, truth)), None)
        grade_row = grade_root_cause_entities(output, truth)
        verified = diagnosis.confidence is Confidence.VERIFIED
        rows.append(
            {
                "scenario_id": scenario_id,
                "prediction": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
                "confidence": diagnosis.confidence.value,
                "correct": grade_row.true_positive > 0,
                "f1": grade_row.f1,
                "verified_f1": grade_row.f1 if verified else 0.0,
                "position": position,
                "ground_truth": [
                    f"{g.namespace or '_cluster'}/{g.kind}/{g.name or '|'.join(g.filters)}"
                    for g in truth.root_cause_groups
                    if g.root_cause
                ],
                "observable": _observable(dataset, scenario_id, truth),
            }
        )
    count = len(rows)

    def rate(values: list[bool]) -> float:
        return round(sum(values) / count, 4) if count else 0.0

    by_confidence: dict[str, dict[str, float]] = {}
    for row in rows:
        bucket = by_confidence.setdefault(row["confidence"], {"count": 0, "correct": 0})
        bucket["count"] += 1
        bucket["correct"] += int(row["correct"])
    for bucket in by_confidence.values():
        bucket["precision"] = round(bucket["correct"] / bucket["count"], 4)
        bucket["share_of_predictions"] = round(bucket["count"] / count, 4) if count else 0.0
    verified_rows = [row for row in rows if row["confidence"] == Confidence.VERIFIED.value]
    verified_count = len(verified_rows)
    verified_correct = sum(int(row["correct"]) for row in verified_rows)
    report = {
        "benchmark": manifest["benchmark"],
        "split": manifest["split"],
        "mode": manifest["mode"],
        "model": manifest.get("model"),
        "git_head": manifest["git_head"],
        "git_dirty": manifest["git_dirty"],
        "scenarios": count,
        "macro_f1": round(sum(r["f1"] for r in rows) / count, 4) if count else 0.0,
        "coverage_weighted_verified_f1": round(sum(r["verified_f1"] for r in rows) / count, 4)
        if count
        else 0.0,
        "verified_conditional_macro_f1": round(
            sum(r["f1"] for r in verified_rows) / verified_count, 4
        )
        if verified_count
        else 0.0,
        "verified_count": verified_count,
        "verified_correct": verified_correct,
        "verified_coverage": round(verified_count / count, 4) if count else 0.0,
        "verified_accuracy": round(verified_correct / verified_count, 4) if verified_count else 0.0,
        "funnel": {
            "root_cause_observable": rate([r["observable"] for r in rows]),
            "in_top_5": rate([r["position"] is not None for r in rows]),
            "in_top_3": rate([r["position"] is not None and r["position"] <= 3 for r in rows]),
            "top_1": rate([r["position"] == 1 for r in rows]),
        },
        "answered": rate([r["prediction"] is not None for r in rows]),
        "by_confidence": by_confidence,
        "baselines_macro_f1": BASELINES,
        "model_calls_total": manifest["model_calls_total"],
        "investigator_errors": manifest.get("investigator_errors", 0),
        "seconds_total": manifest["seconds_total"],
        "rows": rows,
    }
    atomic_json_write(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    funnel = report["funnel"]
    lines = [
        f"# {report['benchmark']} — {report['split']} split",
        "",
        f"Mode `{report['mode']}`"
        + (f" (`{report['model']}`)" if report.get("model") else "")
        + f", commit `{report['git_head'][:12]}`"
        + (" (dirty tree)" if report["git_dirty"] else "")
        + f", {report['scenarios']} scenarios, {report['model_calls_total']} model calls, "
        f"{report['seconds_total']:.1f} s.",
        "",
        *(
            [
                f"**Warning:** the investigator failed in {report['investigator_errors']} "
                "scenario(s); those answers are the engine's. This is not a valid "
                "investigator run.",
                "",
            ]
            if report.get("investigator_errors")
            else []
        ),
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Macro F1 (all answers) | {report['macro_f1']:.3f} |",
        f"| Coverage-weighted VERIFIED F1 | {report['coverage_weighted_verified_f1']:.3f} |",
        f"| VERIFIED conditional macro F1 | {report['verified_conditional_macro_f1']:.3f} |",
        f"| VERIFIED coverage | {report['verified_coverage']:.1%} |",
        f"| VERIFIED accuracy | {report['verified_accuracy']:.1%} |",
        f"| Answered | {report['answered']:.0%} |",
        f"| Root cause observable in snapshot | {funnel['root_cause_observable']:.0%} |",
        f"| Root cause in top 5 | {funnel['in_top_5']:.0%} |",
        f"| Root cause in top 3 | {funnel['in_top_3']:.0%} |",
        f"| Root cause ranked first | {funnel['top_1']:.0%} |",
    ]
    for name, value in report["baselines_macro_f1"].items():
        lines.append(f"| Baseline: {name} | {value:.3f} |")
    lines += [
        "",
        "| Confidence | Count | Correct | Precision | Share |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, bucket in sorted(report["by_confidence"].items()):
        lines.append(
            f"| {name} | {bucket['count']} | {bucket['correct']} | "
            f"{bucket['precision']:.1%} | {bucket['share_of_predictions']:.1%} |"
        )
    lines += [
        "",
        "| Scenario | Prediction | Confidence | Correct | Rank | GT observable |",
        "| --- | --- | --- | :---: | ---: | :---: |",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['scenario_id']} | `{row['prediction']}` | {row['confidence']} | "
            f"{'yes' if row['correct'] else 'no'} | {row['position'] or '-'} | "
            f"{'yes' if row['observable'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["BenchmarkError", "agent_output", "grade", "load_split", "predict", "render_markdown"]
