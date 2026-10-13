"""Bounded source correlation assertions from Loki JSON bodies/structured metadata.

Conflicting values are omitted, not chosen by precedence. No topology inference
or message-text identity extraction is performed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

FIELDS = (
    "request_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "dependency.name",
    "http.request.method",
    "server.address",
    "error.type",
    "http.response.status_code",
)


def _value(key: str, value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text or len(text) > 256:
        return None
    if key in {"trace_id", "span_id", "parent_span_id"}:
        width = 32 if key == "trace_id" else 16
        if not re.fullmatch(rf"[0-9a-fA-F]{{{width}}}", text) or int(text, 16) == 0:
            return None
        return text.lower()
    return text


def source_log_attributes(
    message: str, metadata: Any = None, stream_metadata: Any = None
) -> dict[str, str]:
    body: Any = {}
    if len(message) <= 100_000:
        try:
            body = json.loads(message)
        except (ValueError, RecursionError):
            pass
    sources = [item for item in (body, metadata, stream_metadata) if isinstance(item, Mapping)]
    result = {}
    for key in FIELDS:
        values: set[str] = set()
        invalid = False
        for source in sources:
            for alias in {key, key.replace(".", "_")}:
                if alias not in source or source[alias] is None:
                    continue
                value = _value(key, source[alias])
                if value is None:
                    invalid = True
                else:
                    values.add(value)
        if not invalid and len(values) == 1:
            result[key] = values.pop()
    return result
