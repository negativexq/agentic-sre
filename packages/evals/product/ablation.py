"""T5/T6 replay ablations (M19-7.P2): evaluation-only, production behavior unchanged.

Both start from the complete R2 inputs of a run — an observation source whose
provider reads come from its recorded tape — and rerun only the resolver:
``build_case`` then ``diagnose_case``. No planner runs and no provider is called.

* T5 evidence ablation: the records whose evidence ids are in ``D`` are hidden
  from the source. A Finding of the complete case that cites both ``D`` and
  non-``D`` evidence cannot be filtered safely: ``ABLATION_AMBIGUOUS`` (a FAIL).
* T6 rule ablation: the target rule's outcomes are removed from the built case
  (A1: its ended episodes; A2: its mechanism mismatches) together with the
  rule's precondition and requirement records. The override lives only here,
  described by ``EvalRuleOverrides`` and identified by its own
  ``eval_config_digest``; the product's ``EngineConfig``, API, environment and
  ``code.config_digest`` never see it.

For m21.v2/v3, T5/T6 test loss of unique possible-cause support, not loss
of verified mechanism or incident recovery. Legacy records retain RESOLVED scope.
Either ablated diagnosis must lose its versioned target support; T5 additionally requires that
``H_x`` is no longer eliminated by the target rule.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from packages.rca.engine import Case, EngineConfig, build_case, diagnose_case
from packages.rca.episode_end import RULE_ID as EPISODE_END_RULE_ID
from packages.rca.investigation.state import rca_config_digest
from packages.rca.model import (
    ClusterEvent,
    Diagnosis,
    EntityRef,
    LogRecord,
    ObjectVersion,
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.resolution import has_supported_cause
from packages.rca.resource_mechanism import RULE_ID as RESOURCE_RULE_ID
from packages.rca.root_cause_eligibility import RootCauseEligibilities

PASS, FAIL, ABLATION_AMBIGUOUS = "PASS", "FAIL", "ABLATION_AMBIGUOUS"
ABLATABLE_RULES = frozenset({EPISODE_END_RULE_ID, RESOURCE_RULE_ID})


@dataclass(frozen=True, slots=True)
class EvalRuleOverrides:
    """Evaluation-only rule overrides of one ablation run; never a product setting."""

    disabled_rules: tuple[tuple[str, str], ...]  # (rule_id, rule_version)

    def __post_init__(self) -> None:
        unknown = {rule_id for rule_id, _ in self.disabled_rules} - ABLATABLE_RULES
        if unknown:
            raise ValueError(f"no eval-only ablation for rule(s) {sorted(unknown)}")


def eval_config_digest(config: EngineConfig, overrides: EvalRuleOverrides) -> str:
    """Deterministic identity of the ablation run: base replay config + disabled rules."""
    material = {
        "base_config_digest": rca_config_digest(config, None),
        "disabled_rules": sorted([list(rule) for rule in overrides.disabled_rules]),
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AblationResult:
    verdict: str  # PASS | FAIL | ABLATION_AMBIGUOUS
    diagnosis: Diagnosis | None
    reason: str = ""
    eval_config_digest: str | None = None

    @property
    def claim_version(self) -> str:
        trace = self.diagnosis.resolution_trace if self.diagnosis else None
        return (
            "m21.unique-possible-cause.v1"
            if trace and trace.semantics_version in {"m21.v2", "m21.v3"}
            else "legacy.resolved.v1"
        )


class _WithoutEvidence:
    """The source with every record whose evidence id is in ``hidden`` removed."""

    def __init__(self, source: Any, hidden: frozenset[str]) -> None:
        self._source = source
        self._hidden = hidden

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def _keep(self, records: Sequence[Any]) -> list[Any]:
        return [item for item in records if item.evidence_id not in self._hidden]

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        kept = {
            entity: self._keep(versions)
            for entity, versions in self._source.object_history().items()
        }
        return {entity: versions for entity, versions in kept.items() if versions}

    def events(self) -> Sequence[ClusterEvent]:
        return self._keep(self._source.events())

    def error_logs(self) -> Sequence[LogRecord]:
        return self._keep(self._source.error_logs())

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        return self._keep(self._source.traffic_observations())

    def trace_observations(self) -> Sequence[TraceSpanObservation]:
        return self._keep(self._source.trace_observations())

    def pod_status_observations(self) -> Sequence[Any]:
        return self._keep(self._source.pod_status_observations())

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure] | ProviderReadFailure:
        result = self._source.resource_pressure(pods, since)
        if isinstance(result, ProviderReadFailure):
            return result
        return self._keep(result)


def _mixed_findings(case: Case, decisive: frozenset[str]) -> list[str]:
    mixed = []
    for finding in case.findings:
        cited = set(finding.evidence_ids)
        if cited & decisive and not cited <= decisive:
            mixed.append(f"{finding.kind.value}:{finding.entity.canonical}")
    return mixed


def _eliminated_by(diagnosis: Diagnosis, hypothesis_key: str, rule: tuple[str, str]) -> bool:
    trace = diagnosis.resolution_trace
    if trace is None:
        return False
    ids = {
        item.hypothesis_id
        for item in diagnosis.hypothesis_inventory
        if item.hypothesis_key == hypothesis_key
    }
    return any(
        item.hypothesis_id in ids and (item.rule_id, item.rule_version) == rule
        for item in trace.eliminations
    )


def evidence_ablation(
    source: Any,
    decisive: Sequence[str],
    *,
    hypothesis_key: str,
    rule: tuple[str, str],
    config: EngineConfig | None = None,
) -> AblationResult:
    """T5: rerun the resolver without the records in ``D``."""
    effective = config or EngineConfig()
    hidden = frozenset(decisive)
    if not hidden:
        return AblationResult(FAIL, None, "D is empty")
    mixed = _mixed_findings(build_case(source, effective), hidden)
    if mixed:
        return AblationResult(ABLATION_AMBIGUOUS, None, f"mixed provenance: {mixed}")
    ablated = diagnose_case(
        build_case(_WithoutEvidence(source, hidden), effective), config=effective
    )
    if _eliminated_by(ablated, hypothesis_key, rule):
        return AblationResult(FAIL, ablated, "H_x is still eliminated by the target rule")
    if has_supported_cause(ablated):
        return AblationResult(
            FAIL,
            ablated,
            "the ablated diagnosis still has a supported cause (legacy RESOLVED or scoped support)",
        )
    return AblationResult(PASS, ablated)


def without_rule(case: Case, rule_id: str) -> Case:
    """The built case with every outcome of ``rule_id`` removed (eval-only)."""
    if rule_id not in ABLATABLE_RULES:
        raise ValueError(f"no eval-only ablation for rule {rule_id}")
    eligibilities = case.root_cause_eligibilities
    mismatches = case.mechanism_mismatches
    if rule_id == EPISODE_END_RULE_ID:
        eligibilities = RootCauseEligibilities(eligibilities.assessments, {})
    else:
        mismatches = {}
    return replace(
        case,
        root_cause_eligibilities=eligibilities,
        mechanism_mismatches=mismatches,
        rule_preconditions={k: v for k, v in case.rule_preconditions.items() if k[1] != rule_id},
        precondition_reasons={
            k: v for k, v in case.precondition_reasons.items() if k[1] != rule_id
        },
        requirement_evaluations=tuple(
            item for item in case.requirement_evaluations if item.rule_id != rule_id
        ),
    )


def rule_ablation(
    source: Any,
    rule: tuple[str, str],
    *,
    config: EngineConfig | None = None,
) -> AblationResult:
    """T6: rerun the resolver with only the target rule disabled."""
    effective = config or EngineConfig()
    overrides = EvalRuleOverrides(disabled_rules=(rule,))
    digest = eval_config_digest(effective, overrides)
    ablated = diagnose_case(without_rule(build_case(source, effective), rule[0]), config=effective)
    if has_supported_cause(ablated):
        return AblationResult(
            FAIL,
            ablated,
            "the ablated diagnosis still has a supported cause (legacy RESOLVED or scoped support)",
            digest,
        )
    return AblationResult(PASS, ablated, eval_config_digest=digest)


__all__ = [
    "ABLATABLE_RULES",
    "ABLATION_AMBIGUOUS",
    "FAIL",
    "PASS",
    "AblationResult",
    "EvalRuleOverrides",
    "eval_config_digest",
    "evidence_ablation",
    "rule_ablation",
    "without_rule",
]
