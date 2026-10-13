"""Write explanatory-only episode timing reports from frozen diagnosis JSON/JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packages.evals.episode_relevance import analyze_episode_relevance
from scripts.resolution_blocker_analysis import documents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--observations", type=Path, help="Frozen evidence-ID to source-record JSON map"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    inputs = [args.input, *([args.observations] if args.observations else [])]
    if args.output.resolve() in {path.resolve() for path in inputs}:
        parser.error("Output must not overwrite frozen input evidence")
    content = args.input.read_text()
    values = (
        [json.loads(line) for line in content.splitlines() if line.strip()]
        if args.input.suffix == ".jsonl"
        else [json.loads(content)]
    )
    observations = json.loads(args.observations.read_text()) if args.observations else None
    if observations is not None and not isinstance(observations, dict):
        parser.error("Observations must be an evidence-ID indexed JSON object")
    reports = [
        analyze_episode_relevance(doc, observations=observations, **meta)
        for value in values
        for doc, meta in documents(value)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"revisions": reports}, indent=2) + "\n")
    print(json.dumps({"revision_count": len(reports), "authority": "EXPLANATORY_ONLY"}))


if __name__ == "__main__":
    main()
