"""Testbed ground truth (docs/architecture/testbed-ground-truth-contract.md, ``testbed.v1``).

The injector and an independent oracle write these documents; the engine never reads them. A run
whose truth is incomplete, out of order or measured on skewed clocks is ``INVALID`` and is not
scored. Everything here is written once and never edited.
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "testbed.v1"
TIMELINE_VERSION = "testbed.timeline.v1"
MAX_CLOCK_OFFSET_SECONDS = 1.0

FAMILIES = (
    "direct-pod-fault",
    "dependency-fault",
    "scheduled-recurring",
    "config-or-rollout",
    "negative-control",
    "competing-causes",
)
Family = Literal[
    "direct-pod-fault",
    "dependency-fault",
    "scheduled-recurring",
    "config-or-rollout",
    "negative-control",
    "competing-causes",
]


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Source(StrEnum):
    INJECTOR = "injector"
    ORACLE = "oracle"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class Stamp(Model):
    at: datetime
    source: Source

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)


# Who is allowed to have produced each field (contract §4).
FIELD_SOURCES: dict[str, Source] = {
    "cause_created_at": Source.INJECTOR,
    "execution_started_at": Source.INJECTOR,
    "target_effect_at": Source.ORACLE,
    "propagation_started_at": Source.ORACLE,
    "symptom_started_at": Source.ORACLE,
    "alert_fired_at": Source.INJECTOR,
    "recovery_at": Source.ORACLE,
}
ORDER = tuple(FIELD_SOURCES)  # the ordering of contract §4.1
NEGATIVE_CONTROL_NULLABLE = frozenset(
    {"target_effect_at", "propagation_started_at", "symptom_started_at"}
)
# A fault on the service that shows the symptom has nothing downstream to propagate to (contract §4.2).
NULLABLE_BY_FAMILY: dict[str, frozenset[str]] = {
    "negative-control": NEGATIVE_CONTROL_NULLABLE,
    "direct-pod-fault": frozenset({"propagation_started_at"}),
}


class Timeline(Model):
    version: Literal["testbed.timeline.v1"] = "testbed.timeline.v1"
    cause_created_at: Stamp | None = None
    execution_started_at: Stamp | None = None
    target_effect_at: Stamp | None = None
    propagation_started_at: Stamp | None = None
    symptom_started_at: Stamp | None = None
    alert_fired_at: Stamp | None = None
    recovery_at: Stamp | None = None

    def stamp(self, name: str) -> Stamp | None:
        return getattr(self, name)  # type: ignore[no-any-return]


def timeline_problems(timeline: Timeline, family: str, clock_offset_seconds: float) -> list[str]:
    """Why a run's timeline is ``INVALID`` (contract §4.1 to §4.3); empty when it is valid."""
    problems: list[str] = []
    nullable = NULLABLE_BY_FAMILY.get(family, frozenset())
    for name in ORDER:
        stamp = timeline.stamp(name)
        if stamp is None:
            if name not in nullable:
                problems.append(f"missing {name}")
        elif stamp.source is not FIELD_SOURCES[name]:
            problems.append(f"{name} must come from the {FIELD_SOURCES[name].value}")
    present = [(n, s.at) for n in ORDER if (s := timeline.stamp(n)) is not None]
    for (left, left_at), (right, right_at) in zip(present, present[1:], strict=False):
        if right_at < left_at:
            problems.append(f"{right} precedes {left}")
    if abs(clock_offset_seconds) > MAX_CLOCK_OFFSET_SECONDS:
        problems.append(f"clock offset {clock_offset_seconds:.3f}s exceeds the 1 s bound")
    return problems


Role = Literal["cause", "execution", "target_effect", "propagation", "symptom"]
EvidenceClass = Literal["execution", "effect", "propagation"]


class Link(Model):
    role: Role
    actor: str = Field(min_length=1)
    instance_uid: str | None = None
    knowable: bool
    mechanism: str = Field(min_length=1)
    evidence_class: EvidenceClass | None = None

    @model_validator(mode="after")
    def _uid_matches_knowability(self) -> Link:
        if self.knowable and not self.instance_uid:
            raise ValueError("a knowable link must record its instance_uid")
        if not self.knowable and self.instance_uid is not None:
            raise ValueError("an unknowable link must not carry an instance_uid")
        return self


class Chain(Model):
    version: Literal["testbed.chain.v1"] = "testbed.chain.v1"
    links: tuple[Link, ...] = ()
    # A negative control states why no path exists (checked, not asserted: contract §2.5).
    construction: str | None = None

    def actors(self) -> frozenset[str]:
        return frozenset(link.actor for link in self.links)

    def of_role(self, role: str) -> tuple[Link, ...]:
        return tuple(link for link in self.links if link.role == role)


def chain_problems(chain: Chain, family: str) -> list[str]:
    problems: list[str] = []
    if family == "negative-control" and not chain.construction:
        problems.append("a negative control must state its construction")
    if family == "competing-causes" and len(chain.of_role("cause")) < 2:
        problems.append("competing causes need at least two cause links")
    if family not in {"negative-control"} and not chain.of_role("cause"):
        problems.append("the chain names no cause")
    return problems


class ParameterRange(Model):
    low: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> ParameterRange:
        if self.high < self.low:
            raise ValueError("a parameter range must have low <= high")
        return self


class ScenarioSpec(Model):
    scenario_id: str = Field(min_length=1)
    family: Family
    tier: Literal["DEV", "HOLDOUT"]
    repeats: int = Field(ge=1)
    seeds: tuple[int, ...]
    parameters: dict[str, ParameterRange] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_seed_per_repeat(self) -> ScenarioSpec:
        if len(self.seeds) != self.repeats or len(set(self.seeds)) != self.repeats:
            raise ValueError("a scenario needs one distinct seed per repeat")
        return self


