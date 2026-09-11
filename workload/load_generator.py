"""Seeded, bounded HTTP workload generator."""

import argparse
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from urllib.request import Request, urlopen

from workload.common.contracts import OrderCreateRequest, OrderResponse
from workload.seed import generate_orders


@dataclass(frozen=True, slots=True)
class LoadConfig:
    """Load generator parameters."""

    rate: int
    duration_seconds: int
    seed: int = 42
    concurrency: int = 1

    def __post_init__(self) -> None:
        if self.rate < 0 or self.duration_seconds < 0 or self.concurrency < 1:
            raise ValueError("rate and duration must be non-negative; concurrency must be positive")


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Stable aggregate load result."""

    sent: int
    success: int
    failed: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def run_load(
    config: LoadConfig,
    submit: Callable[[OrderCreateRequest], OrderResponse],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> LoadResult:
    """Submit a seeded request stream and report latency percentiles."""
    total = config.rate * config.duration_seconds
    requests = generate_orders(total, seed=config.seed)
    latencies: list[float] = []
    success = 0
    failed = 0
    interval = 1 / config.rate if config.rate else 0
    for request in requests:
        started = perf_counter()
        try:
            submit(request)
        except Exception:
            failed += 1
        else:
            success += 1
            latencies.append((perf_counter() - started) * 1000)
        if interval:
            sleep(interval)
    return LoadResult(
        sent=total,
        success=success,
        failed=failed,
        p50_ms=_percentile(latencies, 50),
        p95_ms=_percentile(latencies, 95),
        p99_ms=_percentile(latencies, 99),
    )


def http_submitter(
    base_url: str, *, timeout_seconds: float = 5.0
) -> Callable[[OrderCreateRequest], OrderResponse]:
    """Create a standard-library HTTP submitter for order-service."""

    def submit(request: OrderCreateRequest) -> OrderResponse:
        http_request = Request(
            f"{base_url.rstrip('/')}/orders",
            data=request.model_dump_json().encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(http_request, timeout=timeout_seconds) as response:
            return OrderResponse.model_validate_json(response.read())

    return submit


def main(argv: Sequence[str] | None = None) -> None:
    """Run the load generator from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--duration", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args(argv)
    result = run_load(
        LoadConfig(args.rate, args.duration, args.seed, args.concurrency),
        http_submitter(args.base_url),
    )
    print(result)


if __name__ == "__main__":
    main()
