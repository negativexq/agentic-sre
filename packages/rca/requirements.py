"""Evidence requirement identity and per-revision evaluations (M19-5.5).

A requirement is one evidence obligation a rule is still waiting for:
``STATUS_CONTINUITY`` for one exact Pod instance (A1), ``RESOURCE_COVERAGE``
for one hypothesis's lowered resource series (A2). Its identity is
``requirement_key``: the full SHA-256 of a canonical JSON envelope over the
incident, ``hypothesis_key``, rule identity, kind and targets. Revision,
diagnosis, deadline, status and evidence never enter it, so one obligation
keeps one key across revisions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any
from uuid import UUID

from packages.rca.claims import actor_findings
from packages.rca.episode_end import RULE_ID as EPISODE_END_RULE_ID
from packages.rca.episode_end import RULE_VERSION as EPISODE_END_RULE_VERSION
from packages.rca.episode_end import InstanceRequirement
from packages.rca.model import (
    Hypothesis,
    HypothesisInventoryEntry,
    RequirementEvaluation,
    RequirementKind,
    RequirementTarget,
)
from packages.rca.resource_mechanism import RULE_ID as RESOURCE_RULE_ID
from packages.rca.resource_mechanism import RULE_VERSION as RESOURCE_RULE_VERSION
from packages.rca.resource_mechanism import CoverageRequirement

REQUIREMENT_SCHEMA = "agentic-sre.evidence-requirement.v1"


def requirement_targets_document(evaluation: RequirementEvaluation) -> list[dict[str, str]]:
    """The stored ``targets`` array, in the evaluation's canonical order."""
    return [target.model_dump(mode="json", exclude_none=True) for target in evaluation.targets]


def requirement_key(incident_id: UUID, evaluation: RequirementEvaluation) -> str:
    """Full SHA-256 hex of the canonical requirement identity envelope."""
    if not isinstance(incident_id, UUID):
        raise TypeError("requirement identity needs the incident UUID")
    if not evaluation.hypothesis_key:
        raise ValueError("requirement identity needs a hypothesis_key")
    envelope: dict[str, Any] = {
        "schema": REQUIREMENT_SCHEMA,
        "incident_id": str(incident_id),
        "hypothesis_key": evaluation.hypothesis_key,
        "rule_id": evaluation.rule_id,
        "rule_version": evaluation.rule_version,
        "kind": evaluation.kind.value,
        "targets": requirement_targets_document(evaluation),
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


MANIFESTATION_ONLY = "MANIFESTATION_ONLY"


def _mechanism_class(hypothesis: Hypothesis) -> tuple[str, ...]:
    kinds = sorted({finding.kind.value for finding in hypothesis.initiating_findings})
    return tuple(kinds) or (MANIFESTATION_ONLY,)


def _instance_uids(hypothesis: Hypothesis) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                finding.entity_instance.uid
                for finding in (
                    actor_findings(hypothesis)
                    if hypothesis.claim_version == "m21.v2"
                    else hypothesis.findings
                )
                if finding.entity_instance is not None and finding.entity_instance.uid
            }
        )
    )


def hypothesis_inventory(hypotheses: Sequence[Hypothesis]) -> tuple[HypothesisInventoryEntry, ...]:
    """Every hypothesis of a revision, repeated keys kept, ordered by ID, with its identity."""
    return tuple(
        HypothesisInventoryEntry(
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_key=hypothesis.hypothesis_key or None,
            causal_actor=hypothesis.causal_actor,
            mechanism_class=_mechanism_class(hypothesis),
            instance_uids=_instance_uids(hypothesis),
        )
        for hypothesis in sorted(hypotheses, key=lambda item: item.hypothesis_id)
    )


def build_requirement_evaluations(
    hypotheses: Sequence[Hypothesis],
    *,
    instance_requirements: Mapping[str, Sequence[InstanceRequirement]],
    coverage_requirements: Mapping[str, CoverageRequirement],
) -> tuple[RequirementEvaluation, ...]:
    """Requirement evaluations from the A1/A2 evaluators, deterministically ordered."""
    evaluations: list[RequirementEvaluation] = []
    for hypothesis in sorted(hypotheses, key=lambda item: item.hypothesis_id):
        actor = hypothesis.causal_actor.canonical
        key = hypothesis.hypothesis_key or None
        for instance in sorted(
            instance_requirements.get(hypothesis.hypothesis_id, ()), key=lambda item: item.uid
        ):
            evaluations.append(
                RequirementEvaluation(
                    hypothesis_id=hypothesis.hypothesis_id,
                    hypothesis_key=key,
                    rule_id=EPISODE_END_RULE_ID,
                    rule_version=EPISODE_END_RULE_VERSION,
                    kind=RequirementKind.STATUS_CONTINUITY,
                    targets=(RequirementTarget(entity=actor, uid=instance.uid),),
                    result=instance.result,
                    audit_reason=instance.audit_reason,
                )
            )
        coverage = coverage_requirements.get(hypothesis.hypothesis_id)
        if coverage is not None:
            evaluations.append(
                RequirementEvaluation(
                    hypothesis_id=hypothesis.hypothesis_id,
                    hypothesis_key=key,
                    rule_id=RESOURCE_RULE_ID,
                    rule_version=RESOURCE_RULE_VERSION,
                    kind=RequirementKind.RESOURCE_COVERAGE,
                    targets=tuple(
                        sorted(
                            (
                                RequirementTarget(
                                    entity=actor, container=container, resource=resource
                                )
                                for container, resource in coverage.series
                            ),
                            key=RequirementTarget.sort_key,
                        )
                    ),
                    result=coverage.result,
                    audit_reason=coverage.audit_reason,
                )
            )
    return tuple(evaluations)


__all__ = [
    "REQUIREMENT_SCHEMA",
    "build_requirement_evaluations",
    "hypothesis_inventory",
    "requirement_key",
    "requirement_targets_document",
]