def assign_tier(salt: str, scenario_id: str, holdout_fraction: float = 0.3) -> str:
    """A deterministic split, decided by the salt fixed in the manifest before any run."""
    digest = hashlib.sha256(f"{salt}|{scenario_id}".encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return "HOLDOUT" if fraction < holdout_fraction else "DEV"


def _canonical(document: Any) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class SuiteManifest(Model):
    version: Literal["testbed.suite.v1"] = "testbed.suite.v1"
    suite_id: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    contract_version: str = CONTRACT_VERSION
    created_at: datetime
    salt: str = Field(min_length=1)
    scenarios: tuple[ScenarioSpec, ...]
    # Metric thresholds, fixed before the first run (contract §3).
    acceptance: dict[str, float]
    sha256: str = ""

    @field_validator("created_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _unique_scenarios(self) -> SuiteManifest:
        ids = [s.scenario_id for s in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario ids must be unique")
        return self

    def digest(self) -> str:
        body = self.model_dump(mode="json", exclude={"sha256"})
        return hashlib.sha256(_canonical(body)).hexdigest()

    def frozen(self) -> SuiteManifest:
        return self.model_copy(update={"sha256": self.digest()})

    def verified(self) -> bool:
        return bool(self.sha256) and self.sha256 == self.digest()

    def spec(self, scenario_id: str) -> ScenarioSpec:
        for spec in self.scenarios:
            if spec.scenario_id == scenario_id:
                return spec
        raise KeyError(scenario_id)


class RunRecord(Model):
    version: Literal["testbed.run.v1"] = "testbed.run.v1"
    suite_id: str
    scenario_id: str
    repeat: int = Field(ge=0)
    seed: int
    manifest_sha256: str = Field(min_length=64, max_length=64)
    engine_version: str
    family: Family
    clock_offset_seconds: float
    timeline: Timeline
    chain: Chain
    diagnosis_completed_at: datetime | None = None
    invalid_reasons: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.invalid_reasons


def assemble_run(
    manifest: SuiteManifest,
    scenario_id: str,
    repeat: int,
    *,
    timeline: Timeline,
    chain: Chain,
    clock_offset_seconds: float,
    diagnosis_completed_at: datetime | None = None,
) -> RunRecord:
    """Build a run record; it is refused for a manifest that was not frozen and unchanged."""
    if not manifest.verified():
        raise ValueError("the suite manifest is not frozen or has been changed")
    spec = manifest.spec(scenario_id)
    if not 0 <= repeat < spec.repeats:
        raise ValueError(f"repeat {repeat} is outside the manifest's {spec.repeats}")
    return RunRecord(
        suite_id=manifest.suite_id,
        scenario_id=scenario_id,
        repeat=repeat,
        seed=spec.seeds[repeat],
        manifest_sha256=manifest.sha256,
        engine_version=manifest.engine_version,
        family=spec.family,
        clock_offset_seconds=clock_offset_seconds,
        timeline=timeline,
        chain=chain,
        diagnosis_completed_at=(
            _aware(diagnosis_completed_at) if diagnosis_completed_at is not None else None
        ),
        invalid_reasons=tuple(
            timeline_problems(timeline, spec.family, clock_offset_seconds)
            + chain_problems(chain, spec.family)
        ),
    )


class TestbedStore:
    """``<root>/<suite>/manifest.json`` and ``<root>/<suite>/<scenario>/<repeat>/``: write once."""

    __test__ = False  # not a pytest class

    def __init__(self, root: Path) -> None:
        self.root = root

    def _write_once(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() == content:
                return
            raise FileExistsError(f"{path} exists and differs; a re-run is a new repeat")
        path.write_bytes(content)
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    def write_manifest(self, manifest: SuiteManifest) -> Path:
        if not manifest.verified():
            raise ValueError("only a frozen manifest is written")
        path = self.root / manifest.suite_id / "manifest.json"
        self._write_once(path, _canonical(manifest.model_dump(mode="json")))
        return path

    def load_manifest(self, suite_id: str) -> SuiteManifest:
        manifest = SuiteManifest.model_validate_json(
            (self.root / suite_id / "manifest.json").read_bytes()
        )
        if not manifest.verified():
            raise ValueError("the stored manifest does not match its recorded digest")
        return manifest

    def write_artifact(self, record: RunRecord, name: str, content: bytes) -> Path:
        """One more file of a run (journal, series, stored diagnoses, score); written once, read-only."""
        if "/" in name or name.startswith("."):
            raise ValueError("an artifact name is a plain file name")
        path = self.run_dir(record.suite_id, record.scenario_id, record.repeat) / name
        self._write_once(path, content)
        return path

    def run_dir(self, suite_id: str, scenario_id: str, repeat: int) -> Path:
        return self.root / suite_id / scenario_id / str(repeat)

    def write_run(self, record: RunRecord) -> Path:
        stored = self.load_manifest(record.suite_id)
        if stored.sha256 != record.manifest_sha256:
            raise ValueError("the run was recorded against a different manifest")
        directory = self.run_dir(record.suite_id, record.scenario_id, record.repeat)
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError(f"{directory} already holds a run; a re-run is a new repeat")
        body = record.model_dump(mode="json")
        self._write_once(directory / "run.json", _canonical(body))
        self._write_once(directory / "timeline.json", _canonical(body["timeline"]))
        self._write_once(directory / "chain.json", _canonical(body["chain"]))
        return directory
