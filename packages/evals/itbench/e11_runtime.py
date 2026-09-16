"""Provider-injected E11 investigation loop.

This is the production control path for both the offline fake provider and a
future explicitly-authorized OpenAI provider.  It deliberately owns no
ground-truth or evaluator imports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntityPrediction,
    ITBenchEvidenceCategory,
    parse_canonical_entity,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9SemanticOperations
from packages.evals.itbench.e11_context import build_e11_context
from packages.evals.itbench.e11_control import E11_PROMPT, E11CaseMemory, EvidenceAssessment
from packages.evals.itbench.e11_observability import (
    E11_B1_CONFIG,
    E11RetrievalConfig,
    RankedCandidate,
    RetrievalEvidence,
    build_observed_entity_catalog,
    rank_observed_candidates,
)
from packages.evals.itbench.e11_operations import available_e11_operations, comparable_peer_count
from packages.evals.itbench.external_contracts import (
    ITBENCH_EXTERNAL_PROTOCOL_V5,
    E9Action,
    ITBenchInvestigationDecisionV5,
)
from packages.evals.itbench.incident import build_observable_incident
from packages.provider import ModelMessage, ModelProvider, ModelRequest


@dataclass(frozen=True, slots=True)
class E11RuntimeLimits:
    max_model_calls: int = 12
    max_tool_calls: int = 24
    max_agent_turns: int = 12
    max_wall_time_seconds: int = 240
    max_consecutive_rejected_actions: int = 2


MAX_OBSERVE_ACTIONS = 3


class E11InvestigationRuntime:
    """One runtime loop with a replaceable provider, never a replaceable FSM."""

    def __init__(
        self,
        provider: ModelProvider,
        backend: Any,
        *,
        limits: E11RuntimeLimits | None = None,
        execution_id: str = "ITB-E11",
        shortlist_size: int | None = None,
        telemetry_ranking: bool | None = None,
        retrieval_config: E11RetrievalConfig | None = None,
    ) -> None:
        self.provider = provider
        self.backend = backend
        if not hasattr(backend, "_semantic_capability_cache"):
            backend._semantic_capability_cache = {}
        self.limits = limits or E11RuntimeLimits()
        self.execution_id = execution_id
        config = retrieval_config or E11_B1_CONFIG
        if shortlist_size is not None or telemetry_ranking is not None:
            config = E11RetrievalConfig(
                include_telemetry_in_catalog=config.include_telemetry_in_catalog,
                include_telemetry_in_ranking=(
                    config.include_telemetry_in_ranking
                    if telemetry_ranking is None
                    else telemetry_ranking
                ),
                use_direct_topology=config.use_direct_topology,
                use_causal_propagation=config.use_causal_propagation,
                use_namespace_context=config.use_namespace_context,
                use_temporal=config.use_temporal,
                diversity=config.diversity,
                shortlist_size=(
                    config.shortlist_size if shortlist_size is None else shortlist_size
                ),
            )
        self.retrieval_config = config
        self.shortlist_size = config.shortlist_size
        self._runtime_signals: dict[str, list[RetrievalEvidence]] = {}
        self._incident_available = False
        self._incident_context: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        self._runtime_signals = {}
        started = monotonic()
        scenario_id = str(self.backend.scenario.scenario_id)
        run_id = uuid4()
        telemetry_records = {
            category: tuple(
                self.backend.records(category)
                if callable(getattr(self.backend, "records", None))
                else self.backend.complete_source_records(category)
            )
            for category in (
                ITBenchEvidenceCategory.METRICS,
                ITBenchEvidenceCategory.LOGS,
                ITBenchEvidenceCategory.TRACES,
            )
        }
        catalog = build_observed_entity_catalog(
            self.backend,
            include_telemetry=self.retrieval_config.include_telemetry_in_catalog,
            telemetry_records=telemetry_records,
        )
        ranking = self._rank(catalog, telemetry_records)
        initial_ranking = ranking
        memory = E9CaseMemory(execution_id=self.execution_id, scenario_id=scenario_id)
        memory.discover_entities(tuple(entity.as_dict() for entity in catalog.entities()), turn=0)
        memory.set_active_shortlist(tuple(item.handle for item in ranking), turn=0)
        evidence_memory = E11CaseMemory(scenario_id=scenario_id, catalog=catalog)
        evidence_memory.initialize(ranking)
        memory.append(
            "RANKING_REVISION",
            0,
            {
                "revision": 0,
                "triggering_evidence_ref": None,
                "handles": [candidate.handle for candidate in ranking],
            },
        )
        try:
            incident, alerts = build_observable_incident(self.backend)
            self._incident_context = _incident_context(scenario_id, incident, alerts)
        except ValueError:
            # Small unit fixtures may intentionally omit alerts; temporal
            # capability is then unavailable rather than fabricated.
            incident = None
            self._incident_context = {"scenario_id": scenario_id}
        self._incident_available = incident is not None
        operations = E9SemanticOperations(self.backend, memory, incident, enable_discovery=True)
        turns: list[dict[str, Any]] = []
        recent_observations: list[dict[str, Any]] = []
        usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
        terminal = "MODEL_STEP_LIMIT"
        consecutive_rejections = 0
        for turn in range(1, min(self.limits.max_model_calls, self.limits.max_agent_turns) + 1):
            if monotonic() - started >= self.limits.max_wall_time_seconds:
                terminal = "WALL_TIME_LIMIT"
                break
            candidates = self._ranked_for_memory(ranking, memory)
            surface = self._surface(memory, candidates, catalog, turn)
            context = build_e11_context(
                incident=self._incident_context,
                candidates=candidates,
                phase=str(memory.state.get("current_phase", "OBSERVE")),
                remaining_model_calls=self.limits.max_model_calls - turn + 1,
                remaining_semantic_actions=self.limits.max_tool_calls
                - int(memory.state.get("semantic_actions_used", 0)),
                investigation_state=self._investigation_state(memory),
                observation_evidence=tuple(recent_observations[-8:]),
                legal_next_actions=tuple(surface["actions"]),
                legal_target_operations=surface.get("target_operations", {}),
            )
            request = self._request(run_id, context, surface)
            trace: dict[str, Any] = {
                "turn": turn,
                "phase_before": memory.state.get("current_phase"),
                "context_chars": len(context),
                "provider_exposed_actions": list(request.allowed_v5_actions or ())
                + list(request.allowed_decisions or ()),
                "provider_exposed_operations": list(request.allowed_v5_operations or ()),
                "provider_exposed_targets": list(request.allowed_v5_targets or ()),
                "provider_exposed_target_operations": surface.get("target_operations", {}),
                "accepted": False,
            }
            memory.append("MODEL_STEP", turn, {})
            usage["model_calls"] += 1
            try:
                response = self.provider.complete(request)
                usage["input_tokens"] += response.input_tokens
                usage["output_tokens"] += response.output_tokens
                usage["latency_ms"] += response.latency_ms
                payload = dict(response.structured_output)
                if isinstance(payload.get("action"), str):
                    payload["action"] = E9Action(payload["action"])
                decision = ITBenchInvestigationDecisionV5.model_validate(payload)
            except (ValidationError, ValueError) as error:
                consecutive_rejections += 1
                memory.append(
                    "ACTION_REJECTED",
                    turn,
                    {
                        "code": "INVALID_MODEL_ACTION",
                        "reason": str(error)[:200],
                        "attempted_action": payload.get("action"),
                        "attempted_target": payload.get("target"),
                        "attempted_targets": payload.get("targets", []),
                        "attempted_operation": payload.get("operation"),
                        "valid_actions": list(request.allowed_v5_actions or ()),
                        "valid_operations": list(request.allowed_v5_operations or ()),
                    },
                )
                trace.update(
                    {"decision": "ACTION_REJECTED", "rejection_code": "INVALID_MODEL_ACTION"}
                )
                turns.append(trace)
                if consecutive_rejections >= self.limits.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            try:
                result = self._apply(
                    decision, memory, evidence_memory, operations, surface, catalog, turn
                )
            except ValueError as error:
                consecutive_rejections += 1
                memory.append(
                    "ACTION_REJECTED",
                    turn,
                    {
                        "code": "INVALID_TRANSITION",
                        "reason": str(error)[:200],
                        "attempted_action": decision.action.value,
                        "attempted_target": decision.target,
                        "attempted_targets": list(decision.targets),
                        "attempted_operation": decision.operation,
                        "valid_actions": list(request.allowed_v5_actions or ()),
                        "valid_operations": list(request.allowed_v5_operations or ()),
                    },
                )
                trace.update(
                    {"decision": "ACTION_REJECTED", "rejection_code": "INVALID_TRANSITION"}
                )
                turns.append(trace)
                if consecutive_rejections >= self.limits.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            # Parsing alone is not an accepted action.  Only a fully applied
            # action clears the consecutive rejection window.
            consecutive_rejections = 0
            trace.update(result)
            trace["accepted"] = True
            if isinstance(result.get("evidence_summary"), dict):
                recent_observations.append(
                    {
                        "evidence_ref": result.get("evidence_created", [None])[0],
                        "operation": result.get("operation"),
                        "target": result.get("target"),
                        "result_status": result.get("result_status"),
                        "finding": result["evidence_summary"],
                    }
                )
            memory.append("ACTION_ACCEPTED", turn, {"action": decision.action.value})
            turns.append(trace)
            if result.get("terminal"):
                terminal = str(result["terminal"])
                break
            ranking = self._rank(catalog, telemetry_records)
            if evidence_memory.evidence:
                trigger = next(reversed(evidence_memory.evidence))
                previous_ranks = {candidate.handle: candidate.rank for candidate in candidates}
                new_ranks = {candidate.handle: candidate.rank for candidate in ranking}
                evidence_memory.rerank(ranking, trigger)
                memory.set_active_shortlist(tuple(item.handle for item in ranking), turn=turn)
                memory.append(
                    "RANKING_REVISION",
                    turn,
                    {
                        "revision": len(memory.state.get("ranking_history", [])) + 1,
                        "triggering_evidence_ref": trigger,
                        "handles": [candidate.handle for candidate in ranking],
                        "previous_ranks": previous_ranks,
                        "new_ranks": new_ranks,
                    },
                )
        safety = {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        }
        return {
            "terminal": terminal,
            "turn_trace": turns,
            "case_state": memory.projection(),
            "evidence_ledger": evidence_memory.evidence,
            "assessment_history": [item.as_dict() for item in evidence_memory.assessments],
            "event_log": [event.as_dict() for event in memory.events],
            "native_artifact": {"scenario_id": scenario_id, "terminal": terminal},
            "agent_output": self._export_agent_output(
                scenario_id, terminal, memory, evidence_memory, catalog
            ).model_dump(mode="json"),
            "usage": {**usage, "safety": safety},
            "safety": safety,
            "catalog": [entity.as_dict() for entity in catalog.entities()],
            "initial_ranking": [candidate.as_dict() for candidate in initial_ranking],
            "retrieval_config": self.retrieval_config.__dict__
            if hasattr(self.retrieval_config, "__dict__")
            else {
                "include_telemetry_in_catalog": self.retrieval_config.include_telemetry_in_catalog,
                "include_telemetry_in_ranking": self.retrieval_config.include_telemetry_in_ranking,
                "use_direct_topology": self.retrieval_config.use_direct_topology,
                "use_causal_propagation": self.retrieval_config.use_causal_propagation,
                "use_namespace_context": self.retrieval_config.use_namespace_context,
                "use_temporal": self.retrieval_config.use_temporal,
                "diversity": self.retrieval_config.diversity,
                "shortlist_size": self.retrieval_config.shortlist_size,
            },
        }

    def _request(self, run_id: Any, context: str, surface: dict[str, Any]) -> ModelRequest:
        actions = tuple(surface["actions"])
        decisions: list[str] = []
        if any(action in actions for action in ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "REVISE")):
            decisions.append("CALL_TOOLS")
        if "SUBMIT" in actions:
            decisions.append("SUBMIT_DIAGNOSIS")
        if "STOP" in actions:
            decisions.append("STOP")
        return ModelRequest(
            run_id=run_id,
            messages=[
                ModelMessage(role="system", content=E11_PROMPT),
                ModelMessage(role="user", content=context),
            ],
            response_schema_name=ITBENCH_EXTERNAL_PROTOCOL_V5,
            response_schema=ITBenchInvestigationDecisionV5.model_json_schema(),
            model="gpt-5.6-luna",
            reasoning_effort="none",
            max_output_tokens=1200,
            timeout_ms=20_000,
            allowed_decisions=cast(Any, tuple(decisions)),
            allowed_v5_actions=tuple(
                action for action in actions if action not in {"SUBMIT", "STOP"}
            ),
            allowed_v5_operations=tuple(surface["operations"]),
            allowed_v5_targets=tuple(surface["targets"]),
            allowed_v5_action_capabilities=surface["capabilities"],
            allowed_tool_names=(),
            tool_schemas=(),
        )

    def _rank(
        self,
        catalog: Any,
        telemetry_records: dict[ITBenchEvidenceCategory, tuple[dict[str, Any], ...]],
    ) -> tuple[RankedCandidate, ...]:
        """Apply the exact configured retrieval policy used by qualification."""
        return rank_observed_candidates(
            self.backend,
            catalog,
            limit=self.retrieval_config.shortlist_size,
            diversity=self.retrieval_config.diversity,
            include_telemetry=self.retrieval_config.include_telemetry_in_ranking,
            telemetry_records=telemetry_records,
            use_direct_topology=self.retrieval_config.use_direct_topology,
            use_causal_propagation=self.retrieval_config.use_causal_propagation,
            use_namespace_context=self.retrieval_config.use_namespace_context,
            use_temporal=self.retrieval_config.use_temporal,
            runtime_signals={key: tuple(value) for key, value in self._runtime_signals.items()},
        )

    def _surface(
        self,
        memory: E9CaseMemory,
        candidates: tuple[RankedCandidate, ...],
        catalog: Any,
        turn: int,
    ) -> dict[str, Any]:
        handles = tuple(candidate.handle for candidate in candidates)
        phase = str(memory.state.get("current_phase", "OBSERVE"))
        actions: tuple[str, ...]
        operations: tuple[str, ...]
        if phase == "OBSERVE":
            completed = {
                item.get("operation")
                for item in memory.state.get("operations_already_run", [])
                if item.get("entity_handle") is None
            }
            operations = tuple(
                operation
                for operation in (
                    "INCIDENT_OVERVIEW",
                    "ALERT_ANALYSIS",
                    "TOPOLOGY_ANALYSIS",
                    "ANOMALY_DISCOVERY",
                )
                if operation not in completed
            )
            observe_used = int(memory.state.get("observe_actions_used", 0))
            actions = (
                ("OBSERVE", "HYPOTHESIZE", "STOP")
                if observe_used < MAX_OBSERVE_ACTIONS and operations
                else ("HYPOTHESIZE", "STOP")
            )
            capabilities = {
                "OBSERVE": {"targets": (), "operations": operations},
                "HYPOTHESIZE": {"targets": handles, "operations": ()},
                "STOP": {"targets": (), "operations": ()},
            }
        else:
            current = memory.state.get("current_hypothesis")
            current_handle = current.get("entity_handle") if isinstance(current, dict) else None
            available: dict[str, tuple[str, ...]] = {}
            for handle in handles:
                if handle == current_handle:
                    entity = catalog.by_handle(handle)
                    available[handle] = (
                        tuple(
                            operation
                            for operation in available_e11_operations(
                                entity,
                                comparable_peers=comparable_peer_count(catalog, entity),
                                backend=self.backend,
                                incident_available=self._incident_available,
                            )
                            if not memory.has_operation(handle, operation)
                            or memory.recheck_allowed(handle, operation)
                        )
                        if entity is not None
                        else ()
                    )
                else:
                    # Alternative handles receive a bounded source-derived
                    # surface.  Full target capability checks are deferred
                    # until the model selects that alternative, avoiding a
                    # full telemetry scan for every visible candidate.
                    entity = catalog.by_handle(handle)
                    available[handle] = (
                        tuple(
                            operation
                            for operation in available_e11_operations(
                                entity,
                                comparable_peers=comparable_peer_count(catalog, entity),
                                backend=self.backend,
                                incident_available=self._incident_available,
                            )
                            if not memory.has_operation(handle, operation)
                            or memory.recheck_allowed(handle, operation)
                        )
                        if entity is not None
                        else ()
                    )
            operations = tuple(
                dict.fromkeys(operation for values in available.values() for operation in values)
            )
            supported = self._supported_handles(memory)
            actions = ("INVESTIGATE", "REVISE", "STOP")
            if supported:
                actions = ("INVESTIGATE", "REVISE", "SUBMIT", "STOP")
            capabilities = {
                "INVESTIGATE": {
                    "targets": handles,
                    "operations": operations,
                    "target_operations": available,
                },
                "REVISE": {
                    "targets": tuple(handle for handle in handles if handle != current_handle),
                    "operations": (),
                },
                "SUBMIT": {"targets": supported, "operations": ()},
                "STOP": {"targets": (), "operations": ()},
            }
        return {
            "actions": actions,
            "operations": operations,
            "targets": handles,
            "capabilities": capabilities,
            # Keep the union for the provider surface, but retain the
            # target-specific intersection for runtime validation.  A model
            # must not select an operation merely because it is available for
            # a different candidate in the same turn.
            "target_operations": available if phase != "OBSERVE" else {},
        }

    def _apply(
        self,
        decision: ITBenchInvestigationDecisionV5,
        memory: E9CaseMemory,
        evidence_memory: E11CaseMemory,
        operations: E9SemanticOperations,
        surface: dict[str, Any],
        catalog: Any,
        turn: int,
    ) -> dict[str, Any]:
        action = decision.action.value
        capability = surface["capabilities"].get(action)
        if capability is None:
            raise ValueError("action is not exposed in the current phase")
        targets = [handle for handle in (decision.target, *decision.targets) if handle]
        if any(handle not in capability["targets"] for handle in targets):
            raise ValueError("target is not currently visible")
        if action == "OBSERVE":
            if decision.operation not in capability["operations"]:
                raise ValueError("observation operation is not available")
            evidence = operations.execute(decision.operation or "", None, turn)
            self._sync_discovered_entities(memory, catalog, evidence.get("evidence_ref"), turn)
            return {
                "model_action": action,
                "operation": decision.operation,
                "evidence_created": [evidence["evidence_ref"]],
                "result_status": evidence.get("summary", {}).get("result_status"),
                "evidence_summary": evidence.get("summary", {}),
            }
        if action == "HYPOTHESIZE":
            evidence_memory.hypothesize(decision.target or "")
            memory.append(
                "HYPOTHESIS_PROPOSED",
                turn,
                {"entity_handle": decision.target, "rationale": decision.rationale or ""},
            )
            return {"model_action": action, "target": decision.target}
        if action == "REVISE":
            current = memory.state.get("current_hypothesis")
            if isinstance(current, dict) and decision.target == current.get("entity_handle"):
                raise ValueError("revision requires an alternative")
            evidence_memory.revise(decision.target or "")
            memory.append(
                "HYPOTHESIS_REVISED",
                turn,
                {
                    "hypothesis": {
                        "entity_handle": decision.target,
                        "rationale": decision.rationale or "",
                    }
                },
            )
            return {"model_action": action, "target": decision.target}
        if action == "INVESTIGATE":
            target_operations = surface.get("target_operations", {})
            legal_operations = target_operations.get(decision.target, capability["operations"])
            if decision.operation not in legal_operations:
                raise ValueError("investigation operation is not available")
            evidence = operations.execute(decision.operation or "", decision.target, turn)
            self._sync_discovered_entities(memory, catalog, evidence.get("evidence_ref"), turn)
            self._record_assessment(
                memory, evidence_memory, turn, decision.target, evidence, catalog
            )
            return {
                "model_action": action,
                "target": decision.target,
                "operation": decision.operation,
                "evidence_created": [evidence["evidence_ref"]],
                "result_status": evidence.get("summary", {}).get("result_status"),
                "evidence_summary": evidence.get("summary", {}),
            }
        if action == "SUBMIT":
            supported = self._supported_handles(memory)
            if (
                not decision.targets
                or any(handle not in supported for handle in decision.targets)
                or not evidence_memory.submit_ready(tuple(decision.targets))
            ):
                raise ValueError("submission requires runtime-supported candidates")
            memory.append("DIAGNOSIS_SUBMITTED", turn, {"targets": decision.targets})
            return {"model_action": action, "targets": decision.targets, "terminal": "SUBMIT"}
        memory.append(
            "CASE_STOPPED", turn, {"reason": decision.stop_reason or "insufficient evidence"}
        )
        return {"model_action": action, "terminal": "STOP", "stop_reason": decision.stop_reason}

    @staticmethod
    def _sync_discovered_entities(
        memory: E9CaseMemory, catalog: Any, evidence_ref: Any, turn: int
    ) -> None:
        """Bridge structured semantic discoveries into the E11 catalog."""
        for canonical, item in memory.state.get("discovered_entities", {}).items():
            if catalog.get(canonical) is not None:
                continue
            parts = canonical.split("/", 2)
            if len(parts) != 3 or not all(parts):
                continue
            namespace, kind, name = parts
            catalog.add(
                canonical=canonical,
                identity_type="UnresolvedObservedIdentity",
                namespace=namespace,
                kind=kind,
                name=name,
                source_category="semantic_discovery",
                provenance="TOPOLOGY_DERIVED",
                evidence_ref=str(evidence_ref or ""),
                identity_quality="MAPPED",
                handle=str(item.get("handle")) if isinstance(item, dict) else None,
            )

    def _record_assessment(
        self,
        memory: E9CaseMemory,
        evidence_memory: E11CaseMemory,
        turn: int,
        handle: str | None,
        result: dict[str, Any],
        catalog: Any,
    ) -> None:
        evidence_ref = result.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not isinstance(handle, str):
            return
        summary = result.get("summary", {})
        local_ref = evidence_memory.add_evidence(
            handle,
            str(result.get("operation", "")),
            summary if isinstance(summary, dict) else {},
        )
        assessment, dimension, reason, channel, weight = self._assess_semantic_result(
            str(result.get("operation", "")), summary
        )
        supersedes = None
        if assessment == EvidenceAssessment.SUPPORTS:
            prior = next(
                (
                    item
                    for item in reversed(evidence_memory.assessments)
                    if item.handle == handle
                    and item.assessment == EvidenceAssessment.CONTRADICTS
                    and item.dimension == dimension
                ),
                None,
            )
            supersedes = prior.evidence_ref if prior is not None else None
        memory.append(
            "EVIDENCE_ASSESSMENT",
            turn,
            {
                "entity_handle": handle,
                "evidence_handle": evidence_ref,
                "assessment": assessment,
                "dimension": dimension,
                "rationale": reason,
                "supersedes_evidence_ref": supersedes,
            },
        )
        if assessment == "SUPPORTS":
            memory.set_candidate_status(
                turn=turn, handle=handle, status="SUPPORTED", supporting_refs=(evidence_ref,)
            )
        else:
            if assessment == EvidenceAssessment.CONTRADICTS:
                memory.set_candidate_status(
                    turn=turn,
                    handle=handle,
                    status="CONTRADICTED",
                    contradicting_refs=(evidence_ref,),
                    rationale=reason,
                )
        evidence_memory.assess(
            handle,
            local_ref,
            cast(Any, assessment),
            dimension=dimension,
            rationale=reason,
            supersedes_evidence_ref=supersedes,
        )
        canonical = memory.resolve(handle)
        if canonical is not None and channel is not None:
            self._runtime_signals.setdefault(canonical, []).append(
                RetrievalEvidence(
                    channel,
                    evidence_ref,
                    reason,
                    weight if assessment == EvidenceAssessment.SUPPORTS else -weight,
                )
            )

    @staticmethod
    def _assess_semantic_result(
        operation: str, summary: Any
    ) -> tuple[str, str, str, str | None, float]:
        """Derive conservative polarity from structured operation output."""
        if not isinstance(summary, dict):
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "causal_mechanism",
                "non-structured result",
                None,
                0.0,
            )
        if operation == "EVENT_ANALYSIS":
            records = summary.get("records", [])
            diagnostic = any(
                any(
                    token in _event_text(item).casefold()
                    for token in (
                        "fail",
                        "error",
                        "backoff",
                        "crash",
                        "oom",
                        "evict",
                        "mount",
                        "image",
                    )
                )
                for item in records
            )
            if diagnostic:
                return (
                    EvidenceAssessment.SUPPORTS,
                    "failure_signature",
                    "diagnostic incident event observed",
                    "FAILURE_EVENT",
                    7.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "failure_signature",
                "no diagnostic event support",
                None,
                0.0,
            )
        if operation == "SPEC_ANALYSIS":
            findings = summary.get("causal_findings", [])
            if isinstance(findings, list) and findings:
                return (
                    EvidenceAssessment.SUPPORTS,
                    "configuration",
                    "structured configuration or targeting relation explains the incident",
                    "OBSERVED_RELATION",
                    8.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "configuration",
                "object/spec context without a discriminating causal relation",
                None,
                0.0,
            )
        if operation == "LOG_ANALYSIS":
            patterns = summary.get("patterns")
            if summary.get("data_available") and isinstance(patterns, list) and patterns:
                return (
                    EvidenceAssessment.SUPPORTS,
                    "failure_signature",
                    "target-specific structured error pattern observed",
                    "LOG_FAILURE",
                    7.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "failure_signature",
                "no usable target-specific log error",
                None,
                0.0,
            )
        if operation == "METRIC_ANOMALIES":
            groups = summary.get("aggregates_by_metric")
            if isinstance(groups, dict):
                anomalies = [
                    item
                    for item in groups.values()
                    if isinstance(item, dict) and item.get("anomaly")
                ]
                if anomalies:
                    return (
                        EvidenceAssessment.SUPPORTS,
                        "resource_pressure",
                        "candidate-relevant metric anomaly observed",
                        "METRIC_ANOMALY",
                        7.0,
                    )
                if groups and summary.get("matching_count", 0):
                    return (
                        EvidenceAssessment.CONTRADICTS,
                        "resource_pressure",
                        "complete candidate metric coverage has no anomaly",
                        "METRIC_ANOMALY",
                        4.0,
                    )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "resource_pressure",
                "metric evidence is unavailable or inconclusive",
                None,
                0.0,
            )
        if operation == "TRACE_ERROR_TREE":
            edges = summary.get("edges")
            if isinstance(edges, list) and any(
                item.get("status") == "ERROR" for item in edges if isinstance(item, dict)
            ):
                return (
                    EvidenceAssessment.SUPPORTS,
                    "dependency",
                    "error-bearing trace edge observed",
                    "TRACE_ERROR_PROPAGATION",
                    7.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "dependency",
                "no usable error-origin trace edge",
                None,
                0.0,
            )
        if operation == "COMPARE_REPLICAS":
            if summary.get("comparison_available") and (
                summary.get("spec_differences") or summary.get("event_differences")
            ):
                return (
                    EvidenceAssessment.SUPPORTS,
                    "comparative",
                    "target differs from comparable peers",
                    "COMPARATIVE_EVIDENCE",
                    8.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "comparative",
                "peers are equivalent or comparison unavailable",
                None,
                0.0,
            )
        if operation == "VERIFY_TEMPORAL_ALIGNMENT":
            relation = summary.get("causal_temporal_assessment")
            if relation == EvidenceAssessment.SUPPORTS:
                return (
                    EvidenceAssessment.SUPPORTS,
                    "temporal",
                    "diagnostic signal is bounded and near incident onset",
                    "TEMPORAL_ALIGNMENT",
                    5.0,
                )
            if relation == EvidenceAssessment.CONTRADICTS:
                return (
                    EvidenceAssessment.CONTRADICTS,
                    "temporal",
                    "signal timing contradicts the causal hypothesis",
                    "TEMPORAL_ALIGNMENT",
                    4.0,
                )
            return (
                EvidenceAssessment.INCONCLUSIVE,
                "temporal",
                "timing is not sufficiently discriminating",
                None,
                0.0,
            )
        return (
            EvidenceAssessment.INCONCLUSIVE,
            "causal_mechanism",
            "context alone does not establish causality",
            None,
            0.0,
        )

    @staticmethod
    def _ranked_for_memory(
        ranking: tuple[RankedCandidate, ...], memory: E9CaseMemory
    ) -> tuple[RankedCandidate, ...]:
        by_canonical = {
            item["canonical"]: item["handle"]
            for item in memory.state["discovered_entities"].values()
        }
        return tuple(
            RankedCandidate(
                item.handle,
                item.canonical,
                item.rank,
                item.score,
                item.family,
                item.retrieval_evidence,
            )
            for item in ranking
            if item.canonical in by_canonical and by_canonical[item.canonical] == item.handle
        )

    @staticmethod
    def _supported_handles(memory: E9CaseMemory) -> tuple[str, ...]:
        return tuple(
            handle
            for handle, item in memory.state.get("candidate_state", {}).items()
            if item.get("status") == "SUPPORTED"
        )

    def _export_agent_output(
        self,
        scenario_id: str,
        terminal: str,
        memory: E9CaseMemory,
        evidence_memory: E11CaseMemory,
        catalog: Any,
    ) -> ITBenchAgentOutput:
        """Export the native conclusion in the official ITBench shape."""
        predictions: list[ITBenchEntityPrediction] = []
        if terminal == "SUBMIT":
            submitted = memory.state.get("submitted_targets", ())
            if not isinstance(submitted, (list, tuple)):
                submitted = ()
            for rank, handle in enumerate(submitted, start=1):
                entity = catalog.by_handle(str(handle))
                if entity is None:
                    raise ValueError("submitted handle is absent from runtime catalog")
                predictions.append(
                    ITBenchEntityPrediction(
                        entity=parse_canonical_entity(entity.canonical),
                        rank=rank,
                        condition="runtime-validated causal support",
                    )
                )
        reasoning = ""
        if terminal == "STOP":
            last = memory.state.get("last_rejection")
            reasoning = (
                str(last.get("reason", "insufficient causal evidence"))
                if isinstance(last, dict)
                else "insufficient causal evidence"
            )
        else:
            reasoning = "submitted runtime-validated causal candidates"
        return ITBenchAgentOutput(
            incident_id=f"{self.execution_id}:{scenario_id}",
            scenario_id=scenario_id,
            contributing_factor=tuple(predictions),
            reasoning=reasoning[:1000],
            native_terminal=terminal,
        )

    @staticmethod
    def _investigation_state(memory: E9CaseMemory) -> dict[str, Any]:
        projection = memory.projection()
        projection["supporting_evidence"] = [
            item for item in projection["evidence"] if item.get("assessment") == "SUPPORTS"
        ]
        projection["contradicting_evidence"] = [
            item for item in projection["evidence"] if item.get("assessment") == "CONTRADICTS"
        ]
        projection["recent_operations"] = projection.get("operations_already_run", [])[-6:]
        # The context contract calls these revisions; E9 memory retains the
        # historical ``ranking_history`` name for compatibility with its
        # persisted projection.
        projection["ranking_revisions"] = projection.get("ranking_history", [])[-8:]
        return projection


def _event_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    record = item.get("record", item)
    if isinstance(record, dict):
        body = record.get("Body")
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                return " ".join(str(parsed.get(key, "")) for key in ("reason", "message", "type"))
        return " ".join(str(record.get(key, "")) for key in ("reason", "message", "type"))
    return ""


def _incident_context(scenario_id: str, incident: Any, alerts: tuple[Any, ...]) -> dict[str, Any]:
    """Serialize only observable incident metadata for the model context."""
    raw = incident.model_dump(mode="json") if hasattr(incident, "model_dump") else {}
    diagnostic = []
    affected: set[str] = set()
    for alert in alerts[:12]:
        item = alert.model_dump(mode="json") if hasattr(alert, "model_dump") else {}
        diagnostic.append(
            {
                "alert_name": item.get("alert_name"),
                "service": item.get("service"),
                "namespace": item.get("namespace"),
                "starts_at": item.get("starts_at"),
                "labels": item.get("labels", {}),
            }
        )
        for key in ("service", "namespace"):
            if item.get(key):
                affected.add(f"{key}:{item[key]}")
    return {
        "scenario_id": scenario_id,
        "incident_id": str(raw.get("incident_id", "")),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "incident_start": raw.get("created_at"),
        "diagnostic_alerts": diagnostic,
        "affected_identities": sorted(affected),
        "observation_window": "snapshot-bounded",
    }


__all__ = ["E11InvestigationRuntime", "E11RuntimeLimits"]
