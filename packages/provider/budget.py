"""Local guards for explicit live model call budgets."""

import os
from dataclasses import dataclass
from threading import Lock

from packages.provider.contracts import ProviderError, ProviderErrorCode


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Immutable view of a live model budget."""

    limit: int
    calls_used: int

    @property
    def calls_remaining(self) -> int:
        """Return the number of provider requests still permitted."""
        return self.limit - self.calls_used


class LiveModelBudget:
    """Thread-safe, fail-closed budget consumed before every live request."""

    def __init__(self, limit: int = 40) -> None:
        if limit <= 0:
            raise ValueError("live model budget must be positive")
        self._limit = limit
        self._calls_used = 0
        self._lock = Lock()

    @classmethod
    def from_environment(cls) -> "LiveModelBudget":
        """Build a budget from an environment variable without logging it."""
        raw_limit = os.getenv("SRE_LIVE_MODEL_CALL_BUDGET", "40")
        try:
            limit = int(raw_limit)
        except ValueError as error:
            raise ValueError("SRE_LIVE_MODEL_CALL_BUDGET must be an integer") from error
        return cls(limit)

    def snapshot(self) -> BudgetSnapshot:
        """Return a consistent usage snapshot."""
        with self._lock:
            return BudgetSnapshot(self._limit, self._calls_used)

    def ensure_capacity(self, count: int) -> BudgetSnapshot:
        """Fail before a run starts when its worst-case calls do not fit."""
        if count <= 0:
            raise ValueError("capacity requirement must be positive")
        with self._lock:
            if self._calls_used + count > self._limit:
                raise ProviderError(
                    ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED,
                    "live model budget cannot cover the requested worst-case run",
                )
            return BudgetSnapshot(self._limit, self._calls_used)

    def consume(self, count: int = 1) -> BudgetSnapshot:
        """Reserve calls or fail before any network request is made."""
        if count <= 0:
            raise ValueError("budget consumption must be positive")
        with self._lock:
            if self._calls_used + count > self._limit:
                raise ProviderError(
                    ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED,
                    "live model call budget exhausted",
                )
            self._calls_used += count
            return BudgetSnapshot(self._limit, self._calls_used)
