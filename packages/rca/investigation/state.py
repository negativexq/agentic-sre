"""Plain state and policy contracts used by the LangGraph wiring."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from packages.rca.engine import Case, EngineConfig
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    Finding,
    Hypothesis,
    InformationGap,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationActionStatus,
    InvestigationCandidateSelectionAudit,
    InvestigationLedgerEntry,
    InvestigationObservation,
    InvestigationPolicyKind,
    InvestigationResult,
    InvestigationStep,
    InvestigationStopReason,
    Resolution,
    StructuralAlternative,
)
from packages.rca.source import ObservationSource

if TYPE_CHECKING:
    from packages.rca.investigation.intents import IntentMenuItem


@dataclass(frozen=True)
class InvestigationConfig:
    """Operational limits for one investigation run."""

    max_turns: int = 6
    max_model_calls: int = 6
    max_tool_calls: int = 8
    max_tool_calls_per_gap: int = 2
    max_invalid_actions: int = 2
    max_no_progress_rounds: int = 2
    max_wall_time_seconds: float = 120.0
    engine: EngineConfig | None = None

    def __post_init__(self) -> None:
        if (
            min(
                self.max_turns,
                self.max_model_calls,
                self.max_tool_calls,
                self.max_tool_calls_per_gap,
            )
            < 1
        ):
            raise ValueError("investigation budgets must be positive")
        if self.max_invalid_actions < 0 or self.max_no_progress_rounds < 0:
            raise ValueError("investigation retry budgets cannot be negative")
        if self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive")


@dataclass(frozen=True)
class InvestigationPolicyContext:
    """Small bounded context presented to an action policy."""

    incident_id: str
    diagnosis: Diagnosis
    hypotheses: tuple[Hypothesis, ...]
    gaps: tuple[InformationGap, ...]
    attempted_actions: tuple[str, ...]
    turns: int
    model_calls_remaining: int
    tool_calls_remaining: int
    previous_investigations: tuple[InvestigationLedgerEntry, ...] = ()
    last_rejection: tuple[str, str, str] | None = None
    structural_alternatives: tuple[StructuralAlternative, ...] = ()
    candidate_actions: tuple[InvestigationAction, ...] = ()


class InvestigationPolicy(Protocol):
    """Policy that may request one already-authorized observation."""

    counts_as_model: bool

    def choose_action(self, context: InvestigationPolicyContext) -> InvestigationAction: ...


class InvestigationTool(Protocol):
    """Read-only semantic capability used by the graph."""

    name: str

    def execute(
        self,
        case: Case,
        gap: InformationGap,
        target: EntityRef,
    ) -> InvestigationObservation: ...


CaseRebuilder = Callable[[ObservationSource, tuple[Finding, ...]], Case]


class InvestigationState(TypedDict, total=False):
    """Checkpointable bounded graph state: data only.

    Every value must serialize without pickle. Live dependencies (the
    observation source, the policy and its model client, tools, the case
    rebuilder, configuration) are bound to the graph's nodes when it is built;
    the current case is rebuilt from the bounded source plus acquired evidence
    references and legacy ``investigation_findings``.
    """

    incident_id: str
    started_at: datetime
    initial_diagnosis: Diagnosis
    current_diagnosis: Diagnosis
    observations: tuple[InvestigationObservation, ...]
    ledger: tuple[InvestigationLedgerEntry, ...]
    action_audits: tuple[InvestigationActionAudit, ...]
    investigation_findings: tuple[Finding, ...]
    acquired_evidence_refs: tuple[str, ...]
    attempted_actions: tuple[str, ...]
    attempted_observations: tuple[str, ...]
    successful_exploration_observations: tuple[str, ...]
    exploration_covered_atoms: tuple[tuple[str, str], ...]
    last_exploration_progress: bool
    intent_history: tuple[dict[str, object], ...]
    pending_intent_id: str | None
    pending_intent_kind: str | None
    pending_selection_candidates: tuple[InvestigationCandidateSelectionAudit, ...]
    pending_selection_strategy: str | None
    pending_selection_reason: str | None
    pending_baseline_candidate_id: str | None
    pending_active_candidate_id: str | None
    attempted_gap_ids: tuple[str, ...]
    pending_action: InvestigationAction | None
    pending_observation: InvestigationObservation | None
    pending_findings: tuple[Finding, ...]
    pending_returned_evidence_refs: tuple[str, ...]
    pending_new_evidence_refs: tuple[str, ...]
    pending_already_known_refs: tuple[str, ...]
    previous_resolution: Resolution
    previous_gap_fingerprint: tuple[tuple[str, ...], ...]
    previous_evidence_fingerprint: tuple[str, ...]
    previous_hypothesis_fingerprint: tuple[str, ...]
    previous_world_model_fingerprint: str
    frontier_queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...]
    action_validation_status: InvestigationActionStatus | None
    turns: int
    model_calls: int
    tool_calls: int
    invalid_actions: int
    rejected_actions: int
    no_progress_count: int
    last_new_evidence_count: int
    last_new_raw_evidence_count: int
    stop_reason: InvestigationStopReason | None
    trace_steps: tuple[InvestigationStep, ...]
    final_result: InvestigationResult | None
    last_rejection: tuple[str, str, str] | None


def policy_kind(policy: object) -> InvestigationPolicyKind:
    """A policy's control-flow family; a policy that declares none chooses actions."""
    return InvestigationPolicyKind(getattr(policy, "semantic_kind", InvestigationPolicyKind.ACTION))


