"""M19-6.5: the product harness has no evidence-clearing or evidence-mutation authority.

Scans every Python file under ``packages/evals/product/``, so a module added
later is covered without editing this test. Evidence-table mutation is judged
by the repository's single §1.5 scanner and allowlist; the product package is
not, and may not become, one of its exceptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from test_evidence_mutation_allowlist import ALLOWED, ROOT, violations

PRODUCT = ROOT / "packages" / "evals" / "product"
# The legacy suite's evidence reset and the module that orchestrates it.
FORBIDDEN_SYMBOLS = {"truncate_change_journal", "reset_change_journal"}
LEGACY_RESET_MODULE = "packages.evals.live.runner"
# ORM-level writes the §1.5 scanner (SQL statements) does not see.
ORM_WRITES = {
    "delete",
    "merge",
    "bulk_save_objects",
    "bulk_insert_mappings",
    "bulk_update_mappings",
}


def authority_violations(source: str, path: str) -> set[str]:
    found = {f"{operation} {table}" for _, operation, table in violations(source, path)}
    for node in ast.walk(ast.parse(source, path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == LEGACY_RESET_MODULE or alias.name.startswith(
                    LEGACY_RESET_MODULE + "."
                ):
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == LEGACY_RESET_MODULE:
                found.add(f"import from {node.module}")
            for alias in node.names:
                if alias.name in FORBIDDEN_SYMBOLS:
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SYMBOLS:
            found.add(f"reference {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SYMBOLS:
            found.add(f"reference .{node.attr}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ORM_WRITES
        ):
            found.add(f"orm write .{node.func.attr}()")
    return found


def _product_files() -> list[Path]:
    return sorted(PRODUCT.rglob("*.py"))


def test_the_product_package_has_no_evidence_authority() -> None:
    files = _product_files()
    assert {path.name for path in files} >= {
        "__init__.py",
        "spec.py",
        "actions.py",
        "timeline.py",
        "runner.py",
        "proof.py",
        "artifact.py",
        "scenarios.py",
    }
    found = {
        (path.relative_to(ROOT).as_posix(), item)
        for path in files
        for item in authority_violations(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
        )
    }
    assert found == set()


def test_the_product_package_is_not_a_mutation_exception() -> None:
    assert not any(path.startswith("packages/evals/product/") for path, _, _ in ALLOWED)


def test_the_product_cli_is_covered_too() -> None:
    source = (ROOT / "scripts" / "product_benchmark.py").read_text(encoding="utf-8")
    assert authority_violations(source, "scripts/product_benchmark.py") == set()


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        (
            "from packages.evals.live.actions import truncate_change_journal\n",
            "import truncate_change_journal",
        ),
        (
            "from packages.evals.live.actions import truncate_change_journal as reset\n",
            "import truncate_change_journal",
        ),
        (
            "from packages.evals.live.runner import reset_change_journal\n",
            "import from packages.evals.live.runner",
        ),
        ("import packages.evals.live.runner as legacy\n", "import packages.evals.live.runner"),
        ("legacy_actions.truncate_change_journal(context)\n", "reference .truncate_change_journal"),
        (
            'session.execute(text("DELETE FROM lifecycle_observations"))\n',
            "DELETE lifecycle_observations",
        ),
        ("session.execute(update(ObjectVersionRow).values(uid=None))\n", "UPDATE object_versions"),
        ('run("TRUNCATE TABLE event_versions")\n', "TRUNCATE event_versions"),
        ("session.delete(row)\n", "orm write .delete()"),
        (
            "session.bulk_update_mappings(EventVersionRow, rows)\n",
            "orm write .bulk_update_mappings()",
        ),
    ],
)
def test_each_authority_rule_fires(snippet: str, expected: str) -> None:
    assert expected in authority_violations(snippet, "packages/evals/product/fixture.py")


def test_reads_and_harmless_words_are_allowed() -> None:
    harmless = '''
"""Never truncate evidence; the legacy suite used to."""
from packages.evals.live.actions import Context
rows = session.scalars(select(ObjectVersionRow)).all()
label = "truncate"
'''
    assert authority_violations(harmless, "packages/evals/product/fixture.py") == set()
