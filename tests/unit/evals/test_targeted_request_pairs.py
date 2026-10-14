"""A bounded log-ID read cannot substitute guesses or split request pairs."""

from scripts.rollout_resolution_dev_proof import source

from packages.evals.targeted_request_pairs import fetch_log_request_pairs
from packages.rca.model import TraceSpanObservation


def test_exact_pairs_retrieved_without_baseline_or_unrelated_spans() -> None:
    value = source()
    calls: list[str] = []

    def fetch(tid: str) -> tuple[TraceSpanObservation, ...]:
        calls.append(tid)
        return tuple(s for s in value.trace_items if s.trace_id == tid)

    result = fetch_log_request_pairs(value.error_items, fetch)
    assert len(result.spans) == 8
    assert len(calls) == len(set(calls)) == 4
    assert not result.deficits
    assert all(s.evidence_id.startswith("failed-") for s in result.spans)


def test_trace_and_pair_budgets_are_enforced_without_splitting_pairs() -> None:
    value = source()

    def fetch(tid: str) -> tuple[TraceSpanObservation, ...]:
        return tuple(s for s in value.trace_items if s.trace_id == tid)

    trace_bounded = fetch_log_request_pairs(value.error_items, fetch, max_trace_fetches=1)
    assert len(trace_bounded.attempted_trace_ids) == 1 and len(trace_bounded.spans) == 2
    assert len(trace_bounded.deficits) == 3
    odd_cap = fetch_log_request_pairs(value.error_items, fetch, max_selected_spans=3)
    assert len(odd_cap.spans) == 2 and len(odd_cap.attempted_trace_ids) == 1


def test_same_trace_fetched_once_and_duplicate_missing_or_conflicting_pairs_rejected() -> None:
    value = source()
    log = value.error_items[0]
    spans = tuple(s for s in value.trace_items if s.trace_id == log.attributes["trace_id"])
    duplicated = fetch_log_request_pairs([log, log], lambda tid: spans)
    assert len(duplicated.attempted_trace_ids) == 1 and len(duplicated.spans) == 2
    conflict = fetch_log_request_pairs([log], lambda tid: (*spans, spans[0]))
    assert not conflict.spans
    absent = fetch_log_request_pairs([log], lambda tid: ())
    assert not absent.spans


def test_invalid_identity_does_not_issue_a_fetch_and_failures_consume_budget() -> None:
    value = source()
    invalid = value.error_items[0].model_copy(update={"attributes": {"trace_id": "bad"}})
    calls: list[str] = []

    def fail(tid: str) -> tuple[TraceSpanObservation, ...]:
        calls.append(tid)
        raise RuntimeError("unavailable")

    result = fetch_log_request_pairs([invalid, *value.error_items], fail, max_trace_fetches=1)
    assert len(calls) == 1 and not result.spans
    assert any(reason == "EXPLICIT_TRACE_CLIENT_PARENT_ID_INVALID" for _, reason in result.deficits)
    assert any(reason == "TRACE_FETCH_BUDGET_EXHAUSTED" for _, reason in result.deficits)
