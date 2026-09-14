"""Supervise local Kubernetes port-forwards used by benchmark commands.

Port-forwards attach to a selected pod even when started against a Service.
This small wrapper reconnects them after pod replacement and gates the child
command on real endpoint readiness. It is infrastructure-only and is never
available through the investigation tool registry.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


@dataclass(frozen=True, slots=True)
class ForwardSpec:
    name: str
    namespace: str
    service: str
    local_port: int
    remote_port: int
    readiness_url: str


A1_FORWARDS = (
    ForwardSpec(
        "prometheus", "observability", "prometheus", 19090, 9090, "http://127.0.0.1:19090/-/ready"
    ),
    ForwardSpec(
        "alertmanager",
        "observability",
        "alertmanager",
        19093,
        9093,
        "http://127.0.0.1:19093/-/ready",
    ),
    ForwardSpec("loki", "observability", "loki", 19300, 3100, "http://127.0.0.1:19300/ready"),
    ForwardSpec("tempo", "observability", "tempo", 19320, 3200, "http://127.0.0.1:19320/ready"),
    ForwardSpec(
        "control-plane",
        "sre-demo",
        "control-plane",
        18081,
        8000,
        "http://127.0.0.1:18081/api/v1/incidents",
    ),
    ForwardSpec(
        "order-service", "sre-demo", "order-service", 18000, 8000, "http://127.0.0.1:18000/health"
    ),
    ForwardSpec(
        "payment-service",
        "sre-demo",
        "payment-service",
        18001,
        8000,
        "http://127.0.0.1:18001/health",
    ),
)


def _ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=2):
            return True
    except (OSError, URLError, TimeoutError):
        return False


class ForwardSupervisor:
    def __init__(self, specs: tuple[ForwardSpec, ...], *, deadline_seconds: float = 120) -> None:
        self.specs = specs
        self.deadline_seconds = deadline_seconds
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.restart_counts = dict.fromkeys((spec.name for spec in specs), 0)
        self.last_exit_codes: dict[str, int | None] = {}
        self.readiness_failures = dict.fromkeys((spec.name for spec in specs), 0)
        self._stopping = False

    def _start(self, spec: ForwardSpec) -> None:
        log_path = Path(os.getenv("SRE_FORWARD_LOG_DIR", "/tmp")) / (
            f"agentic-sre-{spec.name}-forward.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = log_path.open("ab")
        process = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                "-n",
                spec.namespace,
                f"svc/{spec.service}",
                f"{spec.local_port}:{spec.remote_port}",
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        # The child inherits the descriptor; the supervisor closes its copy.
        stream.close()
        self.processes[spec.name] = process

    def _restart_dead(self, spec: ForwardSpec) -> None:
        process = self.processes.get(spec.name)
        if process is not None and process.poll() is not None:
            self.last_exit_codes[spec.name] = process.returncode
            self.restart_counts[spec.name] += 1
            self._start(spec)

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.deadline_seconds
        pending = {spec.name: spec for spec in self.specs}
        while pending and time.monotonic() < deadline:
            for spec in self.specs:
                self._restart_dead(spec)
                if spec.name not in pending:
                    continue
                if _ready(spec.readiness_url):
                    pending.pop(spec.name)
                else:
                    self.readiness_failures[spec.name] += 1
            if pending:
                time.sleep(0.5)
        if pending:
            raise TimeoutError("local forwarding readiness timed out: " + ",".join(sorted(pending)))

    def diagnostics(self) -> dict[str, object]:
        return {
            "forwards": [
                {
                    "service": spec.service,
                    "namespace": spec.namespace,
                    "local_port": spec.local_port,
                    "remote_port": spec.remote_port,
                    "readiness_url": spec.readiness_url,
                    "restart_count": self.restart_counts[spec.name],
                    "last_exit_code": self.last_exit_codes.get(spec.name),
                    "readiness_failures": self.readiness_failures[spec.name],
                }
                for spec in self.specs
            ]
        }

    def _stop_all(self) -> None:
        self._stopping = True
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def run(self, command: list[str]) -> int:
        for spec in self.specs:
            self._start(spec)
        child: subprocess.Popen[bytes] | None = None
        previous_handlers = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }

        def stop_handler(signum: int, _frame: object) -> None:
            if child is not None and child.poll() is None:
                child.terminate()
            self._stop_all()
            raise KeyboardInterrupt(signum)

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        status = 1
        diagnostics_path = os.getenv("SRE_FORWARD_DIAGNOSTICS")
        try:
            self.wait_until_ready()
            child = subprocess.Popen(command)
            while child.poll() is None:
                for spec in self.specs:
                    self._restart_dead(spec)
                time.sleep(0.25)
            status = int(child.returncode or 0)
        except (KeyboardInterrupt, TimeoutError) as error:
            if isinstance(error, TimeoutError):
                print(str(error), file=sys.stderr)
            status = 1
        finally:
            self._stop_all()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if diagnostics_path:
                path = Path(diagnostics_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self.diagnostics(), sort_keys=True, indent=2) + "\n")
        return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("a1",), default="a1")
    parser.add_argument("--readiness-timeout", type=float, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a child command is required after --")
    specs = A1_FORWARDS if args.profile == "a1" else A1_FORWARDS
    return ForwardSupervisor(specs, deadline_seconds=args.readiness_timeout).run(command)


if __name__ == "__main__":
    raise SystemExit(main())
