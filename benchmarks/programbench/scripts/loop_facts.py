#!/usr/bin/env python3
"""Per-run facts of loop experiments, from their summaries: rounds, whether the checker accepted, wall-clock
minutes, model cost in total and per node, and the cost of a round.

    python3 scripts/loop_facts.py results figures/checker-session/loops.json [EXPERIMENT ...]

Reads results/<experiment>/summary.json for every experiment named (a name or its results
directory), or for every luna-xhigh-*-v1 and luna-xhigh-*-checker-resumed directory under RESULTS
when none is named, and writes
{experiment: {run: facts}}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run_facts(attempt: dict) -> dict:
    builds = attempt.get("builds") or 0
    cost = attempt.get("cost_usd") or {}
    terminal = attempt.get("terminal") or {}
    return {
        "state": attempt.get("state"),
        "builds": builds,
        "checks": attempt.get("checks") or 0,
        "accepted": terminal.get("status") == "succeeded",
        "force_stopped": bool(attempt.get("force_stopped")),
        "wall_minutes": round((attempt.get("wall_seconds") or 0) / 60, 1),
        "cost_usd": cost.get("total"),
        "cost_build_usd": cost.get("build"),
        "cost_check_usd": cost.get("check"),
        "usd_per_round": round(cost["total"] / builds, 4) if cost.get("total") is not None and builds else None,
    }


def main(results: Path, out: Path, experiments: list[str]) -> dict:
    if not experiments:
        experiments = sorted(p.name for p in results.glob("luna-xhigh-*") if p.is_dir() and (p.name.endswith("-v1") or p.name.endswith("-checker-resumed")))
    record = {}
    for experiment in (Path(e).name for e in experiments):
        summary = json.loads((results / experiment / "summary.json").read_text())
        record[experiment] = {a["label"]: run_facts(a) for a in summary["attempts"] if a.get("arm") == "loop"}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


if __name__ == "__main__":
    record = main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:])
    for experiment, runs in record.items():
        accepted = sum(r["accepted"] for r in runs.values())
        print(f"{experiment}: {len(runs)} runs, {accepted} accepted, rounds {[r['builds'] for r in runs.values()]}, cost per round {[r['usd_per_round'] for r in runs.values()]}")
