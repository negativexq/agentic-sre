"""One structured, deterministic packer for model-facing E9 values."""

from __future__ import annotations

import json
from typing import Any


def bounded_pack(value: Any, limit: int = 5_500) -> Any:
    """Bound structured values without hiding partial JSON in a string."""
    if len(json.dumps(value, ensure_ascii=False, default=str)) <= limit:
        return value
    if isinstance(value, dict):
        priority = (
            "identity",
            "entity",
            "object_state",
            "configuration_dependencies",
            "critical_field",
            "ownership",
            "events",
            "edges",
            "data_quality",
        )
        keys = [key for key in priority if key in value] + [
            key for key in value if key not in priority
        ]
        packed: dict[str, Any] = {}
        for key in keys:
            child = bounded_pack(value[key], max(80, limit // max(2, len(keys))))
            candidate = {
                "items": {**packed, key: child},
                "source_key_count": len(value),
                "returned_key_count": len(packed) + 1,
                "section_truncated": True,
            }
            if len(json.dumps(candidate, ensure_ascii=False, default=str)) > limit:
                continue
            packed[key] = child
        result = {
            "items": packed,
            "source_key_count": len(value),
            "returned_key_count": len(packed),
            "section_truncated": True,
        }
        return (
            result
            if len(json.dumps(result, ensure_ascii=False, default=str)) <= limit
            else {
                "items": {},
                "source_key_count": len(value),
                "returned_key_count": 0,
                "section_truncated": True,
            }
        )
    if isinstance(value, list):
        packed_list: list[Any] = []
        for item in value:
            child = bounded_pack(item, max(80, limit // max(2, len(value))))
            candidate = {
                "items": [*packed_list, child],
                "source_count": len(value),
                "returned_count": len(packed_list) + 1,
                "section_truncated": True,
            }
            if len(json.dumps(candidate, ensure_ascii=False, default=str)) > limit:
                continue
            packed_list.append(child)
        result = {
            "items": packed_list,
            "source_count": len(value),
            "returned_count": len(packed_list),
            "section_truncated": True,
        }
        return (
            result
            if len(json.dumps(result, ensure_ascii=False, default=str)) <= limit
            else {
                "items": [],
                "source_count": len(value),
                "returned_count": 0,
                "section_truncated": True,
            }
        )
    return {"value": str(value)[: max(1, limit - 80)], "section_truncated": True}


__all__ = ["bounded_pack"]
