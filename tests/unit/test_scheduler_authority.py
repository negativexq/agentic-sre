"""M19-5.7: the deadline scheduler has no RCA decision or lifecycle authority (§1.3, I8).

A static check of ``apps/control_plane/scheduler.py``. The scheduler may read
requirements and revisions, mark OPEN requirements EXPIRED, and ask for a
revision through its injected ``run(incident_id, EVIDENCE_DEADLINE)``. It may
not reach RCA code, write rows itself, or name a precondition or lifecycle
outcome. Each rule is checked against the real module and shown to fire on a
small violating fixture.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCHEDULER = Path(__file__).resolve().parents[2] / "apps" / "control_plane" / "scheduler.py"

# Where decisions live: RCA code, the diagnosis service, raw ORM rows and guards.
FORBIDDEN_MODULES = (
    "packages.rca",
    "packages.evals",
    "apps.control_plane.diagnosis",
    "packages.storage.models",
    "packages.storage.evidence_guard",
)
ALLOWED_REPOSITORY_IMPORTS = {"DiagnosisRepository", "EvidenceRequirementRepository"}
WRITE_STATEMENTS = {"insert", "update", "delete", "text"}
# The only repository methods the scheduler may call; ``expire`` is its one write.
ALLOWED_METHODS = {
    "EvidenceRequirementRepository": {"open_requirements", "opening_onset", "expire"},
    "DiagnosisRepository": {"deadline_consumed", "revision_count"},
}
SESSION_WRITES = {
    "add",
    "add_all",
    "delete",
    "merge",
    "execute",
    "flush",
    "bulk_save_objects",
    "bulk_insert_mappings",
    "bulk_update_mappings",
}
DECISION_LITERALS = {
    "PASS",
    "PENDING",
    "DISQUALIFIED",
    "OPEN",
    "SATISFIED_BY_REVISION",
    "SUPERSEDED_BY_REVISION",
}
DECISION_ATTRIBUTES = {
    "PASS",
    "PENDING",
    "DISQUALIFIED",
    "passed",
    "pending",
    "disqualified",
    "apply_revision",
    "save_revision",
}


def _docstrings(tree: ast.Module) -> set[int]:
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef))]
    return {
        id(node.body[0].value)
        for node in nodes
        if node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _repository_bindings(tree: ast.Module) -> dict[str, str]:
    """Local names bound to ``SomeRepository(...)`` instances."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id.endswith("Repository")
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value.func.id
    return bound


