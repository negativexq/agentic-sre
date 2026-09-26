"""M19-2.5: RCA never reads the materialized instance index as evidence.

``entity_instances`` is updated in place by the collector. Its time fields are
bookkeeping, not temporal facts, so nothing under ``packages/rca`` may import
its repository or row, name the table, or read those fields.
"""

from __future__ import annotations

import ast
from pathlib import Path

RCA = Path(__file__).resolve().parents[3] / "packages" / "rca"
FORBIDDEN_NAMES = frozenset({"EntityInstanceRepository", "EntityInstanceRow"})
FORBIDDEN_FIELDS = frozenset({"first_observed_at", "last_observed_at", "deleted_observed_at"})
FORBIDDEN_TABLE = "entity_instances"


def violations(source: str, filename: str = "<source>") -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.ImportFrom | ast.Import):
            found += [
                f"{filename}:{node.lineno} imports {alias.name}"
                for alias in node.names
                if alias.name.rsplit(".", 1)[-1] in FORBIDDEN_NAMES
            ]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{filename}:{node.lineno} uses {node.id}")
        elif isinstance(node, ast.Attribute) and (
            node.attr in FORBIDDEN_NAMES or node.attr in FORBIDDEN_FIELDS
        ):
            found.append(f"{filename}:{node.lineno} reads .{node.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (FORBIDDEN_TABLE in node.value or node.value in FORBIDDEN_FIELDS)
        ):
            found.append(f"{filename}:{node.lineno} names {node.value!r}")
    return found


def test_rca_does_not_touch_the_instance_index() -> None:
    sources = sorted(RCA.rglob("*.py"))
    assert sources
    found = [
        item
        for path in sources
        for item in violations(path.read_text(encoding="utf-8"), str(path.relative_to(RCA)))
    ]
    assert found == []


def test_the_scan_catches_each_kind_of_access() -> None:
    offending = """
from packages.storage.repositories import EntityInstanceRepository
import packages.storage.models as models

def leak(session, row):
    models.EntityInstanceRow
    session.execute("SELECT * FROM entity_instances")
    getattr(row, "last_observed_at")
    return row.first_observed_at, row.deleted_observed_at
"""
    found = violations(offending)
    assert any("imports EntityInstanceRepository" in item for item in found)
    assert any(".EntityInstanceRow" in item for item in found)
    assert any("entity_instances" in item for item in found)
    assert any("'last_observed_at'" in item for item in found)
    assert any(".first_observed_at" in item for item in found)
    assert any(".deleted_observed_at" in item for item in found)
