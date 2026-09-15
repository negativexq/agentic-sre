"""Deterministic, offline benchmark identity collection and validation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class E9IdentityMismatch(RuntimeError):
    """The executable benchmark bundle differs from its preregistration."""


def collect_e9_identity(root: Path, relevant_paths: tuple[str, ...]) -> dict[str, Any]:
    """Collect Git and content identity without contacting a provider."""
    head = _git(root, "rev-parse", "HEAD")
    dirty = _git(root, "status", "--porcelain", "--", *relevant_paths)
    files: dict[str, str] = {}
    for relative in relevant_paths:
        path = root / relative
        if path.is_file():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    bundle = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "git_head": head,
        "relevant_paths": list(relevant_paths),
        "relevant_content_sha256": files,
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        "relevant_worktree_dirty": bool(dirty),
    }


def validate_e9_identity(actual: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Fail closed when the executable identity differs from preregistration."""
    expected = manifest.get("runtime_identity")
    if not isinstance(expected, dict):
        raise E9IdentityMismatch("missing runtime_identity in manifest")
    for key in ("git_head", "bundle_sha256", "relevant_content_sha256"):
        if actual.get(key) != expected.get(key):
            raise E9IdentityMismatch(f"runtime identity mismatch: {key}")
    if actual.get("relevant_worktree_dirty"):
        raise E9IdentityMismatch("relevant benchmark paths are dirty")


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


__all__ = ["E9IdentityMismatch", "collect_e9_identity", "validate_e9_identity"]
