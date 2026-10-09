"""The lab's isolation check: which chaos events betray a fault this run did not create."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from packages.evals.live import testbed_lab
from packages.evals.live.testbed_lab import (
    LabWorld,
    code_identity,
    foreign_fault_event,
    tree_problems,
)


def test_an_experiment_of_another_run_is_foreign() -> None:
    assert foreign_fault_event("NetworkChaos", "diag-stress", {"dep-delay-11"})
    assert foreign_fault_event("Schedule", "nightly", {"dep-delay-11"})


def test_the_runs_own_experiment_is_not_foreign() -> None:
    assert not foreign_fault_event("NetworkChaos", "dep-delay-11", {"dep-delay-11"})


def test_chaos_meshs_per_pod_record_of_the_runs_experiment_is_not_foreign() -> None:
    """``PodNetworkChaos`` is named after the target pod, not after the experiment it applies."""
    assert not foreign_fault_event(
        "PodNetworkChaos", "payment-service-7fc956765b-t57g8", {"dep-delay-11"}
    )


def test_ordinary_objects_are_never_faults() -> None:
    assert not foreign_fault_event("Pod", "payment-service-7fc956765b-t57g8", set())


def test_an_experiment_spawned_by_the_runs_schedule_is_not_foreign() -> None:
    assert not foreign_fault_event("NetworkChaos", "sched-delay-41-x7k2p", {"sched-delay-41"})
    assert foreign_fault_event("NetworkChaos", "sched-delay-4-x7k2p", {"sched-delay-41"})
    assert foreign_fault_event("NetworkChaos", "other-x7k2p", {"sched-delay-41"})


# ---- design §31.2 B, C: the harness owns its forwards and runs the tree it checks ------------------


class _Recorded(LabWorld):
    """A lab whose commands are recorded, never run."""

    def __init__(self, tmp_path: Path, answers: dict[str, str] | None = None, **kwargs: Any):
        super().__init__(forwards_file=tmp_path / "forwards.json", **kwargs)
        self.commands: list[list[str]] = []
        self.answers = answers or {}

    def _run(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        out = next((v for k, v in self.answers.items() if k in " ".join(args)), "")
        return subprocess.CompletedProcess(list(args), 0, out, "")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fake_forward(port: int) -> subprocess.Popen[bytes]:
    """A process whose command line reads like the testbed's forward (``ps`` sees the arguments)."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "kubectl",
            "port-forward",
            f"{port}:8000",
        ]
    )


def test_a_forward_recorded_by_an_ended_run_is_stopped_before_isolation(tmp_path: Path) -> None:
    port = _free_port()
    orphan = _fake_forward(port)
    try:
        time.sleep(0.3)
        world = _Recorded(tmp_path, order_port=port, alertmanager_port=_free_port())
        world.forwards_file.write_text(
            json.dumps([{"service": "order-service", "pid": orphan.pid, "port": port}])
        )
        assert world._end_recorded_forwards() == [orphan.pid]
        assert orphan.wait(timeout=5) is not None
        assert not world.forwards_file.exists()
    finally:
        orphan.kill()


def test_a_recorded_pid_that_is_no_longer_a_forward_is_left_alone(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        world = _Recorded(tmp_path)
        world.forwards_file.write_text(
            json.dumps([{"service": "order-service", "pid": other.pid, "port": 18000}])
        )
        assert world._end_recorded_forwards() == []
        assert other.poll() is None
    finally:
        other.kill()


def test_an_unknown_process_on_a_testbed_port_refuses_the_run_before_isolation(
    tmp_path: Path,
) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = int(holder.getsockname()[1])
        world = _Recorded(tmp_path, order_port=port, alertmanager_port=_free_port())
        with pytest.raises(RuntimeError, match=f"did not start: \\[{port}\\]"):
            world.isolate("s1-x-0")
    assert world.commands == []  # nothing in the lab was touched


def test_set_aside_stops_the_control_plane_and_renames_the_database(tmp_path: Path) -> None:
    world = _Recorded(tmp_path, answers={"select 1 from pg_database": "1"})
    renamed = world.set_aside("s1-x-0")
    assert renamed is not None and renamed.startswith("aborted_")
    assert ["make", "cp-stop"] in world.commands
    assert world.commands[-1][-1] == f'alter database "testbed_s1_x_0" rename to "{renamed}"'


def test_set_aside_without_a_database_changes_nothing(tmp_path: Path) -> None:
    world = _Recorded(tmp_path)
    assert world.set_aside("s1-x-0") is None
    assert len(world.commands) == 1  # only the lookup


def test_a_run_tree_without_its_interpreter_or_lab_is_refused(tmp_path: Path) -> None:
    assert tree_problems(tmp_path) == [
        f"{tmp_path / '.venv'} is missing",
        f"{tmp_path / '.local/lab'} is missing",
    ]
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "lab").symlink_to(tmp_path / ".venv")
    assert tree_problems(tmp_path) == []


def test_the_control_plane_imports_the_tree_make_runs_in() -> None:
    makefile = (testbed_lab.REPO / "Makefile").read_text()
    assert "PYTHONPATH=$(CURDIR) nohup $(CLI) serve" in makefile


def test_the_code_identity_says_whether_tracked_files_changed(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a")
    (tmp_path / ".venv").mkdir()  # untracked links to the main tree do not count
    clean = code_identity(tmp_path)
    assert clean["dirty"] == "false" and len(clean["commit"]) == 40
    (tmp_path / "a.py").write_text("x = 2\n")
    assert code_identity(tmp_path)["dirty"] == "true"
