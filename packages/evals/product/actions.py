"""Scenario actions of the product-resolution harness.

M19-6.5 declares only the marker every action derives from. The concrete
actions and their ``apply``/``verify`` behavior belong to M19-6.6.
"""

from __future__ import annotations


class ProductAction:
    """Marker base class for a staged scenario action; it has no behavior yet."""

    __slots__ = ()


__all__ = ["ProductAction"]
