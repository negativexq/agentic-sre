"""Product run artifacts of the product-resolution harness (M19-6.11).

One ``agentic-sre.product-run.v2`` document per successful scenario run, at
``.local/product-bench/<run>/<scenario>.json``, written once and never
rewritten. It separates three layers:

* observed facts: incident, timeline, revisions, provider tape, code provenance;
* derived claims not evaluated yet: ``proof``/``negatives`` are
  ``NOT_EVALUATED``, ``safety`` counters and ``transition`` are ``null``
  (not measured, not zero). M19-7.P computes them before the single write;
* the same schema, later, with evaluated claims.

An artifact's existence never means a proof passed. Any fact that cannot be
established makes the run ``ERROR`` (``ArtifactError``) instead of a document.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from packages.evals.product.actions import ProductAction
from packages.evals.product.revisions import CHAIN
from packages.evals.product.spec import ProductScenario, ProofId

SCHEMA: Literal["agentic-sre.product-run.v2"] = "agentic-sre.product-run.v2"
PROTOCOL_DOCUMENT = "docs/architecture/m19-product-resolution-protocol.md"
_COMMIT = r"^[0-9a-f]{40}$"
_RUN_ID = re.compile(r"^\d{8}T\d{12}Z$")
_SCENARIO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ArtifactError(RuntimeError):
    """The run's artifact facts could not be established; the run is ``ERROR``."""


class ProofStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonNegative = Annotated[int, Field(ge=0)]


class ScenarioInfo(_Model):
    id: str = Field(pattern=_SCENARIO_ID.pattern)
    tier: Literal["DEV"] | None
    protocol_commit: str = Field(pattern=_COMMIT)


class CodeInfo(_Model):
    commit: str = Field(pattern=_COMMIT)
    engine_version: str = Field(min_length=1)
    config_digest: str = Field(min_length=1)


class AlertActivationInfo(_Model):
    """R1 qualification provenance only (M19-6.10); not RCA evidence."""

    labels: dict[str, str]
    starts_at: datetime
    active_at: datetime


class IncidentInfo(_Model):
    id: str = Field(min_length=1)
    onset: datetime
    alert_activations: list[AlertActivationInfo] | None = None


class TimelineEntry(_Model):
    offset: str = Field(pattern=r"^[+-]\d+m(\d{1,2}s)?$")
    action: str = Field(pattern=r"^[A-Za-z]+:[A-Za-z0-9._-]+$")
    at: datetime
    verify: Literal["PASS"]


class RequirementEntry(_Model):
    requirement_key: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    not_before: datetime


class RevisionEntry(_Model):
    number: int = Field(ge=1)
    trigger: Literal["INITIAL", "MANUAL", "EVIDENCE_DEADLINE"]
    window_end: datetime
    manifest_digest: str = Field(min_length=1)
    tape_digest: str = Field(min_length=1)
    epistemic_digest: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    requirements: list[RequirementEntry]


class ProviderTape(_Model):
    """Reads of the accepted R1 + R_early + R2 runs, summed."""

    reads: NonNegative
    capture_reads: NonNegative
    engine_reads: NonNegative
    investigation_reads: NonNegative
    errors: NonNegative

    @model_validator(mode="after")
    def _classes_add_up(self) -> ProviderTape:
        if self.capture_reads + self.engine_reads + self.investigation_reads != self.reads:
            raise ValueError("caller-class reads do not add up to reads")
        if self.errors > self.reads:
            raise ValueError("more errors than reads")
        return self


class Transition(_Model):
    hypothesis_key: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    decisive_evidence_ids: list[str]


class Safety(_Model):
    """Every counter is present; ``None`` = not measured (never zero by default)."""

    @classmethod
    def not_measured(cls) -> Safety:
        return cls.model_validate(dict.fromkeys(cls.model_fields))

    false_resolved: NonNegative | None
    wrong_actor: NonNegative | None
    uid_misbinding: NonNegative | None
    missing_to_contradiction: NonNegative | None
    fabricated: NonNegative | None
    replay_divergence: NonNegative | None
    unresolved_tape_evidence_id: NonNegative | None
    synthetic_cluster_evidence: NonNegative | None


class Attempt(_Model):
    n: int = Field(ge=1)
    result: Literal["PASS", "ERROR"]
    reason: str


def _proof_kind(proof: ProofId) -> str:
    return proof.value[0]


