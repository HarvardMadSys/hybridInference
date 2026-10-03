"""Build fixed-history agent replay windows with source hashes and attribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def prepare(
    source: Path,
    output: Path,
    *,
    rounds: int,
    copies: int,
    max_tokens: int,
    tasks: list[str] | None = None,
    arrival_interval_s: float = 0.0,
    open_loop: bool = False,
) -> dict:
    """Select the first rounds of each public task without consulting model results."""
    if min(rounds, copies, max_tokens) < 1:
        raise ValueError("rounds, copies and max_tokens must be positive")
    if not math.isfinite(arrival_interval_s) or arrival_interval_s < 0:
        raise ValueError("arrival_interval_s must be finite and nonnegative")
    paths = sorted(source.glob("*.jsonl"))
    if tasks is not None:
        missing = set(tasks) - {path.stem for path in paths}
        if missing:
            raise ValueError(f"unknown tasks: {sorted(missing)}")
        paths = [path for path in paths if path.stem in tasks]
    rows: list[dict] = []
    sources: list[dict] = []
    for path in paths:
        blob = path.read_bytes()
        original = [json.loads(line) for line in blob.splitlines() if line.strip()]
        sources.append(
            {"name": path.name, "sha256": hashlib.sha256(blob).hexdigest(), "rows": len(original)}
        )
        for copy in range(copies):
            session = f"{path.stem}.copy{copy:02d}"
            for index, row in enumerate(original[:rounds]):
                item = {
                    "id": f"{session}.round{index:03d}",
                    "session_id": f"{session}.round{index:03d}" if open_loop else session,
                    "round_index": 0 if open_loop else index,
                    "source_round_index": index,
                    "arrival_s": 0.0,
                    "messages": row["messages"],
                    "max_tokens": max_tokens,
                    "tool_wait_s": float(row.get("simulated_tool_delay_ms_after", 0)) / 1000,
                    "fixed_history": True,
                }
                if row.get("tools"):
                    item["tools"] = row["tools"]
                rows.append(item)
    if not rows:
        raise ValueError("no source trace rows found")
    # Interleave independent tasks/copies rather than making task size track arrival order.
    rows.sort(key=lambda row: (row["source_round_index"], row["id"].split(".copy")[1], row["id"]))
    sessions = list(dict.fromkeys(row["session_id"] for row in rows))
    arrivals = {session: i * arrival_interval_s for i, session in enumerate(sessions)}
    for row in rows:
        row["arrival_s"] = arrivals[row["session_id"]]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {
        "source_url": "https://github.com/ArtificialAnalysis/aa-agentperf-local",
        "source_version": "0.3.3",
        "source_license": "Apache-2.0",
        "sources": sources,
        "selection": {"first_rounds_per_task": rounds, "copies": copies, "tasks": tasks},
        "max_tokens": max_tokens,
        "rows": len(rows),
        "sessions": len({row["session_id"] for row in rows}),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "replay": (
            "frozen independent invocation replay; exogenous arrivals; no live tools"
            if open_loop
            else "frozen recorded histories; closed-loop eligibility; no live tools"
        ),
        "arrival_interval_s": arrival_interval_s,
        "open_loop": open_loop,
        "limitations": [
            "not an official AA AgentPerf run or task-quality score",
            "same output cap on both endpoints; actual lengths may differ",
            "the source's target and actual output lengths are excluded from policy input",
            "source histories are fixed; this does not measure generated-answer quality",
            "repeated copies share prompt prefixes; cache state is not reset between runs",
        ],
    }
    with output.with_suffix(".manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return manifest


def main() -> None:
    """Prepare a public-trace subset; never send model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--tasks", nargs="+")
    parser.add_argument("--arrival-interval-s", type=float, default=0.0)
    parser.add_argument("--open-loop", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.source,
                args.output,
                rounds=args.rounds,
                copies=args.copies,
                max_tokens=args.max_tokens,
                tasks=args.tasks,
                arrival_interval_s=args.arrival_interval_s,
                open_loop=args.open_loop,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
