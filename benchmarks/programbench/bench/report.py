"""Summaries: per-attempt table, per-arm aggregates, and the audits that decide which runs enter the
comparisons (`bench parity`, `bench match`)."""

from __future__ import annotations

import os
import statistics
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

from . import accounting, audit
from .config import Experiment, code_digest
from .evaluate import archive_id, infrastructure_error, load_scores
from .util import read_json, secret_values, write_json


def home_changes(meta: dict[str, Any]) -> list[str]:
    """Instruction and hook files in the Codex home (~/.codex) that differ from the attempt's start,
    at any snapshot or at the end."""
    if meta.get("home_surfaces") is None:
        return ["not checked at the start"]
    start = set(meta["home_surfaces"])
    snapshots = {label: s for label, s in (meta.get("snapshots") or {}).items() if isinstance(s, dict)}
    missing = [f"not checked after {label}" for label, s in snapshots.items() if s.get("home_surfaces") is None]
    if meta.get("home_surfaces_end") is None:
        missing.append("not checked at the end")
    seen = [*(s.get("home_surfaces") or [] for s in snapshots.values()), meta.get("home_surfaces_end") or []]
    return sorted({line for lines in seen for line in lines} - start) + missing


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


def _archived_codex_config(trajectories: Path) -> str | None:
    if not trajectories.exists():
        return None
    with tarfile.open(trajectories) as tar:
        for member in tar:
            if member.name.endswith(".codex/config.toml") and member.isfile():
                data = tar.extractfile(member)
                return data.read().decode() if data else None
    return None


def build(exp: Experiment, results: Path) -> dict[str, Any]:
    scores = load_scores(results)
    manifest = read_json(results / "manifest.json") if (results / "manifest.json").exists() else {}
    counts = Counter(v.get("scored_tests") for v in scores.values() if v.get("scored_tests"))
    expected_tests = counts.most_common(1)[0][0] if counts else None
    attempts = []
    for directory in sorted((results / "attempts").glob("*")):
        if not directory.is_dir() or "." in directory.name or not (directory / "attempt.json").exists():
            continue
        try:
            attempts.append(_attempt_record(exp, directory, scores, manifest, expected_tests))
        except Exception as error:  # one unreadable artifact must not sink the whole summary
            meta = read_json(directory / "attempt.json")
            attempts.append(_unreadable_record(meta, f"{type(error).__name__}: {error}"))
    summary = {
        "experiment": exp.id,
        "description": exp.raw.get("description"),
        "model": exp.model,
        "effort": exp.effort,
        "expected_scored_tests": expected_tests,
        "attempts": attempts,
        "discarded_attempts": _discarded(results, exp.pricing),
        "arms": _arms(attempts),
        "secrets": _secrets(results),
        "egress": _egress(results),
        "provenance": manifest.get("provenance"),
        "scored_by": _scoring_code(),
    }
    write_json(results / "summary.json", summary)
    (results / "summary.md").write_text(markdown(summary))
    return summary


def _attempt_record(exp: Experiment, directory: Path, scores: dict[str, Any], manifest: dict[str, Any], expected_tests: int | None) -> dict[str, Any]:
    meta = read_json(directory / "attempt.json")
    label = meta["label"]
    rounds = {k.split("__", 1)[1]: v for k, v in scores.items() if k.startswith(f"{label}__")}
    for name, score in rounds.items():
        archive = directory / "submission.tar.gz" if name == "final" else directory / "snapshots" / f"{name}.tar.gz"
        if score.get("archive_id") and (not archive.exists() or archive_id(archive) != score["archive_id"]):
            rounds[name] = {"score": None, "error_code": "stale_score", "error_details": "scored a different archive; run `eval`"}
    usage = accounting.usage(directory)
    costs = {node: round(accounting.cost(t, exp.pricing), 4) for node, t in usage["nodes"].items()}
    first_build_cost = None if usage["first_build_round"] is None else round(accounting.cost(usage["first_build_round"], exp.pricing), 4)
    snapshots = {k: v for k, v in (meta.get("snapshots") or {}).items() if isinstance(v, dict)}
    snapshot_order = sorted(snapshots, key=lambda k: snapshots[k].get("started_at") or 0)
    archived_config = _archived_codex_config(directory / "trajectories.tar.gz")
    record = {
        "label": label,
        "arm": meta["arm"],
        "state": meta.get("state"),
        "error": meta.get("error"),
        "terminal": meta.get("terminal"),
        "force_stopped": meta.get("force_stopped"),
        "builds": meta.get("builds"),
        "checks": meta.get("checks"),
        "build_outcomes": [b.get("code") or b.get("status") for b in meta.get("build_outcomes", [])],
        "verdicts": [v.get("verdict") or f"{v.get('status')}:{v.get('code')}" for v in meta.get("verdicts", [])],
        "wall_seconds": meta.get("wall_seconds"),
        "rounds": rounds,
        "score_final": (rounds.get("final") or {}).get("score"),
        "score_first_build": (rounds.get("build-1") or {}).get("score"),
        "snapshots": snapshots,
        "tokens": usage,
        "cost_usd": costs,
        "cost_first_build_usd": first_build_cost,
        "commands": audit.command_audit(directory / "trajectories.tar.gz"),
        "checker_edits": audit.checker_edits(directory, snapshot_order),
        "reference_sha256": meta.get("reference_sha256"),
        "harness_unchanged": meta.get("harness_unchanged"),
        "home_changes": home_changes(meta),
        "reference_available_after_build_1": bool(snapshots["build-1"]["reference_at"]) if "reference_at" in snapshots.get("build-1", {}) else None,
        "reference_copies_in_final": audit.reference_copies(directory / "submission.tar.gz", meta.get("reference_sha256")),
        "reference_copies_in_first_build": audit.reference_copies(directory / "snapshots" / "build-1.tar.gz", meta.get("reference_sha256")),
        "codex_config_unchanged": None if archived_config is None or not manifest.get("codex_config") else archived_config == manifest["codex_config"],
    }
    return record


