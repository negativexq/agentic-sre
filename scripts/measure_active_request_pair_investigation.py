"""Actual investigation graph + taped pair acquisition over a frozen K8S DEV case."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from packages.rca.engine import RCA_ENGINE_VERSION, EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.candidates import build_observation_candidates
from packages.rca.investigation.environment import (
    SourceInvestigationBackend,
    TempoInvestigationBackend,
)
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import SEED_FULL_SOURCE, InvestigationConfig
from packages.rca.model import InvestigationAction, TraceSpanStatus
from packages.rca.provider_adapter import ProviderAdapter, ProviderBoundary, ProviderReaders
from packages.rca.replay import ReplayProviderAdapter, _Cursor, _load_tape
from packages.rca.source import InMemorySource
from packages.storage.models import Base, InvestigationReadRow
from scripts.kubernetes_rollout_binding_analysis import load_capture_source


@dataclass
class ActivePairSource(InMemorySource):
    provider_adapter: ProviderBoundary | None = None

    def supports(self, capability: str) -> bool:
        if capability in {"runtime_traces", "resource_pressure", "traffic"}:
            return bool(self.provider_adapter and self.provider_adapter.supports(capability))
        return capability in {"history", "events", "logs", "incident_events", "incident_changes"}

    def supports_typed_runtime(self, capability: str) -> bool:
        return bool(self.provider_adapter and self.provider_adapter.supports(capability))

    def investigation_backend(self) -> TempoInvestigationBackend:
        assert self.provider_adapter is not None
        return TempoInvestigationBackend(
            SourceInvestigationBackend(self), self.provider_adapter, self.cutoff
        )


def measure(capture: Path, output: Path, *, policy_mode: str = "scripted") -> dict[str, Any]:
    if policy_mode not in {"scripted", "deterministic_intent"}:
        raise ValueError("Unsupported measurement policy")
    if output.exists():
        raise ValueError("Fresh output directory required")
    original, evidence, metadata, injection = load_capture_source(capture)
    output.mkdir(parents=True)
    engine = create_engine("sqlite:///" + str((output / "provider-tape.sqlite").resolve()))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    readers = ProviderReaders.from_environment()
    if readers.tempo is None:
        raise ValueError("TEMPO_URL must configure the real pair reader")
    run_id = "active-pair-dev-" + uuid4().hex[:8]
    adapter = ProviderAdapter(run_id, "INVESTIGATION", factory, readers)
    protocol = {
        "engine_version": RCA_ENGINE_VERSION,
        "run_id": run_id,
        "provider_capabilities": adapter.capabilities(),
        "profile": "FROZEN_K8S_DEV_ACTUAL_INVESTIGATION_GRAPH",
        "policy": policy_mode,
        "alert_channel": "DRIVER_NOT_PROMETHEUS",
        "source_sha256": {
            str(p): sha256(p.read_bytes()).hexdigest()
            for p in (
                capture / "remote-evidence.json",
                capture / "object-versions.json",
                capture / "events.json",
                Path(__file__),
            )
        },
        "source_edit": "WITHHOLD_CURRENT_FAILED_CLIENT_SPANS_ONLY",
        "production_cp_used": False,
        "new_holdout": False,
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    observed = ActivePairSource(**vars(original), provider_adapter=adapter)
    observed.trace_items = [
        s
        for s in observed.trace_items
        if not (s.span_kind == "CLIENT" and s.status is TraceSpanStatus.ERROR)
    ]
    case = build_case(observed)
    diagnosis = diagnose_case(case)
    config = EngineConfig()
    candidates = build_observation_candidates(case=case, diagnosis=diagnosis, engine_config=config)
    candidate = next(
        c for c in candidates if c.query.source_log_ids and c.target.name == metadata["caller"]
    )
    action = InvestigationAction(
        action="inspect",
        gap_id=candidate.gap_ids[0],
        capability=candidate.capability,
        target=candidate.target,
        query=candidate.query,
        rationale="Corroborate recorded caller refusals by exact source log identities",
    )
    started = datetime.now(UTC)
    settings = InvestigationConfig(seed_mode=SEED_FULL_SOURCE, engine=config)
    result = investigate_diagnosis(
        observed,
        diagnosis=diagnosis,
        policy=ScriptedInvestigationPolicy(actions=[action])
        if policy_mode == "scripted"
        else DeterministicIntentPolicy(),
        config=settings,
        initial_case=case,
        started_at=started,
    )
    (output / "live-result.json").write_text(result.model_dump_json(indent=2) + "\n")
    replay = ReplayProviderAdapter(
        run_id, "INVESTIGATION", adapter.capabilities(), _load_tape(factory, run_id), _Cursor(0)
    )
    replay_source = ActivePairSource(**vars(original), provider_adapter=replay)
    replay_source.trace_items = list(observed.trace_items)
    replay_case = build_case(replay_source)
    replayed = investigate_diagnosis(
        replay_source,
        diagnosis=diagnose_case(replay_case),
        policy=ScriptedInvestigationPolicy(actions=[action])
        if policy_mode == "scripted"
        else DeterministicIntentPolicy(),
        config=settings,
        initial_case=replay_case,
        started_at=started,
    )
    (output / "replay-result.json").write_text(replayed.model_dump_json(indent=2) + "\n")
    with factory() as session:
        rows = session.scalars(
            select(InvestigationReadRow).order_by(InvestigationReadRow.sequence)
        ).all()
        reads = [
            {
                "capability": r.capability,
                "query_key": r.query_key,
                "descriptor": r.query_descriptor,
                "observation": r.observation,
                "status": r.status,
            }
            for r in rows
        ]
    (output / "provider-reads.json").write_text(json.dumps(reads, indent=2) + "\n")
    summary = {
        "engine_version": RCA_ENGINE_VERSION,
        "initial_resolution": result.initial_resolution.value,
        "final_resolution": result.final_resolution.value,
        "root": result.diagnosis.root_cause.canonical if result.diagnosis.root_cause else None,
        "tool_calls": result.tool_calls,
        "rejected_actions": result.rejected_actions,
        "source_log_ids": candidate.query.source_log_ids,
        "new_evidence_count": result.unique_evidence_added,
        "provider_reads": len(reads),
        "trace_fetch_attempts": sum(
            o.payload.get("request_pair_cost", {}).get("trace_fetch_attempts", 0)
            for o in result.observations
        ),
        "replay_digest_matches": diagnosis_epistemic_digest(result.diagnosis)
        == diagnosis_epistemic_digest(replayed.diagnosis),
        "replay_observations_match": result.observations == replayed.observations,
        "replay_tape_consumed": replay.next_sequence is None,
        "production_cp_used": False,
        "new_holdout": False,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    engine.dispose()
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--policy", choices=("scripted", "deterministic_intent"), default="scripted")
    args = p.parse_args()
    print(json.dumps(measure(args.capture, args.output, policy_mode=args.policy), indent=2))


if __name__ == "__main__":
    main()