def boundary_violations(source: str) -> set[str]:
    """Every way ``source`` exceeds the scheduler's authority."""
    tree = ast.parse(source)
    found: set[str] = set()
    docstrings = _docstrings(tree)
    bindings = _repository_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_MODULES):
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            if module.startswith(FORBIDDEN_MODULES):
                found.add(f"import from {module}")
            elif module.startswith("packages.storage") and (
                module != "packages.storage.repositories" or not names <= ALLOWED_REPOSITORY_IMPORTS
            ):
                found.add(f"import {sorted(names)} from {module}")
            elif module.startswith("sqlalchemy") and names & WRITE_STATEMENTS:
                found.add(f"write statement import {sorted(names & WRITE_STATEMENTS)}")
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                # Row fields and their contents; local containers are fine.
                base = target.value if isinstance(target, ast.Subscript) else target
                if isinstance(base, ast.Attribute):
                    found.add(f"attribute assignment at line {node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and node.value in DECISION_LITERALS:
                found.add(f"decision literal {node.value!r}")
        elif isinstance(node, ast.Attribute) and node.attr in DECISION_ATTRIBUTES:
            found.add(f"decision attribute .{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method, receiver = node.func.attr, node.func.value
            if method in SESSION_WRITES:
                found.add(f"session write .{method}()")
            repository = None
            if isinstance(receiver, ast.Name) and receiver.id in bindings:
                repository = bindings[receiver.id]
            elif (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id.endswith("Repository")
            ):
                repository = receiver.func.id
            if repository is not None and method not in ALLOWED_METHODS.get(repository, set()):
                found.add(f"{repository}.{method}()")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run":
            if (
                len(node.args) != 2
                or node.keywords
                or not isinstance(node.args[1], ast.Name)
                or node.args[1].id != "EVIDENCE_DEADLINE"
            ):
                found.add(f"run(...) without EVIDENCE_DEADLINE at line {node.lineno}")
    return found


def _source() -> str:
    return SCHEDULER.read_text(encoding="utf-8")


def test_the_scheduler_stays_inside_its_authority() -> None:
    assert boundary_violations(_source()) == set()


def test_diagnosis_is_reached_only_through_the_injected_deadline_run() -> None:
    tree = ast.parse(_source())
    (constant,) = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "EVIDENCE_DEADLINE" for t in node.targets)
    ]
    assert isinstance(constant.value, ast.Constant) and constant.value.value == "EVIDENCE_DEADLINE"
    (entry,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_scheduler_pass"
    ]
    assert "run" in {arg.arg for arg in entry.args.kwonlyargs}  # injected, never imported
    runs = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run"
    ]
    assert len(runs) == 1
    # The one storage write the scheduler makes is expiry.
    expires = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "expire"
    ]
    assert len(expires) == 1


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        (
            "from packages.rca.resolution import resolve_hypotheses\n",
            "import from packages.rca.resolution",
        ),
        ("import packages.rca.engine\n", "import packages.rca.engine"),
        (
            "from apps.control_plane.diagnosis import DiagnosisService\n",
            "import from apps.control_plane.diagnosis",
        ),
        (
            "from packages.storage.models import EvidenceRequirementRow\n",
            "import from packages.storage.models",
        ),
        (
            "from packages.storage.repositories import DiagnosisRepository, IncidentRepository\n",
            "import ['DiagnosisRepository', 'IncidentRepository'] from packages.storage.repositories",
        ),
        ("from sqlalchemy import update\n", "write statement import ['update']"),
        (
            "r = EvidenceRequirementRepository(s)\nr.apply_revision(x)\n",
            "EvidenceRequirementRepository.apply_revision()",
        ),
        ("DiagnosisRepository(s).save_revision(x)\n", "DiagnosisRepository.save_revision()"),
        ("row.status = new\n", "attribute assignment at line 1"),
        ("row.document['x'] = 1\n", "attribute assignment at line 1"),
        ("session.add(row)\n", "session write .add()"),
        ("session.execute(stmt)\n", "session write .execute()"),
        ("status = 'SATISFIED_BY_REVISION'\n", "decision literal 'SATISFIED_BY_REVISION'"),
        ("status = 'SUPERSEDED_BY_REVISION'\n", "decision literal 'SUPERSEDED_BY_REVISION'"),
        ("x = result.pending(at)\n", "decision attribute .pending"),
        ("x = Status.DISQUALIFIED\n", "decision attribute .DISQUALIFIED"),
        ("run(incident_id, 'MANUAL')\n", "run(...) without EVIDENCE_DEADLINE at line 1"),
        (
            "run(incident_id, trigger=EVIDENCE_DEADLINE)\n",
            "run(...) without EVIDENCE_DEADLINE at line 1",
        ),
    ],
)
def test_each_boundary_rule_fires_on_a_violation(snippet: str, expected: str) -> None:
    assert expected in boundary_violations(snippet)


def test_harmless_code_is_not_flagged() -> None:
    harmless = '''
"""Docstrings may say OPEN, PENDING or SATISFIED_BY_REVISION."""
import logging
from datetime import timedelta
from sqlalchemy.orm import Session
from packages.storage.repositories import DiagnosisRepository
diagnoses = DiagnosisRepository(session)
count = diagnoses.revision_count(incident_id)
due = {}
due[incident_id] = None
session.commit()
run(incident_id, EVIDENCE_DEADLINE)
'''
    assert boundary_violations(harmless) == set()