def _egress(results: Path) -> dict[str, Any]:
    logs = "\n".join((d / "proxy.log").read_text() for d in sorted((results / "attempts").glob("*")) if (d / "proxy.log").exists())
    return audit.proxy_audit(logs)


def _unreadable_record(meta: dict[str, Any], error: str) -> dict[str, Any]:
    """A minimal record for an attempt whose artifacts could not be read (`bench parity` excludes it)."""
    return {
        "label": meta.get("label"), "arm": meta.get("arm"), "state": meta.get("state"), "error": meta.get("error"),
        "report_error": error, "terminal": meta.get("terminal"), "force_stopped": meta.get("force_stopped"),
        "builds": meta.get("builds"), "checks": meta.get("checks"), "build_outcomes": [], "verdicts": [],
        "wall_seconds": meta.get("wall_seconds"), "rounds": {}, "score_final": None, "score_first_build": None,
        "snapshots": {}, "tokens": {"nodes": {}, "sessions": [], "ledger_total": None, "ledger_nodes": {}}, "cost_usd": {}, "cost_first_build_usd": None,
        "commands": {}, "checker_edits": [], "reference_sha256": meta.get("reference_sha256"), "harness_unchanged": None,
        "home_changes": ["not checked: artifacts unreadable"], "reference_available_after_build_1": None,
        "reference_copies_in_final": [], "reference_copies_in_first_build": [], "codex_config_unchanged": None,
    }


def _discarded(results: Path, pricing: dict[str, Any]) -> list[dict[str, Any]]:
    """Attempts replaced by a re-run (``NN-arm.discarded-<time>``): kept, reported, never scored."""
    out = []
    for directory in sorted((results / "attempts").glob("*.discarded-*")):
        path = directory / "attempt.json"
        meta = read_json(path) if path.exists() else {}
        out.append({
            "directory": directory.name,
            "label": meta.get("label"),
            "state": meta.get("state"),
            "error": meta.get("error"),
            "force_stopped": meta.get("force_stopped"),
            "wall_seconds": meta.get("wall_seconds"),
            "cost_usd": _total_cost(accounting.usage(directory), pricing),
        })
    return out


def _total_cost(usage: dict[str, Any], pricing: dict[str, Any]) -> float | None:
    """None when no transcript was recovered: unknown, not free."""
    total = usage["nodes"].get("total")
    return None if total is None else round(accounting.cost(total, pricing), 4)


def _scoring_code() -> dict[str, Any]:
    """The code that produced this summary, which can differ from the code that ran the attempts."""
    return {"vcs_ref": os.environ.get("ZSBENCH_VCS_REF", "unknown"), "vcs_dirty": os.environ.get("ZSBENCH_VCS_DIRTY", "unknown"), "code_digest": code_digest()}


def _secrets(results: Path) -> dict[str, Any]:
    """Scan with the key when it is available; otherwise keep the last scan that had it."""
    previous = results / "summary.json"
    if not secret_values() and previous.exists():
        last = (read_json(previous).get("secrets") or {})
        if last.get("checked_literal_key"):
            return {**last, "note": "carried over from the last scan made with the key"}
    return audit.secret_scan(results)


