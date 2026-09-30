"""Watch primitives of the change stream (connector contract §15).

A cluster that can watch returns each scope's ``resourceVersion`` with its LIST
(``ObjectListing.resource_versions``) and yields ``WatchEvent``s from ``watch(scope, version)``; the iterator
ends when the server closes the watch, and ``ResourceVersionExpired`` means the version can no longer be
resumed (``410 Gone``), so continuity is lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WatchEventType = Literal["ADDED", "MODIFIED", "DELETED", "BOOKMARK"]


@dataclass(frozen=True)
class WatchEvent:
    type: WatchEventType
    body: dict[str, Any]
    resource_version: str


class ResourceVersionExpired(Exception):
    """The watch cannot resume from its version: continuity is lost (``410 Gone``)."""
