"""Run the benchmark.

    python -m bench --model anthropic/claude-sonnet-4.6 --replicates 5

Results are written as JSON lines so a run can be re-analysed without re-running it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from .runner import CONDITIONS, OpenRouter, RunResult, run_one
from .tasks import build_tasks


def main() -> int:
    parser = argparse.ArgumentParser(prog="bench")
    parser.add_argument("--model", default="anthropic/claude-sonnet-4.6")
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--tasks", default="")
    parser.add_argument("--suite", default="core", choices=["core", "hard", "grid"])
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--out", default="bench/results.jsonl")
    args = parser.parse_args()

    tasks = build_tasks(args.suite)
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t.name in wanted]
    conditions = args.conditions.split(",")
    client = OpenRouter(args.model)

    jobs = [
        (task, condition, replicate)
        for task in tasks
        for condition in conditions
        for replicate in range(args.replicates)
    ]
    print(f"{len(jobs)} runs: {len(tasks)} tasks x {len(conditions)} conditions "
          f"x {args.replicates} replicates on {args.model}", file=sys.stderr)

    results: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_one, t, c, client, r) for t, c, r in jobs]
        for n, future in enumerate(futures, 1):
            result = future.result()
            results.append(result)
            print(f"  [{n}/{len(jobs)}] {result.task:<18} {result.condition:<15} "
                  f"{result.outcome}{' SILENT' if result.silent_error else ''}",
                  file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as handle:
        for result in results:
            handle.write(json.dumps(asdict(result)) + "\n")

    report(results, args.model)
    return 0


def report(results: list[RunResult], model: str) -> None:
    usable = [r for r in results if r.outcome != "error"]
    errors = len(results) - len(usable)

    print(f"\nModel: {model}   runs: {len(usable)}"
          + (f"   ({errors} transport errors excluded)" if errors else ""))
    print("\nBy condition")
    print(f"  {'condition':<16}{'correct':>9}{'wrong':>8}{'blocked':>9}"
          f"{'undet':>8}{'silent':>9}{'flagged':>9}{'calls':>8}")
    for condition in CONDITIONS:
        rows = [r for r in usable if r.condition == condition]
        if not rows:
            continue
        n = len(rows)
        correct = sum(r.outcome == "correct" for r in rows)
        wrong = sum(r.outcome == "wrong" for r in rows)
        blocked = sum(r.outcome == "blocked" for r in rows)
        undet = sum(r.undetected for r in rows)
        silent = sum(r.silent_error for r in rows)
        flagged = sum(bool(r.audit_unsourced or r.audit_mislabelled) for r in rows)
        calls = sum(r.tool_calls for r in rows) / n
        print(f"  {condition:<16}{correct/n:>8.0%}{wrong/n:>8.0%}{blocked/n:>9.0%}"
              f"{undet/n:>8.0%}{silent/n:>9.0%}{flagged/n:>9.0%}{calls:>8.1f}")

    print("\nSilent error rate by hazard")
    hazards = sorted({r.hazard for r in usable})
    print(f"  {'hazard':<20}" + "".join(f"{c:>16}" for c in CONDITIONS))
    for hazard in hazards:
        cells = []
        for condition in CONDITIONS:
            rows = [r for r in usable if r.hazard == hazard and r.condition == condition]
            cells.append(f"{sum(r.silent_error for r in rows)/len(rows):>15.0%}" if rows else f"{'-':>16}")
        print(f"  {hazard:<20}" + "".join(cells))

    print("\nFailures observed")
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for r in usable:
        if r.outcome in ("wrong", "blocked") and r.detail:
            seen[(r.condition, r.detail[:90])] += 1
    for (condition, detail), count in sorted(seen.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {count:>3}x  [{condition}] {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
