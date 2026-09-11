"""Container entrypoint for the Kafka order worker."""

from workload.order_worker.worker import build_default_worker


def main() -> None:
    """Run the worker against the configured Kafka topic."""
    worker = build_default_worker(lambda _: None)
    worker.run_forever("orders.created")


if __name__ == "__main__":
    main()
