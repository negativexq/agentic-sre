"""Canonicalization for trace IDs accepted from Tempo-compatible APIs."""

from __future__ import annotations

import re

_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{1,32}$")


def normalize_trace_id(value: object) -> str:
    """Normalize a 128-bit hexadecimal trace ID to 32 lowercase characters.

    Tempo can omit leading zeroes when serializing a valid 128-bit ID.  Only
    one-to-32 hexadecimal characters are accepted; arbitrary lengths and
    non-hex values remain invalid.
    """
    if not isinstance(value, str) or not _TRACE_ID_RE.fullmatch(value):
        raise ValueError("trace_id must contain 1 to 32 hexadecimal characters")
    return value.lower().zfill(32)


__all__ = ["normalize_trace_id"]
