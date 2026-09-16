"""Deterministic fake-provider canary for the E11 control primitives.

This is a control-plane qualification only.  It does not claim RCA quality and
never constructs a model provider or reads evaluator truth.
"""

from __future__ import annotations

from typing import Any, cast

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


__all__ = ["run_e11_fake_provider_canary"]
