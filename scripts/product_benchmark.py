#!/usr/bin/env python3
"""Product-resolution benchmark: each scenario on a fresh cluster (M19)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from packages.evals.product.scenarios import scenarios


def _list_scenarios() -> int:
    registered = scenarios()
    for scenario in registered:
        proofs = ",".join(proof.value for proof in scenario.expectation.proofs) or "-"
        print(f"{scenario.scenario_id}  phases={len(scenario.phases)} proofs={proofs}")
    print(f"{len(registered)} product scenarios registered")
    return 0


def _dry_run(selected: Sequence[str]) -> int:
    by_id = {scenario.scenario_id: scenario for scenario in scenarios()}
    unknown = [item for item in selected if item not in by_id]
    if unknown:
        print(f"unknown product scenario: {', '.join(unknown)}", file=sys.stderr)
        return 2
    from packages.evals.product.runner import ProductRunner, RecordingBackend  # noqa: PLC0415

    worst = 0
    for scenario_id in selected:
        backend = RecordingBackend()
        result = ProductRunner(backend).run(by_id[scenario_id])
        print(f"{scenario_id}: {result.status.value} (dry-run)")
        for event in backend.events:
            print("  " + " ".join(str(part) for part in event))
        worst = max(worst, 0 if result.status.value == "RUN_OK" else 1)
    return worst


# Local ports of the live harness's port-forwards; checked free before a run.
PORTS = {
    "order-service": 18100,
    "payment-service": 18101,
    "control-plane": 18000,
    "prometheus": 19090,
    "postgres": 15432,
}


def _smoke(name: str, run_id: str | None) -> int:  # pragma: no cover - live Kind only
    import socket  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415

    from packages.evals.product import artifact, smoke  # noqa: PLC0415
    from packages.evals.product.live import (  # noqa: PLC0415
        LiveBackend,
        LiveClusterControl,
        LiveEvidenceReader,
    )
    from packages.evals.product.runner import ProductRunner  # noqa: PLC0415
    from packages.storage.database import create_session_factory  # noqa: PLC0415

    scenario = smoke.SMOKES.get(name)
    if scenario is None:
        print(f"unknown smoke: {name} (known: {', '.join(smoke.SMOKES)})", file=sys.stderr)
        return 2
    for port in PORTS.values():
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                print(f"local port {port} is already in use", file=sys.stderr)
                return 2
    root = Path(__file__).resolve().parents[1]
    started = datetime.now(UTC)
    run = run_id or artifact.run_id(started)
    # Harness sessions are read-only at the database: a write fails, not just a test.
    engine = create_engine(
        f"postgresql+psycopg://postgres:postgres@127.0.0.1:{PORTS['postgres']}/agentic_sre",
        connect_args={"options": "-c default_transaction_read_only=on"},
        pool_pre_ping=True,
    )
    factory = create_session_factory(engine)
    prometheus = f"http://127.0.0.1:{PORTS['prometheus']}"
    captured: dict[str, object] = {}

    def capture(backend: LiveBackend) -> None:
        captured["baseline"] = backend.baseline_document
        captured["evidence"] = smoke.capture_evidence(
            factory, backend.http, prometheus, since=started, until=datetime.now(UTC)
        )

    backend = LiveBackend(
        root=root,
        control_port=LiveClusterControl(
            {
                service: f"http://127.0.0.1:{PORTS[service]}"
                for service in ("order-service", "payment-service")
            }
        ),
        evidence_port=LiveEvidenceReader(factory),
        port_forwards={service: PORTS[service] for service in ("order-service", "payment-service")},
        control_plane_port=PORTS["control-plane"],
        postgres_port=PORTS["postgres"],
        prometheus_port=PORTS["prometheus"],
        run_id=run,
        before_teardown=capture,
    )
    result = ProductRunner(backend).run(scenario)
    evidence = captured.get("evidence")
    acceptance = smoke.accept(
        name, result, evidence if isinstance(evidence, dict) else None, backend.artifact_path
    )
    timeline = [
        {
            "offset": artifact.format_offset(item.offset),
            "action": artifact.describe_action(item.action),
            "at": item.started_at.isoformat(),
        }
        for item in result.timeline
    ]
    record = root / ".local" / "product-bench" / run / f"{name}.smoke.json"
    smoke.write_evidence(
        record, name, result, evidence if isinstance(evidence, dict) else None,
        acceptance, timeline,
        baseline if isinstance(baseline := captured.get("baseline"), dict) else None,
    )  # fmt: skip
    verdict = "ACCEPTED" if acceptance.accepted else "NOT ACCEPTED"
    print(f"{name}: {result.status.value}/{result.error_type} {verdict} -> {record}")
    for reason in acceptance.reasons:
        print(f"  - {reason}")
    return 0 if acceptance.accepted else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print the scenarios and exit")
    parser.add_argument("--scenario", action="append", default=[], help="scenario id (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="record the fresh-cluster lifecycle; no effects"
    )
    parser.add_argument(
        "--smoke", help="run one live Kind smoke (M19-6.12): smoke-noop|smoke-readiness|smoke-chain"
    )
    parser.add_argument("--run-id", help="shared run id (YYYYMMDDTHHMMSSffffffZ)")
    args = parser.parse_args(argv)
    if args.smoke:
        return _smoke(args.smoke, args.run_id)
    if args.list:
        return _list_scenarios()
    if args.dry_run:
        if not args.scenario:
            print("--dry-run needs at least one --scenario", file=sys.stderr)
            return 2
        return _dry_run(args.scenario)
    print(
        "live DEV scenario execution is not implemented yet (F7); live smokes use --smoke",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
