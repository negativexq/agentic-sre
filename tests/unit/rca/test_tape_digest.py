"""Canonical provider tape digest and persisted run-scoped reload tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
from provider_test_helpers import provider_session_factory

from packages.rca.tape import TapeDigestEntry, compute_tape_digest
from packages.storage.models import InvestigationReadRow
from packages.storage.repositories import InvestigationReadRepository
from packages.storage.tape import load_tape_digest

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _entry(
    sequence: int = 1,
    caller_class: str = "ENGINE",
    query_key: str = "same-query",
    status: str = "SUCCESS",
    evidence_ids: tuple[str, ...] = ("evidence:a",),
) -> TapeDigestEntry:
    return TapeDigestEntry(sequence, caller_class, query_key, status, evidence_ids)


def _row(
    run_id: str,
    sequence: int,
    *,
    caller_class: str = "ENGINE",
    query_key: str = "same-query",
    status: str = "SUCCESS",
    evidence_ids: list[str] | None = None,
    capability: str = "resource_pressure",
    descriptor: dict[str, Any] | None = None,
    observation: Any = None,
    started_at: datetime = AT,
    finished_at: datetime = AT + timedelta(seconds=1),
    committed_at: datetime | None = AT + timedelta(seconds=2),
    error_type: str | None = None,
    error_message: str | None = None,
) -> InvestigationReadRow:
    return InvestigationReadRow(
        run_id=run_id,
        sequence=sequence,
        caller_class=caller_class,
        capability=capability,
        query_key=query_key,
        query_descriptor=descriptor or {"query": "same"},
        started_at=started_at,
        finished_at=finished_at,
        committed_at=committed_at,
        status=status,
        observation=observation,
        evidence_ids=evidence_ids if evidence_ids is not None else ["evidence:a"],
        error_type=error_type,
        error_message=error_message,
    )


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        (_entry(), _entry(sequence=2)),
        (_entry(), _entry(caller_class="INVESTIGATION")),
        (_entry(), _entry(query_key="another-query")),
        (_entry(), _entry(status="ERROR")),
        (_entry(), _entry(evidence_ids=("evidence:a", "evidence:b"))),
    ],
    ids=("sequence", "caller-class", "query-key", "status", "evidence-membership"),
)
def test_each_included_field_changes_digest(
    original: TapeDigestEntry, changed: TapeDigestEntry
) -> None:
    assert compute_tape_digest([original]) != compute_tape_digest([changed])


def test_identical_tapes_are_deterministic_and_use_canonical_json() -> None:
    entries = [_entry(2, query_key="repeat"), _entry(1, query_key="repeat")]
    expected_payload = [
        [1, "ENGINE", "repeat", "SUCCESS", ["evidence:a"]],
        [2, "ENGINE", "repeat", "SUCCESS", ["evidence:a"]],
    ]
    import json

    expected_canonical = json.dumps(
        expected_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = compute_tape_digest(entries)
    assert digest == compute_tape_digest(list(entries))
    assert digest == sha256(expected_canonical.encode("utf-8")).hexdigest()


def test_sequence_is_authoritative_but_input_order_is_irrelevant() -> None:
    first = _entry(sequence=1, query_key="first")
    second = _entry(sequence=2, query_key="second")
    baseline = compute_tape_digest([first, second])
    assert compute_tape_digest([second, first]) == baseline
    assert (
        compute_tape_digest([replace(first, sequence=2), replace(second, sequence=1)]) != baseline
    )


def test_evidence_id_order_is_canonical_without_deduplication() -> None:
    forward = _entry(evidence_ids=("a", "b", "a"))
    reverse = _entry(evidence_ids=("a", "a", "b"))
    assert compute_tape_digest([forward]) == compute_tape_digest([reverse])
    assert compute_tape_digest([forward]) != compute_tape_digest([_entry(evidence_ids=("a", "b"))])


def test_repeated_query_key_rows_remain_distinct() -> None:
    repeated = [_entry(1), _entry(2), _entry(3, status="ERROR")]
    assert compute_tape_digest(repeated) != compute_tape_digest(repeated[:2])


def test_empty_tape_has_deterministic_sha256_of_empty_json_array() -> None:
    expected = sha256(b"[]").hexdigest()
    assert compute_tape_digest([]) == expected
    assert compute_tape_digest([]) == compute_tape_digest(())
    assert len(expected) == 64
    factory = provider_session_factory()
    with factory() as session:
        assert load_tape_digest(session, "empty-run") == expected


def test_persisted_reload_orders_by_sequence_and_ignores_insert_order() -> None:
    factory = provider_session_factory()
    sequence_one = _row("run-a", 1, query_key="first", evidence_ids=["b", "a"])
    sequence_two = _row("run-a", 2, query_key="second", status="ERROR", evidence_ids=[])
    with factory() as session:
        session.add_all([sequence_two, sequence_one])
        session.commit()
    with factory() as session:
        rows = InvestigationReadRepository(session).list_for_run("run-a")
        assert [row.sequence for row in rows] == [1, 2]
        expected = compute_tape_digest(
            TapeDigestEntry(
                row.sequence,
                row.caller_class,
                row.query_key,
                row.status,
                tuple(row.evidence_ids),
            )
            for row in rows
        )
        persisted_digest = load_tape_digest(session, "run-a")
        assert persisted_digest == expected
        assert len(InvestigationReadRepository(session).list_for_run("run-a")) == 2


def test_excluded_persisted_fields_do_not_change_digest() -> None:
    factory = provider_session_factory()
    with factory() as session:
        session.add(
            _row(
                "run-a",
                1,
                status="ERROR",
                capability="resource_pressure",
                descriptor={"query": "old"},
                observation={"old": [1, 2]},
                started_at=AT,
                finished_at=AT + timedelta(seconds=1),
                committed_at=AT + timedelta(seconds=2),
                error_type="TimeoutError",
                error_message="old error",
            )
        )
        session.add(
            _row(
                "run-b",
                1,
                status="ERROR",
                capability="tempo_traces",
                descriptor={"query": "different"},
                observation={"new": [9]},
                started_at=AT + timedelta(days=1),
                finished_at=AT + timedelta(days=2),
                committed_at=AT + timedelta(days=3),
                error_type="ConnectionError",
                error_message="different error",
            )
        )
        session.commit()
    with factory() as session:
        assert load_tape_digest(session, "run-a") == load_tape_digest(session, "run-b")
        row_a = InvestigationReadRepository(session).list_for_run("run-a")[0]
        row_b = InvestigationReadRepository(session).list_for_run("run-b")[0]
        assert row_a.read_id != row_b.read_id
        assert row_a.capability != row_b.capability
        assert row_a.query_descriptor != row_b.query_descriptor
        assert row_a.observation != row_b.observation
        assert (row_a.started_at, row_a.finished_at, row_a.committed_at) != (
            row_b.started_at,
            row_b.finished_at,
            row_b.committed_at,
        )
        assert row_a.error_type != row_b.error_type
        assert row_a.error_message != row_b.error_message


def test_other_runs_are_ignored_and_run_id_is_not_hashed() -> None:
    factory = provider_session_factory()
    with factory() as session:
        session.add_all([_row("run-a", 1), _row("run-b", 1)])
        session.commit()
    with factory() as session:
        digest_a = load_tape_digest(session, "run-a")
        digest_b = load_tape_digest(session, "run-b")
        assert digest_a == digest_b
        session.add(_row("run-b", 2, query_key="unrelated", status="ERROR"))
        session.commit()
    with factory() as session:
        assert load_tape_digest(session, "run-a") == digest_a
        assert load_tape_digest(session, "run-b") != digest_b
