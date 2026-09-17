"""Typed access helpers for untyped Kubernetes JSON."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a JSON object, else an empty dict."""
    return value if isinstance(value, dict) else {}


def child(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return the JSON object at ``key``, else an empty dict."""
    return mapping(value.get(key))


def object_content_hash(body: Mapping[str, Any]) -> str:
    """Hash of an object's desired state: everything except status and bookkeeping metadata."""
    content = {key: value for key, value in body.items() if key not in {"status", "metadata"}}
    metadata = mapping(body.get("metadata"))
    content["labels"] = metadata.get("labels")
    content["annotations"] = {
        key: value
        for key, value in mapping(metadata.get("annotations")).items()
        if key != "kubectl.kubernetes.io/last-applied-configuration"
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


__all__ = ["child", "mapping", "object_content_hash"]
