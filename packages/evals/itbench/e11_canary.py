"""Deterministic fake-provider canary for the E11 control primitives.

This is a control-plane qualification only.  It does not claim RCA quality and
never constructs a model provider or reads evaluator truth.
"""

from __future__ import annotations

from typing import Any, cast

from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e11_control import (
    CandidateStatus,
    E11CaseMemory,
    EvidenceAssessment,
    e11_control_surface,
)
from packages.evals.itbench.e11_observability import (
    ObservedEntityCatalog,
    RankedCandidate,
    RetrievalEvidence,
)
from packages.evals.itbench.e11_runtime import E11InvestigationRuntime
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.provider.fake import FakeModelProvider


def run_e11_fake_provider_canary(scenario_ids: tuple[str, ...]) -> dict[str, Any]:
    """Exercise observe → hypothesize → investigate → submit for each scenario."""
    completed = 0
    for scenario_id in scenario_ids:
        catalog = ObservedEntityCatalog(scenario_id=scenario_id)
        entity = catalog.add(
            canonical=f"otel-demo/Deployment/canary-{scenario_id.lower()}",
            identity_type="KubernetesEntity",
            namespace="otel-demo",
            kind="Deployment",
            name=f"canary-{scenario_id.lower()}",
            source_category="k8s_objects",
            provenance="DIRECT_K8S_OBJECT",
            evidence_ref=f"fixture:{scenario_id}",
        )
        candidate = RankedCandidate(
            handle=entity.handle,
            canonical=entity.canonical,
            rank=1,
            score=1.0,
            family="otel-demo/Deployment",
            retrieval_evidence=(
                RetrievalEvidence("FAILURE_EVENT", f"fixture:{scenario_id}", "canary failure", 1.0),
            ),
        )
        memory = E11CaseMemory(scenario_id=scenario_id, catalog=catalog)
        memory.initialize((candidate,))
        assert e11_control_surface(memory).actions[0] == "OBSERVE"
        memory.observe((candidate,))
        memory.hypothesize(entity.handle)
        assert "INVESTIGATE" in e11_control_surface(memory).actions
        evidence_ref = memory.add_evidence(entity.handle, "ENTITY_CONTEXT", {"source": "canary"})
        memory.assess(
            entity.handle,
            evidence_ref,
            cast(Any, EvidenceAssessment.SUPPORTS),
            dimension="causal",
        )
        assert memory.candidate_status[entity.handle] == CandidateStatus.SUPPORTED
        assert memory.submit_ready((entity.handle,))
        completed += 1
    return {
        "scenario_count": len(scenario_ids),
        "completed": completed,
        "provider_invocations": 0,
        "ground_truth_access": 0,
        "invalid_evidence_refs": 0,
        "stable_handles": True,
        "control_plane_only": True,
    }


def run_e11_snapshot_runtime_canary(root: Any) -> dict[str, Any]:
    """Run the real E11 loop against every pinned snapshot with a fake provider."""
    dataset = ITBenchLiteDataset.open(root)
    completed = 0
    fake_provider_responses = 0
    terminals: dict[str, int] = {}
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=20)
        state = {"observed": False, "hypothesized": False, "investigated": False}

        scripted = _script_for_state(state)

        provider = FakeModelProvider([scripted] * 12, model="fake-luna")
        result = E11InvestigationRuntime(
            provider,
            backend,
            execution_id="ITB-E11-OFFLINE-CANARY",
        ).run()
        completed += 1
        fake_provider_responses += len(provider.requests)
        terminals[result["terminal"]] = terminals.get(result["terminal"], 0) + 1
    return {
        "scenario_count": completed,
        "completed": completed,
        "terminals": terminals,
        "provider_invocations": 0,
        "fake_provider_responses": fake_provider_responses,
        "ground_truth_access": 0,
        "control_plane_only": True,
    }


__all__ = ["run_e11_fake_provider_canary", "run_e11_snapshot_runtime_canary"]


def _script_for_state(state: dict[str, bool]) -> Any:
    def scripted(request: Any) -> dict[str, Any]:
        actions = set(request.allowed_v5_actions or ())
        targets = tuple(request.allowed_v5_targets or ())
        if not state["observed"] and "OBSERVE" in actions:
            state["observed"] = True
            return {"action": "OBSERVE", "operation": "INCIDENT_OVERVIEW", "rationale": None}
        if not state["hypothesized"] and "HYPOTHESIZE" in actions and targets:
            state["hypothesized"] = True
            return {"action": "HYPOTHESIZE", "target": targets[0], "rationale": None}
        if not state["investigated"] and "INVESTIGATE" in actions and targets:
            state["investigated"] = True
            operation = next(iter(request.allowed_v5_operations or ()), "ENTITY_CONTEXT")
            return {
                "action": "INVESTIGATE",
                "target": targets[0],
                "operation": operation,
                "rationale": None,
            }
        if "SUBMIT_DIAGNOSIS" in (request.allowed_decisions or ()) and targets:
            return {"action": "SUBMIT", "targets": [targets[0]], "rationale": None}
        return {"action": "STOP", "stop_reason": "offline runtime qualification"}

    return scripted
