"""M19-6.8: the clean-baseline probe path has no write, capture or provider authority."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "apps" / "control_plane" / "baseline.py"
MAIN = ROOT / "apps" / "control_plane" / "main.py"
FORBIDDEN_CALLS = {
    "add",
    "add_all",
    "commit",
    "flush",
    "delete",
    "merge",
    "execute",
    "save_revision",
    "apply_revision",
    "build_manifest",
    "snapshot",
    "snapshot_result",
    "create",
    "append",
    "run",
    "investigate_diagnosis",
}
FORBIDDEN_NAMES = {
    "ProviderAdapter",
    "ProviderReaders",
    "IncidentRepository",
    "DiagnosisRepository",
    "EvidenceRequirementRepository",
    "IncidentEventRepository",
    "InvestigationRunRepository",
    "SnapshotCycleRepository",
    "build_manifest",
    "DiagnosisService",
}


def violations(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_CALLS:
                found.add(f".{node.func.attr}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.add(node.id)
        if isinstance(node, ast.ImportFrom):
            found |= {alias.name for alias in node.names if alias.name in FORBIDDEN_NAMES}
    return found


def _handler() -> ast.FunctionDef:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    (handler,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "baseline_probe"
    ]
    return handler


def test_the_probe_module_only_reads() -> None:
    assert violations(ast.parse(PROBE.read_text(encoding="utf-8"))) == set()


def test_the_endpoint_only_evaluates() -> None:
    handler = _handler()
    assert violations(handler) == set()
    called = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "evaluate_baseline" in called
    route = handler.decorator_list[0]
    assert isinstance(route, ast.Call)
    assert "require_api_token" in ast.unparse(route)  # same protection as the diagnosis POST


@pytest.mark.parametrize(
    "snippet",
    [
        "session.add(row)\n",
        "session.commit()\n",
        "DiagnosisRepository(session).save_revision(x)\n",
        "build_manifest(factory, request, timestamp=t)\n",
        "ProviderAdapter(run, 'ENGINE', factory, readers)\n",
        "IncidentRepository(session).create(incident)\n",
    ],
)
def test_each_rule_fires(snippet: str) -> None:
    assert violations(ast.parse(snippet))
