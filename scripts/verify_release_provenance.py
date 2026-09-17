"""Verify that a published benchmark result belongs to its immutable release tag."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from packages.evals.itbench.benchmark import (
    BenchmarkError,
    _verify_seal,
    load_split,
    verify_report_seal,
)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BenchmarkError(f"git command failed: {' '.join(args)}") from exc


def verify_release(result_dir: Path) -> dict[str, Any]:
    """Verify a result directory against its release tag and sealed artifacts."""
    version_dir = result_dir.parent.name
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version_dir):
        raise BenchmarkError(f"result directory must be versioned, got {version_dir!r}")
    version = version_dir[1:]
    readme_path = result_dir.parent / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if not re.search(rf"^# {re.escape(version_dir)} results$", readme, re.MULTILINE):
        raise BenchmarkError("result README version does not match its directory")
    declared = re.search(r"Commit `([0-9a-f]{7,40})` \(tag `([^`]+)`\)", readme)
    if declared is None or declared.group(2) != version_dir:
        raise BenchmarkError("result README does not declare the expected release tag")

    tag_target = _git("rev-parse", f"{version_dir}^{{}}")
    if not tag_target.startswith(declared.group(1)):
        raise BenchmarkError("result README commit does not match the release tag target")
    manifest = _verify_seal(result_dir)
    if manifest["git_head"] != tag_target:
        raise BenchmarkError("manifest.git_head does not match the release tag target")
    if manifest["git_dirty"] is not False:
        raise BenchmarkError("published prediction manifest is marked dirty")
    expected_ids = list(load_split(manifest["split"]))
    if manifest["scenario_ids"] != expected_ids:
        raise BenchmarkError("manifest scenario IDs do not match the declared split")
    report_seal = verify_report_seal(result_dir)
    report = json.loads((result_dir / "report.json").read_text(encoding="utf-8"))
    if report["git_head"] != tag_target:
        raise BenchmarkError("report.git_head does not match the release tag target")
    return {
        "version": version,
        "tag": version_dir,
        "tag_target": tag_target,
        "result_dir": str(result_dir),
        "scenario_count": len(expected_ids),
        "manifest_git_head": manifest["git_head"],
        "report_seal_git_head": report_seal["git_head"],
        "git_dirty": manifest["git_dirty"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_release(args.result_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
