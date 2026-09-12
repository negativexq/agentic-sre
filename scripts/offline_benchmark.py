"""Run the frozen v0.2.0 benchmark without any live model calls."""

from packages.evals import run_offline_benchmark


def main() -> None:
    """Print the JSON benchmark report without raw model or telemetry data."""
    print(run_offline_benchmark().model_dump_json(indent=2))


if __name__ == "__main__":
    main()
