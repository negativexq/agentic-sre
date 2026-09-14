"""Offline ITBench-Lite SRE adapter and evaluator boundary."""

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntity,
    ITBenchEntityPrediction,
    ITBenchEvidenceCategory,
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchScenario,
    ITBenchScenarioQualification,
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
from packages.evals.itbench.external_context import (
    ITBENCH_EXTERNAL_CONTEXT_VERSION,
    MAX_EXTERNAL_CONTEXT_CHARS,
    build_external_context,
    normalize_alerts,
)
from packages.evals.itbench.external_contracts import (
    ExternalRootCause,
    ExternalStop,
    ITBenchDecisionType,
    ITBenchExternalResult,
    ITBenchInvestigationDecisionV1,
)
from packages.evals.itbench.external_registry import ITBenchExternalToolRegistry
from packages.evals.itbench.external_runtime import (
    ITBENCH_EXTERNAL_PROMPT,
    ITBENCH_EXTERNAL_PROMPT_VERSION,
    ExternalInvestigationRuntime,
    external_prompt_hash,
)
from packages.evals.itbench.grader import (
    ITBenchEntityGrade,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.incident import build_observable_incident
from packages.evals.itbench.official import (
    OFFICIAL_EVALUATOR_REVISION,
    OFFICIAL_EVALUATOR_SOURCE,
    OFFICIAL_OUTPUT_RELATIVE_PATH,
    OfficialITBenchEvaluatorSpec,
    official_evaluator_spec,
)
from packages.evals.itbench.output_adapter import (
    adapt_a1_output,
    adapt_external_output,
    entities_from_k8s_records,
    write_official_output,
)
from packages.evals.itbench.persistence import ITBenchRunStore, atomic_json_write
from packages.evals.itbench.registry import ITBenchSnapshotToolRegistry
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
    "ITBenchScenarioQualification",
    "ExternalRootCause",
    "ExternalStop",
    "ITBenchDecisionType",
    "ITBenchExternalResult",
    "ITBenchInvestigationDecisionV1",
    "ITBenchExternalToolRegistry",
    "ExternalInvestigationRuntime",
    "ITBENCH_EXTERNAL_CONTEXT_VERSION",
    "MAX_EXTERNAL_CONTEXT_CHARS",
    "ITBENCH_EXTERNAL_PROMPT",
    "ITBENCH_EXTERNAL_PROMPT_VERSION",
    "build_external_context",
    "normalize_alerts",
    "external_prompt_hash",
    "ITBenchLiteDataset",
    "ITBenchRunStore",
    "ITBenchScenario",
    "ITBenchSnapshotBackend",
    "ITBenchSnapshotToolRegistry",
    "build_trace_indexes",
    "OFFICIAL_EVALUATOR_REVISION",
    "OFFICIAL_EVALUATOR_SOURCE",
    "OFFICIAL_OUTPUT_RELATIVE_PATH",
    "OfficialITBenchEvaluatorSpec",
    "adapt_a1_output",
    "adapt_external_output",
    "atomic_json_write",
    "build_observable_incident",
    "entities_from_k8s_records",
    "grade_root_cause_entities",
    "macro_average",
    "official_evaluator_spec",
    "write_official_output",
    "trace_index_path",
]
