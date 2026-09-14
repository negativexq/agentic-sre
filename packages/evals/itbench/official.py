"""Pinned metadata for the optional official ITBench evaluator boundary."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

OFFICIAL_EVALUATOR_SOURCE = "itbench-hub/ITBench-Evaluations"
OFFICIAL_EVALUATOR_REVISION = "14f026fc9cc348c4ecec5ab32714de954c95c1b1"
OFFICIAL_OUTPUT_RELATIVE_PATH = "agent-outputs/{scenario_id}/{trial}/outputs/agent_output.json"


class OfficialITBenchEvaluatorSpec(BaseModel):
    """Reproducibility identity for the secondary evaluator, not a judge run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str = OFFICIAL_EVALUATOR_SOURCE
    revision: str = Field(min_length=40, max_length=40)
    criteria: tuple[str, ...] = (
        "ROOT_CAUSE_ENTITY",
        "ROOT_CAUSE_REASONING",
        "PROPAGATION_CHAIN",
        "FAULT_LOCALIZATION",
    )
    executed: bool = False


def official_evaluator_spec() -> OfficialITBenchEvaluatorSpec:
    """Return the pinned official evaluator contract used for future E1 runs."""
    return OfficialITBenchEvaluatorSpec(revision=OFFICIAL_EVALUATOR_REVISION)


__all__ = [
    "OFFICIAL_EVALUATOR_REVISION",
    "OFFICIAL_EVALUATOR_SOURCE",
    "OFFICIAL_OUTPUT_RELATIVE_PATH",
    "OfficialITBenchEvaluatorSpec",
    "official_evaluator_spec",
]
