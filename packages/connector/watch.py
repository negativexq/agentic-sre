"""Watch primitives of the change stream (connector contract §15).

A cluster that can watch returns each scope's ``resourceVersion`` with its LIST
(``ObjectListing.resource_versions``) and yields ``WatchEvent``s from ``watch(scope, version)``; the iterator
ends when the server closes the watch, and ``ResourceVersionExpired`` means the version can no longer be
resumed (``410 Gone``), so continuity is lost. Defined with the cluster reader in ``packages.rca.live``.
"""

from packages.rca.live import ResourceVersionExpired, WatchEvent, WatchEventType

__all__ = ["ResourceVersionExpired", "WatchEvent", "WatchEventType"]
