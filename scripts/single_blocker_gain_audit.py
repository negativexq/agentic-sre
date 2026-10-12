"""Recalculate the 23 previously measured single-blocker candidates, read-only.

No closure or coverage condition is changed. The C-withheld arm is diagnostic.
The input candidate list is development data, not a validation set.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from packages.rca import frontier, resolution
from packages.rca.engine import EngineConfig, _selected_pods, build_case, diagnose_case
from packages.rca.evidence_coverage import continuously_observed
from packages.rca.model import EntityRef
from packages.rca.replay import ReplaySource
from packages.rca.resolution import _covered_symptoms, _execution_interval
from scripts.dependency_coverage_recalculation import (
    decision,
    readonly_engine,
    without_negative_closure,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    candidates = json.loads(args.candidates.read_text())
    for candidate in candidates:
        engine = readonly_engine(candidate["db"])
        try:
            factory = sessionmaker(engine)
            source = ReplaySource.from_run(candidate["run"], session_factory=factory)
            config = EngineConfig()
            case = build_case(source, config)
            diagnosis = diagnose_case(case, config=config)
            before = decision(diagnosis)
            with patch.object(frontier, "dependency_declared_unused", without_negative_closure):
                after = decision(diagnose_case(case, config=config))
            trace = diagnosis.resolution_trace
            assert trace is not None
            by_id = {h.hypothesis_id: h for h in case.hypotheses}
            topology = case.context.topology
            owners = {e.source: e.target for e in topology.edges if e.relation == "owned_by"}
            selects = _selected_pods(topology)
            statuses = source.pod_status_observations()
            coverage = []
            for audit in trace.hypothesis_audits:
                if audit.hypothesis_id not in trace.mechanism_verified_hypotheses:
                    continue
                witnesses = [
                    w
                    for r in audit.root_support
                    if r.support_kind == "OBSERVED_MECHANISM_CAUSE"
                    for w in r.witnesses
                ]
                hypothesis = by_id[audit.hypothesis_id]
                covered = _covered_symptoms(witnesses, owners, selects, statuses)
                coverage.append(
                    {
                        "claim": audit.hypothesis_id,
                        "actor": hypothesis.causal_actor.canonical,
                        "uncovered": sorted(
                            e.canonical for e in hypothesis.symptom_entities if e not in covered
                        ),
                        "witnesses": [w.model_dump(mode="json") for w in witnesses],
                    }
                )
            alternatives = {a.alternative_id: a for a in case.structural_alternatives}
            for side in (before, after):
                for answer in side["answers"]:
                    alternative = alternatives.get(answer["alternative_id"])
                    if alternative:
                        answer["actor"] = alternative.actor.canonical
                        answer["role"] = alternative.role
            reads = source.trace_read_record()
            collective_checks: list[dict[str, Any]] = []

            def collective(
                witnesses: Any,
                owners: Any,
                selected: Any = None,
                pod_statuses: Any = (),
                _source: Any = source,
                _checks: Any = collective_checks,
            ) -> set[EntityRef]:
                covered = _covered_symptoms(witnesses, owners, selected, pod_statuses)
                for service, pods in (selected or {}).items():
                    if len(pods) < 2 or not set(pods).issubset({w.symptom for w in witnesses}):
                        continue
                    intervals = [_execution_interval(w) for w in witnesses if w.symptom in pods]
                    if not intervals or any(i is None for i in intervals):
                        continue
                    start = max(i[0] for i in intervals if i is not None)
                    end = min(i[1] for i in intervals if i is not None)
                    record = _source.evidence_coverage_record()
                    gap = (
                        record is None
                        or start >= end
                        or any(
                            continuously_observed(record, service.namespace or "", kind, start, end)
                            for kind in ("Pod", "Service")
                        )
                    )
                    uids = {
                        pod.canonical: sorted({p.uid for p in pod_statuses if p.pod == pod})
                        for pod in pods
                    }
                    identities_known = all(
                        len(values) == 1 and values[0] for values in uids.values()
                    )
                    _checks.append(
                        {
                            "service": service.canonical,
                            "pods": sorted(p.canonical for p in pods),
                            "common_start": start.isoformat(),
                            "common_end": end.isoformat(),
                            "continuity_proven": not gap,
                            "uids": uids,
                        }
                    )
                    if not gap and identities_known:
                        covered.add(service)
                return covered

            with patch.object(resolution, "_covered_symptoms", collective):
                shadow = decision(diagnose_case(case, config=config))
                with patch.object(frontier, "dependency_declared_unused", without_negative_closure):
                    strict_shadow = decision(diagnose_case(case, config=config))
            controls = []
            for audit in trace.hypothesis_audits:
                if audit.hypothesis_id not in trace.mechanism_verified_hypotheses:
                    continue
                witnesses = [
                    w
                    for r in audit.root_support
                    if r.support_kind == "OBSERVED_MECHANISM_CAUSE"
                    for w in r.witnesses
                ]
                original = _covered_symptoms(witnesses, owners, selects, statuses)
                gains = collective(witnesses, owners, selects, statuses) - original
                for service in gains:
                    pods = selects[service]
                    target = next(iter(pods))
                    missing_witness = [w for w in witnesses if w.symptom != target]
                    assert service not in collective(missing_witness, owners, selects, statuses)
                    missing_status = [p for p in statuses if p.pod != target]
                    existing_on_missing = _covered_symptoms(
                        witnesses, owners, selects, missing_status
                    )
                    assert service not in (
                        collective(witnesses, owners, selects, missing_status) - existing_on_missing
                    )
                    extra = EntityRef(
                        kind="Pod", namespace=service.namespace, name="audit-extra-replica"
                    )
                    unknown_roster = dict(selects) | {service: frozenset((*pods, extra))}
                    assert service not in collective(witnesses, owners, unknown_roster, statuses)
                    absent_proof = SimpleNamespace(evidence_coverage_record=lambda: None)
                    assert service not in collective(
                        witnesses, owners, selects, statuses, _source=absent_proof
                    )
                    controls.append(
                        {
                            "service": service.canonical,
                            "missing_witness": "no_coverage",
                            "missing_identity": "no_additional_coverage",
                            "existing_single_pod_rule_covers_missing_identity": service
                            in existing_on_missing,
                            "extra_unwitnessed_replica": "no_coverage",
                            "missing_continuity_proof": "no_coverage",
                        }
                    )
            repeated = decision(diagnose_case(case, config=config))
            assert all(repeated[key] == before[key] for key in before if key != "answers")
            cutoff = source.observation_cutoff()
            assert cutoff is not None
            results.append(
                {
                    "database": candidate["db"],
                    "run": candidate["run"],
                    "incident": diagnosis.incident_id,
                    "alerts": [a.model_dump(mode="json") for a in source.alerts()],
                    "baseline": before,
                    "c_withheld": after,
                    "coverage": coverage,
                    "trace_reads": [r.model_dump(mode="json") for r in reads or ()],
                    "trace_reads_present": reads is not None,
                    "pod_statuses": [p.model_dump(mode="json") for p in statuses],
                    "selects": {
                        s.canonical: sorted(p.canonical for p in pods)
                        for s, pods in selects.items()
                    },
                    "unresolved": list(trace.unresolved_hypotheses),
                    "snapshot_window_end": cutoff.isoformat(),
                    "collective_shadow": shadow,
                    "collective_c_withheld_shadow": strict_shadow,
                    "collective_checks": collective_checks,
                    "negative_controls": controls,
                }
            )
        finally:
            engine.dispose()
        args.output.write_text(json.dumps(results, indent=2, default=str))
        print(f"{len(results)}/{len(candidates)} candidates", flush=True)


if __name__ == "__main__":
    main()
