"""Canonical, typed preregistration contract for future live smoke runs."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION
from packages.evals.itbench.e9_identity import collect_e9_identity
from packages.evals.itbench.e9_runtime import ITBENCH_E9_PROMPT_VERSION, e9_prompt_hash
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V5

LIVE_SMOKE_OFFLINE_READINESS: Final[Literal["READY_FOR_SINGLE_LIVE_SMOKE_REVIEW"]] = (
    "READY_FOR_SINGLE_LIVE_SMOKE_REVIEW"
)
LIVE_SMOKE_REQUIRED_BUDGET = 8
LIVE_SMOKE_MAX_MODEL_CALLS = 8
LIVE_SMOKE_MAX_AGENT_TURNS = 8
LIVE_SMOKE_MAX_SEMANTIC_ACTIONS = 8
LIVE_SMOKE_MAX_WALL_TIME_SECONDS = 180
LIVE_SMOKE_MAX_CONSECUTIVE_REJECTIONS = 2


class ITBenchLiveSmokeRuntimeIdentity(BaseModel):
    """Git/content identity collected before future provider construction."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    git_head: str = Field(min_length=1)
    relevant_paths: list[str] = Field(min_length=1)
    relevant_content_sha256: dict[str, str]
    bundle_sha256: str = Field(min_length=1)
    relevant_worktree_dirty: bool


class ITBenchLiveSmokeFixture(BaseModel):
    """The only fixture identity permitted by the future single-smoke contract."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scenario_id: Literal["Scenario-999"]
    fixture_path: Literal["tests/fixtures/itbench_smoke/Scenario-999"]
    synthetic: Literal[True]
    official_scenario: Literal[False]
    ground_truth_loaded: Literal[False]


class ITBenchLiveSmokeManifestV1(BaseModel):
    """One canonical preregistration shape consumed by future preflight."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution: str = Field(pattern=r"^ITB-CONTROL-LIVE-SMOKE-[0-9]{3}$", max_length=64)
    dataset_revision: str = Field(min_length=1, max_length=255)
    runtime_identity: ITBenchLiveSmokeRuntimeIdentity
    control_policy_hash: str = Field(min_length=1)
    semantic_registry_hash: str = Field(min_length=1)
    semantic_capability_policy_hash: str = Field(min_length=1)
    provider_schema_hash: str = Field(min_length=1)
    context_planner_hash: str = Field(min_length=1)
    candidate_discovery_hash: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_hash: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1, max_length=128)
    protocol_hash: str = Field(min_length=1)
    provider: Literal["openai"]
    model: Literal["gpt-5.6-luna"]
    reasoning_effort: Literal["none"]
    provider_retries: Literal[0]
    required_budget: int = Field(gt=0)
    max_model_calls: int = Field(gt=0)
    max_agent_turns: int = Field(gt=0)
    max_semantic_actions: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    max_consecutive_rejected_actions: int = Field(ge=0)
    judge_disabled: Literal[True]
    rerun_policy: Literal["NEVER"]
    offline_readiness_status: Literal["READY_FOR_SINGLE_LIVE_SMOKE_REVIEW"]
    fixture: ITBenchLiveSmokeFixture


def build_live_smoke_manifest(
    root: Path,
    relevant_paths: tuple[str, ...],
    *,
    execution: str = "ITB-CONTROL-LIVE-SMOKE-002",
) -> ITBenchLiveSmokeManifestV1:
    """Build a future manifest from the source identity at the frozen HEAD."""
    identity = collect_e9_identity(root, relevant_paths)
    hashes = identity["relevant_content_sha256"]

    def source_hash(path: str) -> str:
        value = hashes.get(path)
        if not isinstance(value, str) or not value:
            raise ValueError(f"relevant source hash missing: {path}")
        return value

    return ITBenchLiveSmokeManifestV1(
        execution=execution,
        dataset_revision=ITBENCH_DATASET_REVISION,
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_validate(identity),
        control_policy_hash=source_hash("packages/evals/itbench/e9_control.py"),
        semantic_registry_hash=source_hash("packages/evals/itbench/e9_semantic.py"),
        semantic_capability_policy_hash=source_hash("packages/evals/itbench/e9_semantic.py"),
        provider_schema_hash=source_hash("packages/provider/openai.py"),
        context_planner_hash=source_hash("packages/evals/itbench/e9_context.py"),
        candidate_discovery_hash=source_hash("packages/evals/itbench/e9_semantic.py"),
        prompt_version=ITBENCH_E9_PROMPT_VERSION,
        prompt_hash=e9_prompt_hash(),
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V5,
        protocol_hash=source_hash("packages/evals/itbench/external_contracts.py"),
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
        required_budget=LIVE_SMOKE_REQUIRED_BUDGET,
        max_model_calls=LIVE_SMOKE_MAX_MODEL_CALLS,
        max_agent_turns=LIVE_SMOKE_MAX_AGENT_TURNS,
        max_semantic_actions=LIVE_SMOKE_MAX_SEMANTIC_ACTIONS,
        max_wall_time_seconds=LIVE_SMOKE_MAX_WALL_TIME_SECONDS,
        max_consecutive_rejected_actions=LIVE_SMOKE_MAX_CONSECUTIVE_REJECTIONS,
        judge_disabled=True,
        rerun_policy="NEVER",
        offline_readiness_status=LIVE_SMOKE_OFFLINE_READINESS,
        fixture=ITBenchLiveSmokeFixture(
            scenario_id="Scenario-999",
            fixture_path="tests/fixtures/itbench_smoke/Scenario-999",
            synthetic=True,
            official_scenario=False,
            ground_truth_loaded=False,
        ),
    )


__all__ = [
    "ITBenchLiveSmokeFixture",
    "ITBenchLiveSmokeManifestV1",
    "ITBenchLiveSmokeRuntimeIdentity",
    "LIVE_SMOKE_MAX_AGENT_TURNS",
    "LIVE_SMOKE_MAX_CONSECUTIVE_REJECTIONS",
    "LIVE_SMOKE_MAX_MODEL_CALLS",
    "LIVE_SMOKE_MAX_SEMANTIC_ACTIONS",
    "LIVE_SMOKE_MAX_WALL_TIME_SECONDS",
    "LIVE_SMOKE_OFFLINE_READINESS",
    "LIVE_SMOKE_REQUIRED_BUDGET",
    "build_live_smoke_manifest",
]
