"""Offline ITBench-Lite dataset, snapshot access, and deterministic grading."""

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntity,
    ITBenchEntityPrediction,
    ITBenchEvidenceCategory,
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchScenario,
    ITBenchScenarioQualification,
    parse_canonical_entity,
)
from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_LICENSE,
    ITBENCH_SCENARIO_IDS,
    ITBENCH_SOURCE,
    ITBENCH_SRE_VERSION,
    ITBenchDatasetError,
    ITBenchLiteDataset,
)
from packages.evals.itbench.grader import (
    ITBenchEntityGrade,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.io import atomic_json_write
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.evals.itbench.sparse_index import build_trace_indexes, trace_index_path

__all__ = [
    "ITBENCH_DATASET_REVISION",
    "ITBENCH_LICENSE",
    "ITBENCH_SCENARIO_IDS",
    "ITBENCH_SOURCE",
    "ITBENCH_SRE_VERSION",
    "ITBenchAgentOutput",
    "ITBenchDatasetError",
    "ITBenchEntity",
    "ITBenchEntityGrade",
    "ITBenchEntityPrediction",
    "ITBenchEvidenceCategory",
    "ITBenchGroundTruth",
    "ITBenchGroundTruthGroup",
    "ITBenchLiteDataset",
    "ITBenchScenario",
    "ITBenchScenarioQualification",
    "ITBenchSnapshotBackend",
    "atomic_json_write",
    "build_trace_indexes",
    "grade_root_cause_entities",
    "macro_average",
    "parse_canonical_entity",
    "trace_index_path",
]
