"""Process-local memory ballast for the ``memory_ballast_mb`` test fault (M19-6.2)."""

from collections.abc import Callable

MIB = 1024 * 1024


class MemoryBallast:
    """Holds exactly the requested MiB of memory until resized or released.

    One instance per application. A resize allocates the new buffer before
    dropping the old one, so a failed allocation leaves the previous ballast
    and its size in place; resizing to the current size allocates nothing.
    """

    def __init__(self, allocate: Callable[[int], bytearray] = bytearray) -> None:
        self.allocate = allocate
        self.buffer: bytearray | None = None

    @property
    def size_mb(self) -> int:
        return len(self.buffer) // MIB if self.buffer is not None else 0

    def resize(self, size_mb: int) -> None:
        if size_mb == self.size_mb:
            return
        self.buffer = self.allocate(size_mb * MIB) if size_mb else None
