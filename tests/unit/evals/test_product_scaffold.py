"""M19-6.5: product-resolution harness scaffold types, registry and offline listing."""

from __future__ import annotations

import dataclasses
import importlib
import json
import socket
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.evals.product import Expectation, Phase, ProductAction, ProductScenario, ProofId
from packages.evals.product.scenarios import SCENARIOS, scenarios, validate_registry
from packages.evals.product.timeline import action_sequence

ROOT = Path(__file__).resolve().parents[3]
MODULES = ("actions", "artifact", "proof", "runner", "scenarios", "spec", "timeline")


class Marker(ProductAction):
    """A test-only action: the concrete DSL is M19-6.6."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _scenario(scenario_id: str = "scaffold") -> ProductScenario:
    return ProductScenario(
        scenario_id=scenario_id,
        phases=(
            Phase(timedelta(minutes=-25), (Marker("a"), Marker("b"))),
            Phase(timedelta(minutes=-10), (Marker("c"),)),
            Phase(timedelta(0), (Marker("d"),)),
        ),
        expectation=Expectation((ProofId.T1, ProofId.T4, ProofId.N0)),
    )


@pytest.mark.parametrize("name", MODULES)
def test_every_product_module_imports(name: str) -> None:
    importlib.import_module(f"packages.evals.product.{name}")


def test_core_types_construct_with_minimal_values() -> None:
    empty = ProductScenario(scenario_id="noop", phases=(), expectation=Expectation())
    assert empty.phases == () and empty.expectation.proofs == ()
    assert Phase(timedelta(seconds=30), ()).offset == timedelta(seconds=30)
    assert [proof.value for proof in ProofId] == ["T1", "T2", "T3", "T4", "N0", "N1", "N2", "N3"]


def test_phase_and_action_order_is_preserved() -> None:
    scenario = _scenario()
    assert [phase.offset for phase in scenario.phases] == [
        timedelta(minutes=-25),
        timedelta(minutes=-10),
        timedelta(0),
    ]
    sequence = action_sequence(scenario)
    assert [(offset, cast_name(action)) for offset, action in sequence] == [
        (timedelta(minutes=-25), "a"),
        (timedelta(minutes=-25), "b"),
        (timedelta(minutes=-10), "c"),
        (timedelta(0), "d"),
    ]


def cast_name(action: ProductAction) -> str:
    assert isinstance(action, Marker)
    return action.name


@pytest.mark.parametrize(
    "build",
    [
        lambda: Phase(30, ()),  # type: ignore[arg-type]
        lambda: Phase(timedelta(0), [Marker("a")]),  # type: ignore[arg-type]
        lambda: Phase(timedelta(0), ("not an action",)),  # type: ignore[arg-type]
        lambda: Expectation(("T1",)),  # type: ignore[arg-type]
        lambda: Expectation((ProofId.T1, ProofId.T1)),
        lambda: ProductScenario(scenario_id=" ", phases=(), expectation=Expectation()),
        lambda: ProductScenario(
            scenario_id="x",
            phases=(Phase(timedelta(0), ()), Phase(timedelta(0), ())),
            expectation=Expectation(),
        ),
        lambda: ProductScenario(
            scenario_id="x",
            phases=(Phase(timedelta(minutes=1), ()), Phase(timedelta(0), ())),
            expectation=Expectation(),
        ),
    ],
)
def test_invalid_definitions_fail_loudly(build: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        build()


def test_definitions_are_frozen() -> None:
    scenario = _scenario()
    with pytest.raises(dataclasses.FrozenInstanceError):
        scenario.scenario_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        scenario.phases[0].offset = timedelta(0)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        scenario.expectation.proofs = ()  # type: ignore[misc]


def test_registry_is_explicit_ordered_and_rejects_duplicate_ids() -> None:
    assert scenarios() == scenarios() == SCENARIOS
    assert isinstance(SCENARIOS, tuple)
    first, second = _scenario("a"), _scenario("b")
    assert validate_registry([first, second]) == (first, second)
    with pytest.raises(ValueError, match="duplicate product scenario id: a"):
        validate_registry([first, second, _scenario("a")])


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/product_benchmark.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_list_is_deterministic_and_exits_zero() -> None:
    first, second = _cli("--list"), _cli("--list")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout == f"{len(SCENARIOS)} product scenarios registered\n"
    assert first.stderr == ""


def test_execution_is_not_faked() -> None:
    result = _cli()
    assert result.returncode == 2
    assert "not implemented yet" in result.stderr and result.stdout == ""


def test_list_touches_no_external_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    def bomb(*_: Any, **__: Any) -> Any:
        raise AssertionError("--list reached an external boundary")

    monkeypatch.setattr(subprocess, "run", bomb)
    monkeypatch.setattr(subprocess, "Popen", bomb)
    monkeypatch.setattr(socket.socket, "connect", bomb)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        cli = importlib.import_module("product_benchmark")
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    assert cli.main(["--list"]) == 0


def test_list_loads_no_legacy_cluster_storage_or_model_code() -> None:
    probe = (
        "import json, sys, runpy\n"
        "sys.argv = ['product_benchmark.py', '--list']\n"
        "try:\n"
        "    runpy.run_path('scripts/product_benchmark.py', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout.splitlines()[-1])
    forbidden = (
        "packages.evals.live",
        "packages.storage",
        "packages.rca",
        "apps.",
        "kubernetes",
        "sqlalchemy",
        "openai",
        "httpx",
    )
    assert [name for name in loaded if name.startswith(forbidden)] == []


def test_make_target_is_offline_and_uses_the_product_entry_point() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("\nproduct-bench-dev:\n", 1)[1].split("\n\n", 1)[0]
    assert block.strip() == "$(PRODUCT) --list"
    assert "PRODUCT := $(PY) scripts/product_benchmark.py" in makefile
    for word in ("live", "cluster", "kubectl", "kind", "docker", "deploy", "truncate", "LIVE"):
        assert word not in block