class ProductRunArtifact(_Model):
    schema_: Literal["agentic-sre.product-run.v2"] = Field(alias="schema")
    scenario: ScenarioInfo
    code: CodeInfo
    incident: IncidentInfo
    timeline: list[TimelineEntry]
    revisions: list[RevisionEntry]
    provider_tape: ProviderTape
    transition: Transition | None
    proof: dict[ProofId, ProofStatus]
    decision_authority: dict[str, str] = Field(default_factory=dict)
    negatives: dict[ProofId, ProofStatus]
    safety: Safety
    attempts: list[Attempt]

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _coherent(self) -> ProductRunArtifact:
        if [(item.number, item.trigger) for item in self.revisions] != list(
            enumerate(CHAIN, start=1)
        ):
            raise ValueError("revisions must be 1 INITIAL, 2 MANUAL, 3 EVIDENCE_DEADLINE")
        if any(_proof_kind(key) != "T" for key in self.proof):
            raise ValueError("proof holds only T* ids")
        if any(_proof_kind(key) != "N" for key in self.negatives):
            raise ValueError("negatives holds only N* ids")
        if not self.attempts or [item.n for item in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("attempts are numbered 1..n")
        if self.attempts[-1].result != "PASS":
            raise ValueError("an artifact is written only for a passing attempt")
        return self

    def document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# --- facts -> fields -------------------------------------------------------------------


def run_id(started_at: datetime) -> str:
    """The UTC run-start stamp ``YYYYMMDDTHHMMSSffffffZ``."""
    if started_at.tzinfo is None:
        raise ValueError("run start must be timezone-aware")
    return started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def format_offset(offset: timedelta) -> str:
    """The requested scenario offset as ``±Nm`` or ``±NmSs``; whole seconds only."""
    if offset % timedelta(seconds=1):
        raise ArtifactError(f"offset {offset} is not a whole number of seconds")
    sign = "-" if offset < timedelta(0) else "+"
    minutes, seconds = divmod(int(abs(offset).total_seconds()), 60)
    return f"{sign}{minutes}m" + (f"{seconds}s" if seconds else "")


def describe_action(action: ProductAction) -> str:
    """A stable descriptor ``Type:service``, never ``repr()``."""
    service = getattr(action, "service", None)
    if not isinstance(service, str) or not service:
        raise ArtifactError(f"{type(action).__name__} has no service to describe")
    return f"{type(action).__name__}:{service}"


def not_evaluated(scenario: ProductScenario) -> tuple[dict[ProofId, ProofStatus], ...]:
    """``proof`` and ``negatives`` from the scenario's own expectation, all NOT_EVALUATED."""
    ids = scenario.expectation.proofs
    return (
        {item: ProofStatus.NOT_EVALUATED for item in ids if _proof_kind(item) == "T"},
        {item: ProofStatus.NOT_EVALUATED for item in ids if _proof_kind(item) == "N"},
    )


def canonical_labels(labels: Mapping[str, str]) -> dict[str, str]:
    return {key: labels[key] for key in sorted(labels)}


def code_info(commit: str, summaries: Sequence[Mapping[str, Any]]) -> CodeInfo:
    """``engine_version``/``config_digest`` shared by every revision, or ``ArtifactError``."""
    pairs = {(item.get("engine_version"), item.get("config_digest")) for item in summaries}
    if len(pairs) != 1:
        raise ArtifactError(
            f"revisions disagree on engine version/config digest: {sorted(map(str, pairs))}"
        )
    ((engine_version, config_digest),) = pairs
    if not engine_version or not config_digest:
        raise ArtifactError("a revision has no engine version or config digest")
    return CodeInfo(commit=commit, engine_version=engine_version, config_digest=config_digest)


def _parse(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    raise ArtifactError(f"{what} is not a timestamp: {value!r}")


def revision_entries(
    summaries: Sequence[Mapping[str, Any]],
    requirements: Mapping[int, Sequence[RequirementEntry]],
) -> list[RevisionEntry]:
    entries = []
    for item in sorted(summaries, key=lambda summary: int(summary["revision_number"])):
        entries.append(
            RevisionEntry(
                number=int(item["revision_number"]),
                trigger=item["trigger"],
                window_end=_parse(item["window_end"], "window_end"),
                manifest_digest=item["manifest_digest"],
                tape_digest=item["tape_digest"],
                epistemic_digest=item["epistemic_digest"],
                resolution=item["resolution"],
                requirements=list(requirements.get(int(item["diagnosis_id"]), ())),
            )
        )
    return entries


class VerifiedAction(Protocol):
    """A runner timeline record: requested offset, verified action, its start."""

    @property
    def offset(self) -> timedelta: ...

    @property
    def action(self) -> ProductAction: ...

    @property
    def started_at(self) -> datetime: ...


class Activation(Protocol):
    @property
    def labels(self) -> Mapping[str, str]: ...

    @property
    def starts_at(self) -> datetime: ...

    @property
    def active_at(self) -> datetime: ...


def build_artifact(
    *,
    scenario: ProductScenario,
    commit: str,
    protocol_commit: str,
    incident_id: str,
    onset: datetime,
    activations: Sequence[Activation],
    timeline: Sequence[VerifiedAction],
    summaries: Sequence[Mapping[str, Any]],
    requirements: Mapping[int, Sequence[RequirementEntry]],
    provider_tape: ProviderTape,
    attempts: Sequence[Attempt] | None = None,
    evaluation: Any = None,
) -> ProductRunArtifact:
    """The v2 document of observed facts, with the proof evaluation when one ran.

    Without an ``evaluation`` the derived claims are NOT_EVALUATED/null. With one
    (``proof.Evaluation``) they are the evaluated PASS/FAIL, counters and transition.
    """
    proof, negatives = not_evaluated(scenario)
    safety = Safety.not_measured()
    transition = None
    if evaluation is not None:
        proof = {key: ProofStatus(value) for key, value in evaluation.proof.items()}
        negatives = {key: ProofStatus(value) for key, value in evaluation.negatives.items()}
        safety = Safety.model_validate(dict(evaluation.safety))
        if evaluation.transition is not None:
            item = evaluation.transition
            transition = Transition(
                hypothesis_key=item.hypothesis_key,
                rule_id=item.rule_id,
                rule_version=item.rule_version,
                decisive_evidence_ids=list(item.decisive_evidence_ids),
            )
    try:
        return ProductRunArtifact(
            schema=SCHEMA,
            scenario=ScenarioInfo(
                id=scenario.scenario_id, tier=scenario.tier, protocol_commit=protocol_commit
            ),
            code=code_info(commit, summaries),
            incident=IncidentInfo(
                id=incident_id,
                onset=onset,
                alert_activations=[
                    AlertActivationInfo(
                        labels=canonical_labels(item.labels),
                        starts_at=item.starts_at,
                        active_at=item.active_at,
                    )
                    for item in activations
                ],
            ),
            timeline=[
                TimelineEntry(
                    offset=format_offset(item.offset),
                    action=describe_action(item.action),
                    at=item.started_at,
                    verify="PASS",
                )
                for item in timeline
            ],
            revisions=revision_entries(summaries, requirements),
            provider_tape=provider_tape,
            transition=transition,
            proof=proof,
            decision_authority=getattr(evaluation, "decision_authority", {}),
            negatives=negatives,
            safety=safety,
            attempts=list(attempts)
            if attempts is not None
            else [Attempt(n=1, result="PASS", reason="")],
        )
    except ValidationError as error:
        raise ArtifactError(f"artifact facts do not validate: {error}") from error


# --- code provenance -------------------------------------------------------------------


class GitProvenance(Protocol):
    def head(self) -> str: ...

    def last_commit(self, path: str) -> str: ...

    def tracked_changes(self) -> bool: ...


class LocalGit:
    """Read-only git queries on the harness checkout."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _git(self, *args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(self._root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def last_commit(self, path: str) -> str:
        return self._git("rev-list", "-1", "HEAD", "--", path)

    def tracked_changes(self) -> bool:
        return bool(self._git("status", "--porcelain", "--untracked-files=no"))


def provenance(git: GitProvenance) -> tuple[str, str]:
    """``(code commit, protocol commit)``; a dirty tracked tree is ``ArtifactError``."""
    if git.tracked_changes():
        raise ArtifactError("the tracked tree is dirty: the run's code is not a commit")
    commit, protocol = git.head(), git.last_commit(PROTOCOL_DOCUMENT)
    if not protocol:
        raise ArtifactError(f"{PROTOCOL_DOCUMENT} has no commit")
    return commit, protocol


# --- writer ------------------------------------------------------------------------------


def artifact_path(bench_root: Path, run: str, scenario_id: str) -> Path:
    if not _RUN_ID.match(run):
        raise ArtifactError(f"run id {run!r} is not YYYYMMDDTHHMMSSffffffZ")
    if not _SCENARIO_ID.match(scenario_id):
        raise ArtifactError(f"scenario id {scenario_id!r} is not a safe file name")
    return bench_root / run / f"{scenario_id}.json"


def write_artifact(bench_root: Path, run: str, artifact: ProductRunArtifact) -> Path:
    """Validate, create the file exclusively (never overwrite), read back and validate."""
    path = artifact_path(bench_root, run, artifact.scenario.id)
    try:
        validated = ProductRunArtifact.model_validate_json(
            json.dumps(artifact.document(), sort_keys=True)
        )
    except ValidationError as error:
        raise ArtifactError(f"artifact does not validate: {error}") from error
    text = json.dumps(validated.document(), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as error:
        raise ArtifactError(f"{path} already exists; artifacts are never rewritten") from error
    if load_artifact(path) != validated:
        raise ArtifactError(f"{path} does not read back as written")
    return path


def load_artifact(path: Path) -> ProductRunArtifact:
    try:
        return ProductRunArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise ArtifactError(f"{path} is not a valid {SCHEMA} artifact: {error}") from error


__all__ = [
    "PROTOCOL_DOCUMENT",
    "SCHEMA",
    "AlertActivationInfo",
    "ArtifactError",
    "Attempt",
    "CodeInfo",
    "GitProvenance",
    "IncidentInfo",
    "LocalGit",
    "ProductRunArtifact",
    "ProofStatus",
    "ProviderTape",
    "RequirementEntry",
    "RevisionEntry",
    "Safety",
    "ScenarioInfo",
    "TimelineEntry",
    "Transition",
    "artifact_path",
    "build_artifact",
    "canonical_labels",
    "code_info",
    "describe_action",
    "format_offset",
    "load_artifact",
    "not_evaluated",
    "provenance",
    "revision_entries",
    "run_id",
    "write_artifact",
]
