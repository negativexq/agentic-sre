"""Disk-conscious metadata indexes for expensive immutable snapshot lookups."""

from __future__ import annotations

import csv
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.dataset import ITBenchLiteDataset

csv.field_size_limit(sys.maxsize)


class _BinaryLineSource:
    """Yield decoded physical lines while retaining exact byte positions."""

    def __init__(self, path: Path) -> None:
        self.stream = path.open("rb")
        self.position = 0

    def __iter__(self) -> _BinaryLineSource:
        return self

    def __next__(self) -> str:
        line = self.stream.readline()
        if not line:
            self.stream.close()
            raise StopIteration
        self.position += len(line)
        return line.decode("utf-8")


def _iter_trace_offsets(
    source_path: Path, trace_column_name: str
) -> Iterator[tuple[str, int, int, int]]:
    source = _BinaryLineSource(source_path)
    reader = csv.reader(source, delimiter="\t")
    try:
        headers = next(reader)
        trace_column = headers.index(trace_column_name)
        row_index = 0
        while True:
            start = source.position
            try:
                row = next(reader)
            except StopIteration:
                return
            end = source.position
            if len(row) != len(headers):
                continue
            yield row[trace_column], row_index, start, end - start
            row_index += 1
    finally:
        source.stream.close()


def build_trace_indexes(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    """Build per-scenario trace-id indexes without copying source payloads."""
    built: list[dict[str, Any]] = []
    for scenario in dataset.scenarios():
        source_file = scenario.evidence_files[ITBenchEvidenceCategory.TRACES][0]
        source_path = Path(scenario.snapshot_path) / source_file
        index_dir = Path(scenario.snapshot_path) / ".index"
        index_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / "traces.sqlite3"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".traces.", suffix=".sqlite3", dir=index_dir
        )
        os.close(descriptor)
        try:
            connection = sqlite3.connect(temporary_name)
            try:
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                connection.execute(
                    "CREATE TABLE trace_index ("
                    "trace_id TEXT NOT NULL, row_index INTEGER NOT NULL, "
                    "byte_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL)"
                )
                connection.execute("CREATE INDEX trace_id_idx ON trace_index(trace_id)")
                rows: list[tuple[str, int, int, int]] = []
                for row in _iter_trace_offsets(source_path, "TraceId"):
                    rows.append(row)
                    if len(rows) >= 10_000:
                        connection.executemany("INSERT INTO trace_index VALUES (?, ?, ?, ?)", rows)
                        rows.clear()
                if rows:
                    connection.executemany("INSERT INTO trace_index VALUES (?, ?, ?, ?)", rows)
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary_name, index_path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        built.append(
            {
                "scenario_id": scenario.scenario_id,
                "source_file": source_file,
                "index_path": str(index_path),
                "source_bytes": source_path.stat().st_size,
                "index_bytes": index_path.stat().st_size,
            }
        )
    return {"status": "PASS", "kind": "trace_id_offset", "scenarios": built}


def trace_index_path(snapshot_path: str | Path) -> Path:
    """Return the optional scenario-local trace index path."""
    return Path(snapshot_path) / ".index" / "traces.sqlite3"


__all__ = ["build_trace_indexes", "trace_index_path"]
