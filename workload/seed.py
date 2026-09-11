"""Seeded deterministic workload data generation."""

import argparse
import json
import random
from collections.abc import Sequence

from workload.common.contracts import OrderCreateRequest


def generate_orders(count: int, *, seed: int = 42) -> list[OrderCreateRequest]:
    """Generate the same order request dataset for the same seed."""
    if count < 0:
        raise ValueError("count must be non-negative")
    generator = random.Random(seed)
    return [
        OrderCreateRequest(
            customer_id=f"customer-{index:04d}",
            amount_cents=generator.randrange(1_000, 100_000),
            currency="USD",
        )
        for index in range(count)
    ]


def main(argv: Sequence[str] | None = None) -> None:
    """Print a deterministic seed dataset as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            [order.model_dump(mode="json") for order in generate_orders(args.count, seed=args.seed)]
        )
    )


if __name__ == "__main__":
    main()
