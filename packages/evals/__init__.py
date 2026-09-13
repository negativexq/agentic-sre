"""Credit-free frozen scenarios and deterministic investigation graders."""

from packages.evals.a1_generalization import (
    A1_GENERALIZATION_SCENARIOS,
    A1GeneralizationScenario,
    ObservableEvidenceSurface,
    generalization_dataset_hash,
    generalization_target_hash,
)
from packages.evals.a1_graders import (
    A1_GRADER_VERSION,
    A1AggregateGrade,
    A1FailureLabel,
    A1Rate,
    A1ScenarioGrade,
    aggregate_a1_grades,
    grade_a1_run,
)
from packages.evals.a1_reporting import a0_compatibility_baseline
from packages.evals.a1_targets import (
    A1_COMPATIBILITY_TARGETS,
    A1_TARGET_BY_SCENARIO,
    A1EvaluationTarget,
    a1_compatibility_target_hash,
)
from packages.evals.capabilities import ScenarioCapability, capability_matrix
from packages.evals.dataset import FROZEN_DATASET, FrozenIncident, frozen_dataset_hash
from packages.evals.graders import EvidenceGrade, HypothesisGrade, grade_evidence, grade_hypothesis
from packages.evals.live_fixtures import (
    FIXTURE_BY_NAME,
    FIXTURE_DEFINITIONS,
    GENERALIZATION_FIXTURE_BY_NAME,
    GENERALIZATION_FIXTURE_DEFINITIONS,
    BenchmarkTrial,
    FixtureDefinition,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    fixture_registry_is_complete,
    preflight_evidence,
    select_harness_scenarios,
)
from packages.evals.runner import OfflineBenchmarkReport, run_offline_benchmark

__all__ = [
    "A1AggregateGrade",
    "A1EvaluationTarget",
    "A1FailureLabel",
    "A1GeneralizationScenario",
    "A1_GENERALIZATION_SCENARIOS",
    "A1_GRADER_VERSION",
    "A1Rate",
    "A1ScenarioGrade",
    "A1_COMPATIBILITY_TARGETS",
    "A1_TARGET_BY_SCENARIO",
    "EvidenceGrade",
    "FROZEN_DATASET",
    "FrozenIncident",
    "HypothesisGrade",
    "OfflineBenchmarkReport",
    "frozen_dataset_hash",
    "ScenarioCapability",
    "capability_matrix",
    "grade_evidence",
    "grade_hypothesis",
    "aggregate_a1_grades",
    "grade_a1_run",
    "a1_compatibility_target_hash",
    "generalization_dataset_hash",
    "generalization_target_hash",
    "ObservableEvidenceSurface",
    "a0_compatibility_baseline",
    "run_offline_benchmark",
    "BenchmarkTrial",
    "FIXTURE_BY_NAME",
    "FIXTURE_DEFINITIONS",
    "FixtureDefinition",
    "FixtureLifecycle",
    "GENERALIZATION_FIXTURE_BY_NAME",
    "GENERALIZATION_FIXTURE_DEFINITIONS",
    "LiveBenchmarkEnvironment",
    "fixture_registry_is_complete",
    "preflight_evidence",
    "select_harness_scenarios",
]