class IntentTiebreakPolicy(Protocol):
    """An ``INTENT_TIEBREAK`` policy: picks one intent id from an equally relevant menu."""

    def choose_intent(
        self,
        menu: Sequence[IntentMenuItem],
        history: Sequence[Mapping[str, object]] = (),
    ) -> str: ...


def investigation_config_document(config: InvestigationConfig) -> dict[str, Any]:
    """The effective config as JSON: every dataclass field, the engine config resolved."""
    effective = replace(config, engine=config.engine or EngineConfig())
    encoded = _encode_config(effective)
    assert isinstance(encoded, dict)
    return encoded


RCA_CONFIG_SCHEMA = "agentic-sre.rca-config.v1"


def rca_config_digest(engine: EngineConfig, investigation: InvestigationConfig | None) -> str:
    """SHA-256 of a run's effective configuration envelope (M19-4.2).

    ``investigation`` is the effective bounded-investigation config, or ``None``
    when no bounded investigation ran. Default values are part of the digest.
    """
    envelope = {
        "schema": RCA_CONFIG_SCHEMA,
        "engine": _encode_config(engine),
        "investigation": (
            investigation_config_document(investigation) if investigation is not None else None
        ),
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def investigation_config_from_document(document: Mapping[str, Any]) -> InvestigationConfig:
    """Rebuild the exact config ``investigation_config_document`` wrote."""
    decoded = _decode_config(InvestigationConfig, document)
    assert isinstance(decoded, InvestigationConfig)
    return decoded


def _encode_config(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _encode_config(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tuple):
        return [_encode_config(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported investigation config value: {type(value).__name__}")


def _decode_config(kind: Any, value: Any) -> Any:
    origin = get_origin(kind)
    if origin in (Union, UnionType):
        options = [item for item in get_args(kind) if item is not type(None)]
        if value is None:
            return None
        if len(options) != 1:
            raise TypeError(f"ambiguous investigation config type: {kind}")
        return _decode_config(options[0], value)
    if origin is tuple:
        (item_kind, _ellipsis) = get_args(kind)
        return tuple(_decode_config(item_kind, item) for item in value)
    if isinstance(kind, type) and is_dataclass(kind):
        if not isinstance(value, Mapping):
            raise TypeError(f"{kind.__name__} config must be an object")
        hints = get_type_hints(kind)
        names = {item.name for item in fields(kind)}
        if set(value) != names:
            raise ValueError(f"{kind.__name__} config fields differ: {sorted(set(value) ^ names)}")
        return kind(**{name: _decode_config(hints[name], value[name]) for name in names})
    if kind is timedelta:
        return timedelta(seconds=value)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if kind in (bool, int, float, str) and isinstance(value, kind):
        return value
    raise TypeError(f"investigation config value {value!r} is not a {kind}")


__all__ = [
    "CaseRebuilder",
    "InvestigationConfig",
    "IntentTiebreakPolicy",
    "investigation_config_document",
    "policy_kind",
    "RCA_CONFIG_SCHEMA",
    "rca_config_digest",
    "investigation_config_from_document",
    "InvestigationPolicy",
    "InvestigationPolicyContext",
    "InvestigationState",
    "InvestigationTool",
]
