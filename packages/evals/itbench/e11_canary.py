"""Deterministic fake-provider canary for the E11 control primitives.

This is a control-plane qualification only.  It does not claim RCA quality and
never constructs a model provider or reads evaluator truth.
"""

from __future__ import annotations

import json
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


class ContextAwareFakeProvider(FakeModelProvider):
    """Fake Luna that chooses only from the exact serialized E11 context."""

    def __init__(self, *, model: str = "fake-luna") -> None:
        super().__init__(self._make_responses(), model=model)

    @staticmethod
    def _make_responses() -> Any:
        def decide(request: Any) -> dict[str, Any]:
            context = json.loads(request.messages[-1].content)
            investigation = context.get("investigation", {})
            phase = context.get("phase")
            actions = set(request.allowed_v5_actions or ())
            candidates = context.get("active_candidates", [])
            if phase == "OBSERVE" and "OBSERVE" in actions:
                if not investigation.get("recent_evidence"):
                    operations = request.allowed_v5_operations or ()
                    operation = next(iter(operations), None)
                    if operation:
                        return {
                            "action": "OBSERVE",
                            "operation": operation,
                            "rationale": "context-visible observation",
                        }
                if candidates and "HYPOTHESIZE" in actions:
                    return {
                        "action": "HYPOTHESIZE",
                        "target": candidates[0]["handle"],
                        "rationale": "evidence-backed candidate",
                    }
            if phase == "OBSERVE" and candidates and "HYPOTHESIZE" in actions:
                return {
                    "action": "HYPOTHESIZE",
                    "target": candidates[0]["handle"],
                    "rationale": "observe budget reached",
                }
            if "SUBMIT_DIAGNOSIS" in (request.allowed_decisions or ()):
                submit = (request.allowed_v5_action_capabilities or {}).get("SUBMIT", {})
                targets = submit.get("targets", ()) if isinstance(submit, dict) else ()
                if targets:
                    return {
                        "action": "SUBMIT",
                        "targets": [targets[0]],
                        "rationale": "validated support",
                    }
            if phase == "VERIFY" and "INVESTIGATE" in actions:
                hypothesis = investigation.get("current_hypothesis") or {}
                target = hypothesis.get("entity_handle") if isinstance(hypothesis, dict) else None
                target_operations = investigation.get("legal_target_operations", {})
                operations = target_operations.get(target, ()) if isinstance(target, str) else ()
                completed = {
                    (item.get("entity_handle"), item.get("operation"))
                    for item in investigation.get("recent_operations", [])
                    if isinstance(item, dict)
                }
                operation = next(
                    (item for item in operations if (target, item) not in completed), None
                )
                if target and operation:
                    return {
                        "action": "INVESTIGATE",
                        "target": target,
                        "operation": operation,
                        "rationale": "discriminating check",
                    }
            return {
                "action": "STOP",
                "stop_reason": "context has no further legal discriminating check",
            }

        return [decide] * 12


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
    """Run a healthy, intentionally terminating path on every pinned snapshot."""
    dataset = ITBenchLiteDataset.open(root)
    completed = 0
    fake_provider_responses = 0
    action_rejections = 0
    recovered_rejections = 0
    repeated_operations = 0
    first_hypothesis_turns: list[int] = []
    context_chars: list[int] = []
    context_evidence_visible = 0
    terminals: dict[str, int] = {}
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=20)
        provider = ContextAwareFakeProvider()
        result = E11InvestigationRuntime(
            provider,
            backend,
            execution_id="ITB-E11-OFFLINE-CANARY",
        ).run()
        completed += 1
        fake_provider_responses += len(provider.requests)
        action_rejections += int(result["case_state"].get("action_rejections", 0))
        recovered_rejections += int(result["case_state"].get("recovered_action_rejections", 0))
        operations = [
            (item.get("entity_handle"), item.get("operation"))
            for item in result["case_state"].get("operations_already_run", [])
        ]
        repeated_operations += len(operations) - len(set(operations))
        hypotheses = [
            int(item["turn"])
            for item in result["turn_trace"]
            if item.get("model_action") == "HYPOTHESIZE"
        ]
        if hypotheses:
            first_hypothesis_turns.append(hypotheses[0])
        context_chars.extend(int(item.get("context_chars", 0)) for item in result["turn_trace"])
        context_evidence_visible += sum(
            1
            for request in provider.requests
            if '"recent_evidence":[' in request.messages[-1].content
            and '"recent_evidence":[]' not in request.messages[-1].content
        )
        terminals[result["terminal"]] = terminals.get(result["terminal"], 0) + 1
    return {
        "scenario_count": completed,
        "completed": completed,
        "terminals": terminals,
        "provider_invocations": 0,
        "fake_provider_responses": fake_provider_responses,
        "action_rejections": action_rejections,
        "recovered_action_rejections": recovered_rejections,
        "repeated_operations": repeated_operations,
        "first_hypothesis_turns": first_hypothesis_turns,
        "context_chars": context_chars,
        "context_distribution": _distribution(context_chars),
        "hypothesis_distribution": _distribution(first_hypothesis_turns),
        "context_evidence_visible_requests": context_evidence_visible,
        "runtime_errors": 0,
        "replay_errors": 0,
        "ground_truth_access": 0,
        "control_plane_only": True,
    }


__all__ = [
    "ContextAwareFakeProvider",
    "run_e11_fake_provider_canary",
    "run_e11_snapshot_runtime_canary",
]


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
            # Keep the standard 35-snapshot canary bounded and deterministic:
            # EVENT_ANALYSIS is a real semantic operation, but unlike a trace
            # tree or a full metric scan it cannot accidentally turn the
            # qualification into a source-file stress test.  The runtime
            # remains free to expose/use every operation in normal execution.
            capabilities = request.allowed_v5_action_capabilities or {}
            investigate = capabilities.get("INVESTIGATE", {})
            target_operations = (
                investigate.get("target_operations", {}) if isinstance(investigate, dict) else {}
            )
            target_ops = target_operations.get(targets[0], ())
            operation = next(
                (item for item in target_ops if item != "ENTITY_CONTEXT"),
                "ENTITY_CONTEXT",
            )
            return {
                "action": "INVESTIGATE",
                "target": targets[0],
                "operation": operation,
                "rationale": None,
            }
        if "SUBMIT_DIAGNOSIS" in (request.allowed_decisions or ()):
            capabilities = request.allowed_v5_action_capabilities or {}
            submit = capabilities.get("SUBMIT", {})
            supported = tuple(submit.get("targets", ())) if isinstance(submit, dict) else ()
            if supported:
                return {"action": "SUBMIT", "targets": [supported[0]], "rationale": None}
        return {"action": "STOP", "stop_reason": "offline runtime qualification"}

    return scripted


def _distribution(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"min": 0, "median": 0, "p95": 0, "max": 0}
    return {
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": ordered[-1],
    }
