from __future__ import annotations

from packages.evals.unresolved_subsumption import RULE_IDS, build_blind_audit
from packages.rca.source import InMemorySource


def test_full_production_path_builds_audit_without_gt() -> None:
    audit = build_blind_audit(InMemorySource(name="synthetic-subsumption"))
    assert audit.scenario_id == "synthetic-subsumption"
    assert tuple(item.rule_id for item in audit.rule_results) == RULE_IDS
    assert audit.production_resolution == "INSUFFICIENT_EVIDENCE"
