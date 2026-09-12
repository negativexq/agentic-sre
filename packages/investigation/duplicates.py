"""Canonical, provider-independent tool request identity and history."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from packages.investigation.contracts import ToolRepeatPolicy


@dataclass(frozen=True, slots=True)
class ToolRequestIdentity:
    """Stable identity for one effective read-only observation request."""

    tool_name: str
    canonical_arguments_json: str
    effective_query_scope: str

    @property
    def serialized(self) -> str:
        """Return the deterministic provider-independent comparison value."""
        return json.dumps(
            {
                "tool_name": self.tool_name,
                "canonical_arguments": json.loads(self.canonical_arguments_json),
                "effective_query_scope": self.effective_query_scope,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def identity_hash(self) -> str:
        """Return a safe identity digest for audit records."""
        return sha256(self.serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolObservationHistory:
    """Bounded reusable state for one previously observed request."""

    identity: ToolRequestIdentity
    repeat_policy: ToolRepeatPolicy
    turn: int
    status: str
    evidence_ids: tuple[str, ...]
    tool_call_id: str | None
    result_count: int


def make_tool_request_identity(
    tool_name: str,
    canonical_arguments: dict[str, Any],
    observation_window: dict[str, str],
) -> ToolRequestIdentity:
    """Build identity after strict runtime argument canonicalization."""
    return ToolRequestIdentity(
        tool_name=tool_name,
        canonical_arguments_json=json.dumps(
            canonical_arguments, sort_keys=True, separators=(",", ":"), default=str
        ),
        effective_query_scope=json.dumps(
            observation_window, sort_keys=True, separators=(",", ":"), default=str
        ),
    )
