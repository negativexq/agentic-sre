"""Local guards for explicit live model call budgets."""

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
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

    def __init__(self, limit: int = 40, *, ledger_path: str | None = None) -> None:
        if limit <= 0:
            raise ValueError("live model budget must be positive")
        self._limit = limit
        self._calls_used = 0
        self._lock = Lock()
        self._ledger_path = Path(ledger_path) if ledger_path else None

    @classmethod
    def from_environment(cls, *, require_shared_ledger: bool = False) -> "LiveModelBudget":
        """Build a budget from an environment variable without logging it."""
        raw_limit = os.getenv("SRE_LIVE_MODEL_CALL_BUDGET", "40")
        try:
            limit = int(raw_limit)
        except ValueError as error:
            raise ValueError("SRE_LIVE_MODEL_CALL_BUDGET must be an integer") from error
        ledger_path = os.getenv("SRE_LIVE_MODEL_BUDGET_FILE")
        if require_shared_ledger and not ledger_path:
            raise ProviderError(
                ProviderErrorCode.LIVE_MODEL_BUDGET_LEDGER_REQUIRED,
                "a shared live model budget ledger is required",
            )
        return cls(limit, ledger_path=ledger_path)

    @property
    def shared_ledger_enabled(self) -> bool:
        """Return whether reservations are persisted across live processes."""
        return self._ledger_path is not None

    @property
    def ledger_path(self) -> str | None:
        """Return the configured ledger path without reading its contents."""
        return str(self._ledger_path) if self._ledger_path is not None else None

    def snapshot(self) -> BudgetSnapshot:
        """Return a consistent usage snapshot."""
        with self._lock:
            if self._ledger_path is not None:
                self._calls_used = self._read_ledger()
            return BudgetSnapshot(self._limit, self._calls_used)

    def ensure_capacity(self, count: int) -> BudgetSnapshot:
        """Fail before a run starts when its worst-case calls do not fit."""
        if count <= 0:
            raise ValueError("capacity requirement must be positive")
        with self._lock:
            if self._ledger_path is not None:
                self._calls_used = self._read_ledger()
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
            if self._ledger_path is not None:
                self._calls_used = self._consume_ledger(count)
                return BudgetSnapshot(self._limit, self._calls_used)
            if self._calls_used + count > self._limit:
                raise ProviderError(
                    ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED,
                    "live model call budget exhausted",
                )
            self._calls_used += count
            return BudgetSnapshot(self._limit, self._calls_used)

    @staticmethod
    def reconcile_ledger(
        ledger_path: str, *, expected_current: int, corrected_current: int
    ) -> None:
        """Atomically reconcile a known ledger without ever decrementing it."""
        if expected_current < 0 or corrected_current < expected_current:
            raise ValueError("ledger reconciliation would decrease or invalidate usage")
        ledger = Path(ledger_path)
        if not ledger.exists():
            raise ValueError("ledger reconciliation requires an existing ledger")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                current = LiveModelBudget._parse_ledger(handle.read())
                if current != expected_current:
                    raise ValueError("ledger reconciliation precondition failed")
                handle.seek(0)
                handle.truncate()
                json.dump({"calls_used": corrected_current}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def verify_ledger_delta(
        before: BudgetSnapshot, after: BudgetSnapshot, outbound_api_attempts: int
    ) -> None:
        """Fail closed when persisted usage differs from outbound attempts."""
        if after.calls_used - before.calls_used != outbound_api_attempts:
            raise ProviderError(
                ProviderErrorCode.LIVE_MODEL_BUDGET_LEDGER_MISMATCH,
                "shared ledger delta does not match outbound API attempts",
            )

    def _read_ledger(self) -> int:
        """Read the shared call counter under an advisory file lock."""
        assert self._ledger_path is not None
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_path.open("a+", encoding="utf-8") as ledger:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
            ledger.seek(0)
            content = ledger.read()
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)
        return self._parse_ledger(content)

    def _consume_ledger(self, count: int) -> int:
        """Atomically reserve calls in the shared ledger."""
        assert self._ledger_path is not None
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_path.open("a+", encoding="utf-8") as ledger:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
            ledger.seek(0)
            calls_used = self._parse_ledger(ledger.read())
            if calls_used + count > self._limit:
                fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)
                raise ProviderError(
                    ProviderErrorCode.LIVE_MODEL_BUDGET_EXHAUSTED,
                    "live model call budget exhausted",
                )
            calls_used += count
            ledger.seek(0)
            ledger.truncate()
            json.dump({"calls_used": calls_used}, ledger)
            ledger.flush()
            os.fsync(ledger.fileno())
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)
        return calls_used

    @staticmethod
    def _parse_ledger(content: str) -> int:
        """Validate a ledger payload without accepting arbitrary metadata."""
        if not content:
            return 0
        try:
            value = json.loads(content).get("calls_used", 0)
        except (AttributeError, json.JSONDecodeError) as error:
            raise ValueError("live model budget ledger is invalid") from error
        if not isinstance(value, int) or value < 0:
            raise ValueError("live model budget ledger is invalid")
        return value
