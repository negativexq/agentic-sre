"""Durability and phase-ledger tests for the benchmark harness."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.evals.benchmark_store import BenchmarkPhase, PhaseLedger, PhaseLedgerRecord


def test_phase_ledger_appends_and_reads_records(tmp_path: Path) -> None:
    ledger = PhaseLedger(tmp_path / "phases.jsonl", execution_id="run-1")
    started = ledger.start("V001", BenchmarkPhase.VERIFY_BASELINE)
    ledger.end("V001", BenchmarkPhase.VERIFY_BASELINE, started)

    records = ledger.read()
    assert [(item.event, item.phase) for item in records] == [
        ("start", BenchmarkPhase.VERIFY_BASELINE),
        ("end", BenchmarkPhase.VERIFY_BASELINE),
    ]
    assert records[1].duration_seconds is not None


def test_phase_ledger_tolerates_only_a_truncated_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "phases.jsonl"
    ledger = PhaseLedger(path, execution_id="run-1")
    ledger.append(
        PhaseLedgerRecord(
            execution_id="run-1",
            scenario_id="V001",
            phase=BenchmarkPhase.RUN_AGENT,
            event="start",
            wall_timestamp=datetime.now(UTC),
            monotonic_timestamp=1.0,
        )
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"execution_id":"run-1"')
    assert len(ledger.read()) == 1


def test_phase_ledger_rejects_corrupt_nontrailing_record(tmp_path: Path) -> None:
    path = tmp_path / "phases.jsonl"
    path.write_text("not-json\n{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        PhaseLedger(path, execution_id="run-1").read()


def test_phase_ledger_context_records_failed_phase(tmp_path: Path) -> None:
    ledger = PhaseLedger(tmp_path / "phases.jsonl", execution_id="run-1")
    with pytest.raises(RuntimeError, match="boom"):
        with ledger.phase("V003", BenchmarkPhase.RUN_WORKLOAD):
            raise RuntimeError("boom")
    records = ledger.read()
    assert records[-1].event == "end"
    assert records[-1].outcome == "FAIL"
    assert records[-1].error_code == "RuntimeError"


def test_phase_record_serialization_is_stable(tmp_path: Path) -> None:
    ledger = PhaseLedger(tmp_path / "phases.jsonl", execution_id="run-1")
    ledger.start("V001", BenchmarkPhase.PREPARE_ENVIRONMENT)
    first = (tmp_path / "phases.jsonl").read_text(encoding="utf-8")
    payload = json.loads(first)
    assert payload["phase"] == "PREPARE_ENVIRONMENT"
    assert payload["event"] == "start"
