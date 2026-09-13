"""Offline tests for the A1 Phase 2 prompt and protocol boundary."""

from packages.investigation import (
    INVESTIGATOR_PROMPT_V3,
    INVESTIGATOR_PROMPT_V4,
    investigator_prompt_v3_hash,
    investigator_prompt_v4_hash,
)


def test_prompt_versions_keep_v3_immutable_and_v4_stable() -> None:
    """A1 uses a new stable prompt while preserving the v3 hash."""
    assert "The alert scope identifies" not in INVESTIGATOR_PROMPT_V3
    assert "The alert scope identifies" in INVESTIGATOR_PROMPT_V4
    assert (
        investigator_prompt_v3_hash()
        == "50681df77387cd707157b52c530e16e4c420bd6cf4a9a1d10d1dc62eb6216d29"
    )
    assert (
        investigator_prompt_v4_hash()
        == "9c21bd17b5965545aa61c57a5eea788715570b52187447edfa94a3a871a3be20"
    )
