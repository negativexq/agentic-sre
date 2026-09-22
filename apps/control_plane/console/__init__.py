"""The operator console API: UI-facing DTOs over the deterministic core."""

from apps.control_plane.console.router import create_console_router

__all__ = ["create_console_router"]
