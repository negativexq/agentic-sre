"""Only bounded explicit assertions survive; no inferred target or precedence."""

import json

from packages.rca.log_attributes import source_log_attributes


def test_otlp_metadata_and_json_body_keep_the_same_correlation() -> None:
    assert source_log_attributes(
        "timeout",
        {
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "server_address": "payment",
            "error_type": "TimeoutError",
        },
    ) == {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "server.address": "payment",
        "error.type": "TimeoutError",
    }
    assert source_log_attributes(
        json.dumps({"request_id": "req", "parent_span_id": "c" * 16}), {"request_id": "req"}
    ) == {"request_id": "req", "parent_span_id": "c" * 16}


def test_conflicts_invalid_ids_and_oversized_fields_do_not_bind() -> None:
    result = source_log_attributes(
        json.dumps(
            {
                "request_id": "a",
                "trace_id": "0" * 32,
                "span_id": "bad",
                "target_uid": "invented",
                "error.type": "x" * 257,
            }
        ),
        {"request_id": "b"},
    )
    assert result == {}
    assert source_log_attributes("{}", {"trace_id": "a" * 32, "span_id": True}) == {
        "trace_id": "a" * 32
    }


def test_legacy_plain_text_and_unstructured_json_stay_unbound() -> None:
    assert source_log_attributes("payment-service timeout request_id=req") == {}
    assert source_log_attributes('["timeout"]') == {}
    assert source_log_attributes("{broken") == {}
