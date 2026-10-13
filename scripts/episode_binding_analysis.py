"""Validate explicit frozen episode-binding bundles; development use only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.episode_binding import EpisodeBindingBundle, assess_episode_binding


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Output must not overwrite frozen evidence")
    data = json.loads(args.input.read_text())
    rows = data if isinstance(data, list) else [data]
    # Validate all inputs before creating output; no partial positive report.
    bundles = [EpisodeBindingBundle.model_validate(row) for row in rows]
    result = {
        "assessment_kind": "DEVELOPMENT_CONTRACT_CHECK",
        "source_authenticity": "NOT_VALIDATED",
        "assessments": [assess_episode_binding(b).model_dump(mode="json") for b in bundles],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"bundle_count": len(bundles), "authority": "EXPLANATORY_ONLY"}))


if __name__ == "__main__":
    main()
