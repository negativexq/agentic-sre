"""DEV guard experiment: don't treat shared aggregate IDs as mechanism accounting.

No production rule registration. Same-operation corroboration plus an existing
strong witness can account for current refusals; old/unknown facts survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.evals.frozen_request_binding import (
    FrozenLog,
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.rca.causal_closure import FAILED_ROLLOUT_RULE
from packages.rca.claims import actor_findings
from packages.rca.engine import Case
from packages.rca.model import Diagnosis, FindingKind, RootSupportStatus
from packages.rca.resolution import _execution_interval
from packages.rca.signals import dependency_findings


@dataclass(frozen=True)
class ClaimAccounting:
    claim_id: str
    eligible_for_shared_explanation: bool
    accounted_log_ids: tuple[str, ...]
    unaccounted_log_ids: tuple[str, ...]
    deficits: tuple[str, ...]
    explaining_claim_id: str | None
    authority: str = "DEVELOPMENT_COUNTERFACTUAL_ONLY"


def assess_rollout_refusal_accounting(
    case: Case, diagnosis: Diagnosis
) -> tuple[ClaimAccounting, ...]:
    trace = diagnosis.resolution_trace
    strong = set(trace.mechanism_verified_hypotheses) if trace else set()
    supported = set(trace.plausible_hypotheses) if trace else set()
    holder = next((h for h in case.hypotheses if h.hypothesis_id in strong), None)
    unique = len(strong) == 1 and strong == supported and holder is not None
    history = case.source.object_history()
    logs = tuple(case.source.error_logs())
    spans = tuple(case.source.trace_observations())
    cutoff = case.source.observation_cutoff()
    witnesses = [
        w
        for audit in (trace.hypothesis_audits if trace else ())
        if audit.hypothesis_id in strong
        for support in audit.root_support
        if support.rule_id == FAILED_ROLLOUT_RULE and support.status is RootSupportStatus.FIRED
        for w in support.witnesses
    ]
    results = []
    for target in case.hypotheses:
        local = actor_findings(target)
        if target.causal_actor.kind != "Service" or not any(
            f.kind is FindingKind.DEPENDENCY_ERRORS for f in local
        ):
            continue
        deficits: list[str] = []
        accounted: set[str] = set()
        unaccounted: set[str] = set()
        if not unique:
            deficits.append("NO_UNIQUE_STRONG_SUPPORTED_SOURCE")
        if target.hypothesis_id in supported:
            deficits.append("TARGET_IS_SUPPORTED_INDEPENDENT_CLAIM")
        if target.mechanism != "MANIFESTATION_ONLY" or any(
            f.kind is not FindingKind.DEPENDENCY_ERRORS for f in local
        ):
            deficits.append("OTHER_LOCAL_FACTS_OR_INITIATING_MECHANISM")
        for finding in local:
            if finding.kind is not FindingKind.DEPENDENCY_ERRORS:
                continue
            caller_id = finding.details.get("caller")
            caller = next((e for e in case.topology.latest if e.canonical == caller_id), None)
            if caller is None or target.causal_actor not in case.topology.outgoing(caller, "calls"):
                deficits.append("DECLARED_CALLER_SERVICE_RELATION_NOT_RECORDED")
                unaccounted.update(finding.evidence_ids)
                continue
            # Reconstruct exactly the producer's group, including logs beyond its first-four citations.
            group = [
                log
                for log in logs
                if any(
                    f.entity == target.causal_actor
                    for f in dependency_findings((log,), case.topology, {caller})
                )
            ]
            if (
                len(group) != finding.details.get("errors")
                or not set(finding.evidence_ids) <= {record.evidence_id for record in group}
                or len({record.evidence_id for record in group}) != len(group)
            ):
                deficits.append("AGGREGATE_LOG_INVENTORY_NOT_ACCOUNTED")
                unaccounted.update(finding.evidence_ids)
                continue
            for log in group:
                matched = False
                for witness in witnesses:
                    pod = next(
                        (hop.target for hop in witness.path if hop.relation == "rolls_out"), None
                    )
                    interval = _execution_interval(witness, history, cutoff)
                    if (
                        holder is None
                        or witness.actor_instance != holder.actor_instance
                        or witness.onset != target.episode_onset
                        or interval is None
                        or cutoff is None
                        or pod is None
                    ):
                        continue
                    if (
                        pod not in case.topology.outgoing(target.causal_actor, "selects")
                        or case.topology.workload_of(pod) != holder.causal_actor
                    ):
                        continue
                    if (
                        caller.namespace != witness.symptom.namespace
                        or log.service != witness.symptom.name
                        or witness.symptom.namespace != target.causal_actor.namespace
                    ):
                        continue
                    if (
                        log.attributes.get("error.type") != "ConnectionRefusedError"
                        or log.attributes.get("server.address") != target.causal_actor.name
                    ):
                        continue
                    client = [
                        s
                        for s in spans
                        if s.trace_id == log.attributes.get("trace_id")
                        and s.span_id == log.attributes.get("span_id")
                    ]
                    if len(client) != 1 or client[0].evidence_id not in witness.evidence_ids:
                        continue
                    provenance: dict[str, Any] = {
                        "incident_id": case.incident_id,
                        "run_id": "frozen-dev-scope",
                        "captured_at": cutoff,
                    }
                    evidence = FrozenRequestEvidence(
                        incident_id=case.incident_id,
                        run_id="frozen-dev-scope",
                        episode_start=interval[0],
                        episode_end=interval[1],
                        frozen_cutoff=cutoff,
                        caller_service=log.service,
                        configured_peer=target.causal_actor.name,
                        logs=(
                            FrozenLog(
                                **provenance,
                                source_reference=f"frozen-log:{log.evidence_id}",
                                record=log,
                            ),
                        ),
                        spans=tuple(
                            FrozenSpan(
                                **provenance,
                                source_reference=f"frozen-span:{s.evidence_id}",
                                record=s,
                            )
                            for s in spans
                        ),
                    )
                    relation = corroborate_frozen_requests(evidence).relations[0]
                    if relation.request_relation == "CORROBORATED":
                        matched = True
                        break
                (accounted if matched else unaccounted).add(log.evidence_id)
        if unaccounted:
            deficits.append("LOG_FACTS_NOT_BOUND_TO_OBSERVED_ROLLOUT_OPERATIONS")
        if not accounted:
            deficits.append("NO_ACCOUNTED_CURRENT_REFUSAL")
        results.append(
            ClaimAccounting(
                target.hypothesis_id,
                not deficits and not unaccounted and bool(accounted),
                tuple(sorted(accounted)),
                tuple(sorted(unaccounted)),
                tuple(sorted(set(deficits))),
                holder.hypothesis_id if unique and holder is not None else None,
            )
        )
    return tuple(results)
