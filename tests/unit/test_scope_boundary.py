"""Guardrails for the credit-aware single-agent release boundary."""

import tomllib
from pathlib import Path

from packages.investigation.registry import live_observability_registry

FORBIDDEN_PACKAGES = {
    "agent-framework",
    "agno",
    "anthropic",
    "autogen",
    "chromadb",
    "crewai",
    "langchain",
    "langgraph",
    "llama-index",
    "mcp",
    "qdrant-client",
    "sentence-transformers",
    "transformers",
}


def test_release_has_no_agent_orchestration_dependencies() -> None:
    """The baseline may use one guarded provider but no agent framework."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    dependencies.extend(project["project"]["optional-dependencies"]["dev"])

    package_names = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for dependency in dependencies
    }

    assert package_names.isdisjoint(FORBIDDEN_PACKAGES)


def test_benchmark_state_preparation_is_not_model_accessible() -> None:
    registry = live_observability_registry(
        "http://prometheus",
        "http://loki",
        "http://tempo",
    )

    assert "/api/v1/benchmark/state/prepare" not in registry.names()
    assert all("benchmark" not in name.lower() for name in registry.names())
