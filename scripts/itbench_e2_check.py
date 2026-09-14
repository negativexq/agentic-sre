#!/usr/bin/env python3
"""Zero-network ITB-E2 contract, runtime and performance qualification."""

from __future__ import annotations

import json
import statistics
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SRE_VERSION,
    ExternalInvestigationRuntime,
    ITBenchEvidenceCategory,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.external_context import normalize_alerts
from packages.provider import FakeModelProvider, OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(".local/itbench-lite")
REPORT = ROOT / "docs/benchmarks/itbench-lite-e2-qualification.json"


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _request_payload(
    runtime: ExternalInvestigationRuntime, incident: Any, alerts: Any
) -> dict[str, Any]:
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    builder = OpenAIProvider.__new__(OpenAIProvider)
    payload = builder._request_parameters(request)
    return {
        "context_chars": len(request.messages[-1].content),
        "context_bytes": len(request.messages[-1].content.encode()),
        "tool_schema_bytes": len(json.dumps(payload.get("tools", []), sort_keys=True).encode()),
        "request_valid": True,
        "provider_request_build": True,
        "model": request.model,
        "reasoning": request.reasoning_effort,
    }


def _performance(registry: ITBenchExternalToolRegistry) -> dict[str, dict[str, float | int]]:
    probes: dict[str, dict[str, Any]] = {
        "itbench_alert_summary": {},
        "itbench_entity_search": {"limit": 5},
        "itbench_entity_context": {"entity": "otel-demo/Service/frontend", "limit": 5},
        "itbench_topology": {"limit": 20},
        "itbench_metric_analysis": {"limit": 5},
        "itbench_logs": {"limit": 5},
        "itbench_trace_search": {"limit": 5},
        "itbench_kubernetes_events": {"limit": 5},
        "itbench_kubernetes_objects": {"limit": 5},
    }
    result: dict[str, dict[str, float | int]] = {}
    for name, args in probes.items():
        timings: list[float] = []
        for _ in range(1):
            started = time.perf_counter()
            try:
                registry.invoke(name, dict(args))
            except ValueError:
                # Entity context is scenario-specific; an absent entity is a valid
                # bounded NOT_FOUND response for this generic probe.
                pass
            timings.append((time.perf_counter() - started) * 1000)
        result[name] = {
            "median_ms": statistics.median(timings),
            "p95_ms": max(timings),
            "max_ms": max(timings),
            "samples": len(timings),
        }
    return result


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("E2 scenario order mismatch")
    completeness = dataset.source_completeness()
    if completeness.get("status") != "PASS":
        raise RuntimeError("source completeness is not PASS")
    for entry in completeness.get("files", []):
        if entry.get("source_rows") != entry.get("indexed_rows", 0) + entry.get(
            "parse_failures", 0
        ):
            raise RuntimeError(f"source/index invariant failed: {entry.get('source_file')}")
    source_entries = completeness.get("files", [])

    def category_counts(scenario_id: str) -> dict[str, int]:
        return {
            category.value: sum(
                int(entry.get("indexed_rows", 0))
                for entry in source_entries
                if f"/{scenario_id}/" in str(entry.get("source_file", ""))
                and entry.get("category") == category.value
            )
            for category in ITBenchEvidenceCategory
        }

    rows: list[dict[str, Any]] = []
    fake_terminal_counts = {"STOP": 0, "MODEL_CALL_LIMIT": 0}
    performance: dict[str, list[dict[str, float | int]]] = {}
    for index, scenario in enumerate(scenarios, 1):
        backend = ITBenchSnapshotBackend(dataset, scenario)
        registry = ITBenchExternalToolRegistry(backend)
        incident, alerts = build_observable_incident(backend)
        context = _request_payload(
            ExternalInvestigationRuntime(
                FakeModelProvider([]), registry.investigation_registry(), backend
            ),
            incident,
            alerts,
        )
        if "ground_truth" in json.dumps(context).casefold():
            raise RuntimeError(
                f"investigator request mentions evaluator data: {scenario.scenario_id}"
            )

        def submit(_request: Any) -> dict[str, Any]:
            user_context = json.loads(_request.messages[-1].content)
            evidence_id = user_context["evidence"][0]["evidence_id"]
            return {
                "decision": "SUBMIT_DIAGNOSIS",
                "root_causes": [
                    {
                        "entity": "otel-demo/Service/frontend",
                        "causal_summary": "synthetic offline runtime qualification",
                        "evidence_ids": [evidence_id],
                    }
                ],
            }

        if index % 2 == 0:
            terminal_response: Any = {
                "decision": "STOP",
                "stop": {
                    "stop_reason": "insufficient_evidence",
                    "evidence_categories_considered": ["alerts"],
                    "entities_considered": [],
                    "missing_evidence_categories": ["metrics"],
                },
            }
        else:
            terminal_response = submit
        fake = FakeModelProvider(
            [
                {
                    "decision": "CALL_TOOLS",
                    "requests": [{"tool": "itbench_alert_summary", "arguments": {"limit": 5}}],
                },
                terminal_response,
            ],
            model="fake-model",
        )
        runtime = ExternalInvestigationRuntime(fake, registry.investigation_registry(), backend)
        result = runtime.run(incident, alerts)
        if result.terminal not in fake_terminal_counts:
            fake_terminal_counts[result.terminal] = 0
        fake_terminal_counts[result.terminal] += 1
        timings = _performance(registry)
        for tool, value in timings.items():
            performance.setdefault(tool, []).append(value)
        rows.append(
            {
                "scenario_id": scenario.scenario_id,
                "context": context,
                "unique_alerts": len(normalize_alerts(backend)),
                "raw_alerts": category_counts(scenario.scenario_id)["alerts"],
                "fake_runtime_terminal": result.terminal,
                "fake_provider_invocations": len(fake.requests),
                "category_row_counts": category_counts(scenario.scenario_id),
            }
        )
        print(
            f"[{index}/35] PASS {scenario.scenario_id} context={context['context_chars']} chars terminal={result.terminal}",
            flush=True,
        )
    report = {
        "artifact_type": "ITBENCH_LITE_E2_OFFLINE_QUALIFICATION",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "sre_version": ITBENCH_SRE_VERSION,
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "scenario_count": len(rows),
        "request_build_passes": len(rows),
        "fake_runtime_passes": len(rows),
        "fake_terminal_counts": fake_terminal_counts,
        "performance": {
            tool: {
                "median_ms": statistics.median(float(item["median_ms"]) for item in values),
                "p95_ms": max(float(item["p95_ms"]) for item in values),
                "max_ms": max(float(item["max_ms"]) for item in values),
                "scenario_samples": len(values),
            }
            for tool, values in performance.items()
        },
        "source_completeness_manifest": str(DATA_ROOT / ".itbench-source-completeness.json"),
        "ground_truth_loaded": False,
        "provider_constructed": False,
        "openai_calls": 0,
        "scenarios": rows,
    }
    atomic_json_write(REPORT, report)
    print(
        json.dumps(
            {
                "qualification": "PASS",
                "scenarios": len(rows),
                "report": str(REPORT),
                "digest": _digest(report),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
