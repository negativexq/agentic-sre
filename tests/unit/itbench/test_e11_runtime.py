"""End-to-end E11 control-path qualification with a fake provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import ITBenchEvidenceCategory, ITBenchLiteDataset, ITBenchScenario
from packages.evals.itbench.e11_control import (
    E11CaseMemory,
    EvidenceAssessment,
)
from packages.evals.itbench.e11_runtime import E11InvestigationRuntime
from packages.provider.fake import FakeModelProvider


def _backend(tmp_path: Path, **kwargs: Any) -> Any:
    root = tmp_path / "Scenario-runtime"
    root.mkdir(parents=True)
    (root / "alerts.json").write_text(json.dumps({"data": {"alerts": []}}), encoding="utf-8")

    def write_tsv(name: str, rows: list[dict[str, str]], fields: tuple[str, ...]) -> str:
        content = ["\t".join(fields)]
        content.extend("\t".join(row.get(field, "") for field in fields) for row in rows)
        (root / name).write_text("\n".join(content) + "\n", encoding="utf-8")
        return name

    objects = kwargs.get("object_bodies", [])
    events = kwargs.get("event_bodies", [])
    object_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in objects
    ]
    event_rows = [
        {"Timestamp": "2025-01-01T00:00:00Z", "Body": json.dumps(body)} for body in events
    ]
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
        ITBenchEvidenceCategory.K8S_OBJECTS: (
            write_tsv("objects.tsv", object_rows, ("Timestamp", "Body")),
        ),
        ITBenchEvidenceCategory.K8S_EVENTS: (
            write_tsv("events.tsv", event_rows, ("Timestamp", "Body")),
        ),
        ITBenchEvidenceCategory.METRICS: (
            write_tsv("metrics.tsv", [], ("Timestamp", "metric_name", "value")),
        ),
        ITBenchEvidenceCategory.LOGS: (write_tsv("logs.tsv", [], ("Timestamp", "Body")),),
        ITBenchEvidenceCategory.TRACES: (
            write_tsv("traces.tsv", [], ("Timestamp", "ServiceName", "Body")),
        ),
    }
    scenario = ITBenchScenario(
        scenario_id="Scenario-runtime",
        snapshot_path=str(root),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files=files,
    )
    from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

    return ITBenchSnapshotBackend(cast(ITBenchLiteDataset, object()), scenario, max_rows=50)


def test_fake_provider_uses_real_e11_runtime_path(tmp_path: Path) -> None:
    backend = _backend(
        tmp_path,
        object_bodies=[
            {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "prod"}}
        ],
        event_bodies=[
            {
                "involvedObject": {"kind": "Deployment", "name": "checkout", "namespace": "prod"},
                "reason": "BackOff",
                "type": "Warning",
            }
        ],
    )
    provider = FakeModelProvider(
        [
            {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None},
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": None},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "EVENT_ANALYSIS",
                "rationale": None,
            },
            {"action": "SUBMIT", "targets": ["C001"], "rationale": None},
        ]
    )
    result = E11InvestigationRuntime(provider, backend, execution_id="offline").run()
    assert result["terminal"] == "SUBMIT"
    assert result["safety"] == {
        "ground_truth_exposure": 0,
        "cross_scenario_evidence": 0,
        "writes": 0,
        "arbitrary_execution": 0,
    }
    assert provider.accounting_snapshot().provider_invocations == 4
    assert any(item.get("operation") == "EVENT_ANALYSIS" for item in result["turn_trace"])


def test_no_data_evidence_cannot_support_candidate() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
        RankedCandidate,
        RetrievalEvidence,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    entity = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    memory = E11CaseMemory(scenario_id="offline", catalog=catalog)
    memory.initialize(
        (
            RankedCandidate(
                entity.handle,
                entity.canonical,
                1,
                1.0,
                "prod/Deployment",
                (RetrievalEvidence("ALERT_LINK", "alert:1", "bounded", 1.0),),
            ),
        )
    )
    memory.hypothesize(entity.handle)
    evidence = memory.add_evidence(
        entity.handle,
        "LOG_ANALYSIS",
        {"result_status": "NO_DATA", "usable": False, "patterns": []},
    )
    try:
        memory.assess(entity.handle, evidence, EvidenceAssessment.SUPPORTS, dimension="causal")
    except ValueError as error:
        assert "unusable" in str(error)
    else:
        raise AssertionError("NO_DATA evidence was accepted as causal support")


def test_contradiction_can_be_explicitly_superseded() -> None:
    from packages.evals.itbench.e11_observability import (
        ObservedEntityCatalog,
        RankedCandidate,
        RetrievalEvidence,
    )

    catalog = ObservedEntityCatalog(scenario_id="offline")
    entity = catalog.add(
        canonical="prod/Deployment/checkout",
        identity_type="KubernetesEntity",
        namespace="prod",
        kind="Deployment",
        name="checkout",
        source_category="k8s_objects",
        provenance="DIRECT_K8S_OBJECT",
        evidence_ref="object:checkout",
    )
    memory = E11CaseMemory(scenario_id="offline", catalog=catalog)
    candidate = RankedCandidate(
        entity.handle,
        entity.canonical,
        1,
        1.0,
        "prod/Deployment",
        (RetrievalEvidence("FAILURE_EVENT", "event:1", "bounded", 1.0),),
    )
    memory.initialize((candidate,))
    memory.hypothesize(entity.handle)
    contradiction = memory.add_evidence(entity.handle, "EVENT_ANALYSIS", {"reason": "none"})
    memory.assess(entity.handle, contradiction, EvidenceAssessment.CONTRADICTS, dimension="causal")
    support = memory.add_evidence(entity.handle, "LOG_ANALYSIS", {"error_count": 1})
    memory.assess(
        entity.handle,
        support,
        EvidenceAssessment.SUPPORTS,
        dimension="causal",
        supersedes_evidence_ref=contradiction,
    )
    assert memory.submit_ready((entity.handle,))