def _arms(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    arms = {}
    for arm in sorted({a["arm"] for a in attempts}):
        group = [a for a in attempts if a["arm"] == arm]
        finals = [a["score_final"] for a in group if a["score_final"] is not None]
        costs = [a["cost_usd"]["total"] for a in group if "total" in a["cost_usd"]]
        arms[arm] = {
            "runs": len(group),
            "complete": sum(1 for a in group if a["state"] == "complete"),
            "scored": len(finals),
            "final_scores": finals,
            "median_final": statistics.median(finals) if finals else None,
            "mean_final": statistics.fmean(finals) if finals else None,
            "collapses_below_5pct": sum(1 for s in finals if s < 0.05),
            "costed": len(costs),
            "mean_cost_usd": statistics.fmean(costs) if costs else None,
            "total_cost_usd": round(sum(costs), 4),
        }
    return arms


def markdown(summary: dict[str, Any]) -> str:
    lines = [f"# {summary['experiment']}", "", summary.get("description") or "", "", f"Model `{summary['model']}` at effort `{summary['effort']}`; {summary.get('expected_scored_tests')} scored hidden tests per run.", ""]
    lines += ["| Run | Arm | State | Rounds | Verdicts | First build | Final | Wall (min) | Cost (USD) | Flags |", "|---|---|---|---|---|---|---|---|---|---|"]
    for a in summary["attempts"]:
        flags = [k for k, v in (a["commands"].get("rule_counts") or {}).items() if v]
        if a["commands"].get("web_search_calls"):
            flags.append("web_search")
        if any(r["workspace_changes"] for r in a["checker_edits"]):
            flags.append("checker_edited_sources")
        if a.get("force_stopped") and (a.get("terminal") or {}).get("reason") == "force_stopped":
            flags.append(f"force-stopped ({a['force_stopped']})")
        if a.get("error"):
            flags.append(f"error: {a['error'][:80]}")
        if a.get("reference_sha256") and any((v or {}).get("executable_hash") == a["reference_sha256"] for v in a["rounds"].values()):
            flags.append("built executable is the reference")
        if a.get("harness_unchanged") is False:
            flags.append("harness modified")
        if a.get("home_changes") or a["commands"].get("agents_md_loaded"):
            flags.append("instructions left for later sessions")
        if (a.get("build_outcomes") or [None])[0] == "timeout":
            flags.append("build 1 timed out")
        if a.get("reference_available_after_build_1") is False:
            flags.append("reference gone after build 1")
        if any(s.get("usage_decreased") for s in a["tokens"].get("sessions", [])):
            flags.append("usage decreased in a thread")
        for name in ("build-1", "final"):
            score = (a["rounds"].get(name) or {})
            if score.get("error_code") or score.get("test_branch_errors"):
                flags.append(f"{name}: {infrastructure_error(score) or score.get('error_code') or 'test run timed out (its tests count as failed)'}")
        cost = a["cost_usd"].get("total")
        lines.append(
            f"| {a['label']} | {a['arm']} | {a['state']} | {a['builds'] or 0} | {', '.join(v or '—' for v in a['verdicts']) or '—'} | "
            f"{_pct(a['score_first_build'])} | {_pct(a['score_final'])} | "
            f"{(a['wall_seconds'] or 0) / 60:.0f} | {'—' if cost is None else f'{cost:.2f}'} | {', '.join(flags) or '—'} |"
        )
    lines += ["", "| Arm | Runs | Complete | Scored | Median final | Mean final | Collapses | Mean cost |", "|---|---|---|---|---|---|---|---|"]
    for arm, s in summary["arms"].items():
        mean_cost = "—" if s["mean_cost_usd"] is None else f"${s['mean_cost_usd']:.2f} (n={s['costed']})"
        lines.append(f"| {arm} | {s['runs']} | {s['complete']} | {s['scored']} | {_pct(s['median_final'])} | {_pct(s['mean_final'])} | {s['collapses_below_5pct']} | {mean_cost} |")
    for d in summary.get("discarded_attempts") or []:
        cost = "unknown cost" if d["cost_usd"] is None else f"${d['cost_usd']:.2f}"
        lines.append(f"- Discarded and re-run: {d['directory']} ({d['state']}; {d.get('error') or d.get('force_stopped') or 'no error recorded'}; {cost})")
    p, scored = summary.get("provenance") or {}, summary.get("scored_by") or {}
    lines += ["", f"Attempts ran at commit {p.get('vcs_ref', 'unknown')} (dirty={p.get('vcs_dirty', 'unknown')}); scored at {scored.get('vcs_ref', 'unknown')} (dirty={scored.get('vcs_dirty', 'unknown')})."]
    s = summary["secrets"]
    lines += ["", f"Secret scan: literal key checked={s['checked_literal_key']}, hits={len(s['literal_key_hits'])}{' — DO NOT PUBLISH' if s['literal_key_hits'] else ''}."]
    if summary.get("egress"):
        egress = summary["egress"]
        lines.append(f"Egress: connected {egress['established']}; refused {egress['refused']}.")
    return "\n".join(lines) + "\n"
