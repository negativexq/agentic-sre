"""Typed access helpers for untyped Kubernetes JSON."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, else an empty dict."""
    return value if isinstance(value, dict) else {}


def child(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return the JSON object at ``key``, else an empty dict."""
    return mapping(value.get(key))


__all__ = ["child", "mapping"]
