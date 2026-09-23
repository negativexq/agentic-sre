"""Prediction-first benchmark path for bounded investigation audits.

The prediction function receives the public scenario loader only. Grading is a
separate function that verifies the prediction seal before requesting labels.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from packages.evals.investigation_metrics import (
    InvestigationMetricsV1,
    derive_investigation_metrics,
)
from packages.evals.itbench.benchmark import BenchmarkError, _git_dirty, _git_head, agent_output
from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchGroundTruth,
    ITBenchScenario,
)
from packages.evals.itbench.grader import grade_root_cause_entities
from packages.evals.itbench.io import atomic_json_write
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import LLMIntentPolicy, LLMInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig, InvestigationPolicy
from packages.rca.llm import LLMClient, LLMError
from packages.rca.model import Diagnosis, InvestigationResult

PRIMARY_EVALUATION_MODEL = "gpt-6-luna"


class EvaluationCallBudget:
    """Evaluation-wide provider-attempt budget shared across scenarios."""

    def __init__(self, max_calls: int) -> None:
        if max_calls <= 0:
            raise ValueError("provider evaluation requires a positive total call budget")
        self.max_calls = max_calls
        self.calls = 0

    def consume(self) -> None:
        if self.calls >= self.max_calls:
            raise LLMError(f"evaluation model-call budget exhausted ({self.max_calls})")
        self.calls += 1


class PerScenarioLLMClient:
    """Bound one scenario while sharing the run-level provider-call budget."""

    def __init__(
        self,
        client: LLMClient,
        *,
        run_budget: EvaluationCallBudget,
        max_calls: int,
    ) -> None:
        if max_calls <= 0:
            raise ValueError("per-scenario model-call budget must be positive")
        self._client = client
        self._run_budget = run_budget
        self._max_calls = max_calls
        self.calls = 0
        self.model = client.model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], name: str
    ) -> dict[str, Any]:
        if self.calls >= self._max_calls:
            raise LLMError(f"scenario model-call budget exhausted ({self._max_calls})")
        self._run_budget.consume()
        self.calls += 1
        return self._client.complete_json(system=system, user=user, schema=schema, name=name)


class ScenarioDataset(Protocol):
    """Prediction-only dataset interface; it intentionally exposes no labels."""

    def scenario(self, scenario_id: str) -> ITBenchScenario: ...


class GradingDataset(ScenarioDataset, Protocol):
    """Evaluator-side dataset interface with explicit label access."""

    def load_ground_truth(self, scenario_id: str) -> ITBenchGroundTruth: ...


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predict_investigations(
    dataset: ScenarioDataset,
    scenario_ids: tuple[str, ...] | list[str],
    out_dir: Path,
    *,
    split: str,
    config: InvestigationConfig | None = None,
    model_client: LLMClient | None = None,
    max_model_calls_per_scenario: int | None = None,
    max_total_model_calls: int = 0,
    api_usage_class: str = "CLASS 2",
    llm_selection: str = "action",
) -> dict[str, Any]:
    """Run deterministic bounded investigations and seal output before grading.

    This function deliberately calls only ``dataset.scenario``. It never calls
    ``load_ground_truth`` and writes all model outputs, runtime metrics,
    configuration and commit identity before creating the prediction seal.
    """
    if (out_dir / "seal.json").exists():
        raise BenchmarkError(f"{out_dir} already holds a sealed investigation run")
    ids = tuple(scenario_ids)
    if not ids or len(ids) != len(set(ids)):
        raise BenchmarkError("scenario IDs must be non-empty and unique")
    if split == "test" and _git_dirty():
        raise BenchmarkError("frozen test predictions require a clean working tree")

    effective_config = config or InvestigationConfig()
    call_budget: EvaluationCallBudget | None = None
    per_scenario_budget = 0
    if model_client is not None:
        if api_usage_class not in {"CLASS 1", "CLASS 2"}:
            raise BenchmarkError("provider evaluations must declare CLASS 1 or CLASS 2")
        if llm_selection not in {"action", "intent"}:
            raise BenchmarkError("llm_selection must be 'action' or 'intent'")
        if model_client.model != PRIMARY_EVALUATION_MODEL:
            raise BenchmarkError(
                f"new live evaluation requires configured model {PRIMARY_EVALUATION_MODEL}"
            )
        per_scenario_budget = max_model_calls_per_scenario or effective_config.max_model_calls
        required_total = len(ids) * per_scenario_budget
        if max_total_model_calls < required_total:
            raise BenchmarkError(
                "evaluation total model-call budget is below scenario_count × "
                "max_calls_per_scenario"
            )
        if api_usage_class == "CLASS 1" and (
            len(ids) != 1 or per_scenario_budget != 1 or max_total_model_calls != 1
        ):
            raise BenchmarkError("CLASS 1 requires exactly one scenario and one maximum call")
        client_budget = getattr(model_client, "max_calls", max_total_model_calls)
        if client_budget != max_total_model_calls:
            raise BenchmarkError("provider client budget must equal the declared evaluation budget")
        call_budget = EvaluationCallBudget(max_total_model_calls)
    elif max_total_model_calls != 0 or max_model_calls_per_scenario is not None:
        raise BenchmarkError("model-call budgets require a configured provider client")

    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    started = time.monotonic()
    total_model_calls = 0
    total_tool_calls = 0
    for scenario_id in ids:
        if call_budget is not None and call_budget.calls >= call_budget.max_calls:
            raise BenchmarkError("evaluation budget exhausted before the frozen scenario set ended")
        tick = time.monotonic()
        source = SnapshotSource(dataset.scenario(scenario_id))
        if model_client is None or call_budget is None:
            policy: InvestigationPolicy = DeterministicIntentPolicy()
        else:
            bounded_client = PerScenarioLLMClient(
                model_client,
                run_budget=call_budget,
                max_calls=per_scenario_budget,
            )
            policy = (
                LLMInvestigationPolicy(bounded_client)
                if llm_selection == "action"
                else LLMIntentPolicy(bounded_client)
            )
        result = investigate_diagnosis(
            source,
            policy=policy,
            config=effective_config,
        )
        metrics = derive_investigation_metrics(result)
        prediction = {
            "scenario_id": scenario_id,
            "initial_diagnosis": (
                result.initial_diagnosis.model_dump(mode="json")
                if result.initial_diagnosis is not None
                else None
            ),
            "diagnosis": result.diagnosis.model_dump(mode="json"),
            "agent_output": agent_output(result.diagnosis).model_dump(mode="json"),
            "investigation_result": result.model_dump(mode="json"),
            "runtime_metrics": metrics.model_dump(mode="json"),
        }
        atomic_json_write(out_dir / "predictions" / f"{scenario_id}.json", prediction)
        total_model_calls += result.model_calls
        total_tool_calls += result.tool_calls
        records.append(
            {
                "scenario_id": scenario_id,
                "seconds": round(time.monotonic() - tick, 3),
                "model_calls": result.model_calls,
                "tool_calls": result.tool_calls,
                "stop_reason": result.stop_reason.value,
                "runtime_metrics": metrics.model_dump(mode="json"),
            }
        )

    if call_budget is not None and total_model_calls != call_budget.calls:
        raise BenchmarkError(
            "provider attempt count does not match investigation model-call accounting"
        )

    manifest = {
        "benchmark": (
            "ITBench-Lite provider compatibility smoke"
            if api_usage_class == "CLASS 1" and model_client is not None
            else "ITBench-Lite bounded investigation"
        ),
        "metric_version": "m14.v1",
        "split": split,
        "scenario_ids": list(ids),
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": _git_head(),
        "git_dirty": _git_dirty(),
        "mode": (
            "deterministic-intent-policy" if model_client is None else f"llm-{llm_selection}-policy"
        ),
        "model": model_client.model if model_client is not None else None,
        "api_usage_class": "CLASS 0" if model_client is None else api_usage_class,
        "max_total_model_calls": max_total_model_calls,
        "max_calls_per_scenario": per_scenario_budget,
        "real_provider_calls": call_budget.calls if call_budget is not None else 0,
        "investigation_config": json.loads(json.dumps(asdict(effective_config), default=str)),
        "ground_truth_read_during_prediction": False,
        "seconds_total": round(time.monotonic() - started, 3),
        "model_calls_total": total_model_calls,
        "tool_calls_total": total_tool_calls,
        "records": records,
    }
    atomic_json_write(out_dir / "manifest.json", manifest)
    predictions = sorted((out_dir / "predictions").glob("*.json"))
    seal = {
        "manifest_sha256": _sha(out_dir / "manifest.json"),
        "predictions": {path.name: _sha(path) for path in predictions},
    }
    atomic_json_write(out_dir / "seal.json", seal)
    return manifest


def _verify_investigation_seal(out_dir: Path) -> dict[str, Any]:
    seal_path = out_dir / "seal.json"
    if not seal_path.is_file():
        raise BenchmarkError("investigation predictions are not sealed")
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file() or seal.get("manifest_sha256") != _sha(manifest_path):
        raise BenchmarkError("investigation manifest changed after sealing")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {f"{scenario_id}.json" for scenario_id in manifest["scenario_ids"]}
    if set(seal.get("predictions", {})) != expected:
        raise BenchmarkError("sealed prediction set does not match the frozen scenario IDs")
    for name, digest in seal["predictions"].items():
        path = out_dir / "predictions" / name
        if not path.is_file() or _sha(path) != digest:
            raise BenchmarkError(f"investigation prediction changed after sealing: {name}")
    return manifest


def _prediction_correct(output: ITBenchAgentOutput, truth: Any) -> bool:
    return grade_root_cause_entities(output, truth).true_positive > 0


def grade_investigations(dataset: GradingDataset, out_dir: Path) -> dict[str, Any]:
    """Grade only after the complete prediction/metric set passes seal checks."""
    manifest = _verify_investigation_seal(out_dir)
    if manifest.get("api_usage_class") == "CLASS 1":
        raise BenchmarkError("provider compatibility smoke is not a benchmark evaluation")
    rows: list[dict[str, Any]] = []
    metric_payloads: list[InvestigationMetricsV1] = []
    for scenario_id in manifest["scenario_ids"]:
        payload = json.loads(
            (out_dir / "predictions" / f"{scenario_id}.json").read_text(encoding="utf-8")
        )
        investigation_result = InvestigationResult.model_validate_json(
            json.dumps(payload["investigation_result"])
        )
        metrics = InvestigationMetricsV1.model_validate_json(json.dumps(payload["runtime_metrics"]))
        final_output = ITBenchAgentOutput.from_json_value(payload["agent_output"])
        initial = (
            Diagnosis.model_validate_json(json.dumps(payload["initial_diagnosis"]))
            if payload["initial_diagnosis"] is not None
            else investigation_result.diagnosis
        )
        # The only ground-truth access in this module occurs after the full
        # prediction artifact set has been hash-verified above.
        truth = dataset.load_ground_truth(scenario_id)
        before_correct = _prediction_correct(agent_output(initial), truth)
        after_correct = _prediction_correct(final_output, truth)
        outcome = (
            "RECOVERY"
            if not before_correct and after_correct
            else "HARM"
            if before_correct and not after_correct
            else "STABLE_CORRECT"
            if before_correct
            else "STABLE_WRONG"
        )
        metric_payloads.append(metrics)
        rows.append(
            {
                "scenario_id": scenario_id,
                "initial_correct": before_correct,
                "final_correct": after_correct,
                "outcome": outcome,
                "initial_resolution": investigation_result.initial_resolution.value,
                "final_resolution": investigation_result.final_resolution.value,
                "runtime_metrics": metrics.model_dump(mode="json"),
            }
        )

    metric_fields = (
        "tool_calls",
        "unique_observations",
        "useful_call_count",
        "novel_evidence_count",
        "duplicate_read_count",
        "no_data_count",
        "decision_relevant_call_count",
        "hypotheses_changed",
        "alternatives_eliminated",
        "gap_state_changes",
        "resolution_transitions",
        "invalid_actions",
        "rejected_actions",
        "out_of_policy_execution",
        "write_execution",
        "secret_access",
    )
    totals = {
        field: sum(getattr(item, field) for item in metric_payloads) for field in metric_fields
    }
    calls = totals["tool_calls"]
    novel_evidence_calls = sum(
        round((item.novel_evidence_rate or 0.0) * item.tool_calls) for item in metric_payloads
    )
    rates = {
        "useful_call_rate": totals["useful_call_count"] / calls if calls else None,
        "novel_evidence_rate": novel_evidence_calls / calls if calls else None,
        "duplicate_read_rate": totals["duplicate_read_count"] / calls if calls else None,
        "no_data_rate": totals["no_data_count"] / calls if calls else None,
        "decision_relevant_call_rate": totals["decision_relevant_call_count"] / calls
        if calls
        else None,
    }
    outcome_counts = {
        name: sum(row["outcome"] == name for row in rows)
        for name in ("RECOVERY", "STABLE_CORRECT", "STABLE_WRONG", "HARM")
    }
    evaluation_result: dict[str, Any] = {
        "benchmark": manifest["benchmark"],
        "metric_version": manifest["metric_version"],
        "split": manifest["split"],
        "scenario_ids": manifest["scenario_ids"],
        "git_head": manifest["git_head"],
        "mode": manifest["mode"],
        "model": manifest["model"],
        "api_usage_class": manifest["api_usage_class"],
        "real_provider_calls": manifest["real_provider_calls"],
        "scenario_count": len(rows),
        "metric_totals": totals,
        "rates": rates,
        "outcomes": outcome_counts,
        "rows": rows,
    }
    evaluation_path = out_dir / "evaluation.json"
    atomic_json_write(evaluation_path, evaluation_result)
    atomic_json_write(
        out_dir / "evaluation-seal.json",
        {
            "evaluation_sha256": _sha(evaluation_path),
            "prediction_seal_sha256": _sha(out_dir / "seal.json"),
            "git_head": manifest["git_head"],
            "scenario_ids": manifest["scenario_ids"],
        },
    )
    return evaluation_result
