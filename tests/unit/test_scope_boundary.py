"""Guardrails for the deterministic-first release boundary."""

import tomllib
from pathlib import Path

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
    "openai",
    "qdrant-client",
    "sentence-transformers",
    "transformers",
}


def test_deterministic_release_has_no_agent_or_model_dependencies() -> None:
    """The foundation cannot silently acquire probabilistic runtime dependencies."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    dependencies.extend(project["project"]["optional-dependencies"]["dev"])

    package_names = {
        dependency.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for dependency in dependencies
    }

    assert package_names.isdisjoint(FORBIDDEN_PACKAGES)
