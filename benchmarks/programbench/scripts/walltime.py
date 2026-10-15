#!/usr/bin/env python3
"""Wall-clock minutes from a run's submission to its compared workspaces, per task, from the results.

    python3 scripts/walltime.py results figures/cost-parity/walltime.json

For every loop experiment under RESULTS with a parity comparison and a cost-to-match search against
Sol's single workers (results/luna-xhigh-<task>-v1/parity/sol-xhigh-<task>-single.json and
.match.json), records per run: the minutes to each Sol single worker's build (sol_build1), to each
loop run's parity build (loop_parity_build) and to its deliverable build at the cost-to-match budget
(loop_match_build; null when the loop never reached Sol's mean score). Times come from each attempt's
record: the run's submission and the snapshot taken when the build finished.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

NOTE = ("Wall-clock minutes from a run's submission to the snapshot after the named build (the runner's per-attempt timestamps). "
        "sol: each Sol single worker's build 1. parity: each Luna loop run's parity build (bench parity). "
        "match: each loop run's deliverable build at the cost-to-match budget (bench match; 0 for a run with no build within it, "
        "null for a task whose loops never reach Sol's mean score).")


def minutes_to_build(attempt: dict, k: int) -> float | None:
    """Minutes from submission to the snapshot after build ``k`` (None if the snapshot is missing)."""
    snapshot = (attempt.get("snapshots") or {}).get(f"build-{k}")
    if not snapshot or not attempt.get("submitted_at"):
        return None
    return round((snapshot["started_at"] - attempt["submitted_at"]) / 60, 2)


def attempt_json(results: Path, experiment: str, run: str) -> dict:
    return json.loads((results / experiment / "attempts" / run / "attempt.json").read_text())


def task_times(results: Path, parity: dict, match: dict) -> dict:
    luna, sol = parity["luna"], parity["sol"]
    sol_build1 = {r["run"]: minutes_to_build(attempt_json(results, sol, r["run"]), 1) for r in parity["sol_runs"] if not r["excluded"]}
    loops = [r for r in parity["luna_runs"] if not r["excluded"] and not r["promotional"]["excluded"]]
    parity_build = {r["run"]: minutes_to_build(attempt_json(results, luna, r["run"]), r["promotional"]["rounds"]) for r in loops}
    deliverables = match.get("deliverables")  # at the matching budget, or at the last budget searched
    match_build = None
    if match["outcome"] == "reached":
        match_build = {run: (minutes_to_build(attempt_json(results, luna, run), k) if k >= 1 else 0.0) for run, k in deliverables.items()}
    return {"sol_build1": sol_build1, "loop_parity_build": parity_build, "loop_match_build": match_build, "match_deliverables": deliverables}


def main(results: Path, out: Path) -> dict:
    tasks = {}
    for match_path in sorted(results.glob("luna-xhigh-*/parity/sol-xhigh-*.match.json")):
        match = json.loads(match_path.read_text())
        parity = json.loads(match_path.with_name(match_path.name.removesuffix(".match.json") + ".json").read_text())
        task = parity["luna"].removeprefix("luna-xhigh-").removesuffix("-v1")
        tasks[task] = task_times(results, parity, match)
    record = {"note": NOTE, "tasks": tasks}
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


if __name__ == "__main__":
    print(json.dumps({task: {k: v for k, v in t.items() if k != "match_deliverables"} for task, t in main(Path(sys.argv[1]), Path(sys.argv[2]))["tasks"].items()}, indent=1))
