"""Checker-session study: the loop whose checker keeps its session across rounds against the loop whose
checker starts fresh every round, both through the same Sol single workers, from the JSONs that
`bench parity` and `bench match` write (figures/checker-session/<loop>.json|.match.json for the kept
session, figures/cost-parity/<fresh loop>.json|.match.json for the fresh one).

    python3 scripts/plot_checker_session.py figures/checker-session/*.json [--out-dir DIR]

Writes to figures/ (or DIR):
- checker-session-equal-cost.png: per task, Sol's single shot and both loops at Sol's single-shot cost
  (means of 5 runs with 95% bootstrap intervals; rows by the kept session's lead over the fresh one);
- checker-session-equal-score.png: per task, each loop's cost to reach Sol's mean score as a share of
  Sol's cost.

Needs matplotlib; fonts as in plotstyle.py.
"""
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
from matplotlib import font_manager
from matplotlib.lines import Line2D

import plot_parity
from plotstyle import CANVAS, HAIRLINE, INK, INK_2, MUTED, RUST, load_fonts

SUFFIX = "-checker-resumed"
SERIES = {  # key -> (legend, color, marker)
    "sol": ("GPT-5.6 Sol (xhigh), single shot", INK, "o"),
    "fresh": ("GPT-5.6 Luna (xhigh), loop at Sol's cost, fresh checker session every round", RUST, "D"),
    "kept": ("GPT-5.6 Luna (xhigh), loop at Sol's cost, checker keeps its session", INK_2, "^"),
}


def task_of(o: dict) -> str:
    return o["luna"].removeprefix("luna-xhigh-").removesuffix(SUFFIX).removesuffix("-v1")


def rows_from(paths: list[Path], cost_parity: Path) -> list[dict]:
    """One row per task: Sol's and both loops' means and intervals at Sol's cost, and both loops' cost ratios."""
    rows = []
    for path in paths:
        kept = json.loads(path.read_text())
        kept_match = json.loads(path.with_name(path.name.removesuffix(".json") + ".match.json").read_text())
        fresh_id = kept["luna"].removesuffix(SUFFIX) + "-v1"
        fresh = json.loads((cost_parity / f"{fresh_id}.json").read_text())
        fresh_match = json.loads((cost_parity / f"{fresh_id}.match.json").read_text())
        if fresh["sol"] != kept["sol"]:
            raise ValueError(f"{path.name}: compared with {kept['sol']}, the fresh loop with {fresh['sol']}")
        k, f = kept["comparisons"]["promotional"], fresh["comparisons"]["promotional"]
        rows.append({
            "task": task_of(kept), "budget": k["budget_usd"],
            "sol": (f["sol_mean"], f["sol_ci"]), "fresh": (f["luna_mean"], f["luna_ci"]), "kept": (k["luna_mean"], k["luna_ci"]),
            "fresh_ratio": fresh_match["cost_ratio"] if fresh_match["outcome"] == "reached" else math.inf,
            "kept_ratio": kept_match["cost_ratio"] if kept_match["outcome"] == "reached" else math.inf,
            "fresh_rounds": statistics.fmean(r["promotional"]["rounds"] for r in fresh["luna_runs"] if not r["excluded"]),
            "kept_rounds": statistics.fmean(r["promotional"]["rounds"] for r in kept["luna_runs"] if not r["excluded"]),
        })
    rows.sort(key=lambda r: -(r["kept"][0] - r["fresh"][0]))
    return rows


def equal_cost(rows: list[dict], out: Path) -> Path:
    n = len(rows)
    higher = sum(r["kept"][0] > r["fresh"][0] for r in rows)
    lower = sum(r["kept"][0] < r["fresh"][0] for r in rows)
    title = (f"With a checker that keeps its session, the loop\nscores higher at Sol's cost on {higher} of {n} sampled\n"
             f"ProgramBench tasks and lower on {lower}")
    descriptions = dict(plot_parity.TASKS)
    fig, ax = plt.subplots(figsize=(9.6, 0.8 * n + 2.4), dpi=200)
    offsets = {"sol": 0.25, "fresh": 0.0, "kept": -0.25}
    for i, r in enumerate(rows):
        y = n - 1 - i
        for key, (_, color, marker) in SERIES.items():
            (mean, ci), dy = r[key], offsets[key]
            ax.plot(ci, [y + dy] * 2, color=color, lw=2.4, solid_capstyle="round", alpha=0.85, zorder=2)
            ax.scatter([mean], [y + dy], s=52, color=color, marker=marker, edgecolor=CANVAS, linewidth=1.2, zorder=3)
            ax.annotate(f"{mean:.1f}%", (ci[1], y + dy), xytext=(7, 0), textcoords="offset points", va="center", fontsize=8.4, color=color, fontweight="medium")
    ax.set_yticks(range(n))
    ax.set_yticklabels([f"{r['task']}\n{descriptions.get(r['task'], '')}" for r in reversed(rows)], fontsize=9.5, linespacing=1.3)
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_xlim(0, 100)
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(10))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%d%%"))
    ax.set_xlabel("Hidden tests passed (mean of 5 runs, 95% bootstrap interval)", labelpad=8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=HAIRLINE, lw=0.9)
    ax.set_axisbelow(True)
    for label in ax.get_xticklabels():
        label.set_fontproperties(font_manager.FontProperties(family="DejaVu Sans Mono", size=9))
    handles = [Line2D([], [], color=color, marker=marker, lw=2.4, ms=6.5, mec=CANVAS, label=legend) for legend, color, marker in SERIES.values()]
    ax.legend(handles=handles, loc="lower left", frameon=False, fontsize=9.3, bbox_to_anchor=(0.0, 1.0), ncol=1, handlelength=2.2)
    ax.set_title(title, loc="left", fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", fontsize=16, color=INK, pad=62, linespacing=1.15)
    fig.tight_layout()
    path = out / "checker-session-equal-cost.png"
    fig.savefig(path)
    return path


def _median_ratio(ratios: list[float]) -> str:
    median = statistics.median(ratios)
    return "not reached" if math.isinf(median) else f"{100 * median:.0f}%"


def equal_score(rows: list[dict], out: Path) -> Path:
    n = len(rows)
    rows = sorted(rows, key=lambda r: (min(r["fresh_ratio"], r["kept_ratio"]), r["task"]))
    finite = [v for r in rows for v in (r["fresh_ratio"], r["kept_ratio"]) if not math.isinf(v)]
    right = max(1.2, max(finite, default=1.0) * 1.1)
    title = (f"The loop reaches Sol's score for {_median_ratio([r['fresh_ratio'] for r in rows])} of Sol's cost with a fresh\n"
             f"checker session every round, {_median_ratio([r['kept_ratio'] for r in rows])} with a kept session\n"
             f"(medians over {n} sampled ProgramBench tasks)")
    fig, ax = plt.subplots(figsize=(8.6, 0.56 * n + 2.2), dpi=200)
    ax.axvline(1, color=INK, lw=1, ls=(0, (4, 3)), zorder=1)
    ax.text(1 + right * 0.01, n - 0.4, "Sol's cost", fontsize=8.6, color=INK_2, va="bottom")
    box = {"facecolor": CANVAS, "edgecolor": "none", "pad": 0.8}
    for i, r in enumerate(rows):
        y = n - 1 - i
        for key, dy, color in (("fresh_ratio", 0.18, RUST), ("kept_ratio", -0.18, INK_2)):
            ratio = r[key]
            if math.isinf(ratio):
                ax.text(right * 0.007, y + dy, "not reached", va="center", fontsize=8.2, color=MUTED, bbox=box, zorder=3)
                continue
            ax.barh(y + dy, ratio, color=color, height=0.32, zorder=2)
            ax.text(ratio + right * 0.012, y + dy, f"{100 * ratio:.0f}%", va="center", fontsize=8.2, color=color, bbox=box, zorder=3)
    ax.set_yticks(range(n), [r["task"] for r in reversed(rows)], fontsize=9)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, right)
    ax.set_ylim(-0.8, n + 0.2)
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.5 if right <= 3 else 1.0))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{100 * v:.0f}%"))
    ax.set_xlabel("The loop's cost to reach Sol's mean score, as a share of Sol's cost", labelpad=8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    handles = [Line2D([], [], color=RUST, lw=8, label="fresh checker session every round"), Line2D([], [], color=INK_2, lw=8, label="checker keeps its session")]
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=9)
    ax.set_title(title, loc="left", fontsize=14, pad=12, fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", color=INK)
    fig.tight_layout()
    path = out / "checker-session-equal-score.png"
    fig.savefig(path)
    return path


def summary(rows: list[dict]) -> str:
    diffs = [r["kept"][0] - r["fresh"][0] for r in rows]
    lines = [f"{len(rows)} tasks; at Sol's cost the kept-session loop is higher on {sum(d > 0 for d in diffs)}, lower on {sum(d < 0 for d in diffs)}; "
             f"mean difference {statistics.fmean(diffs):+.1f} points, median {statistics.median(diffs):+.1f}",
             f"cost to reach Sol's score, median share of Sol's cost: fresh {_median_ratio([r['fresh_ratio'] for r in rows])}, kept {_median_ratio([r['kept_ratio'] for r in rows])}; "
             f"kept cheaper on {sum(r['kept_ratio'] < r['fresh_ratio'] for r in rows)} tasks",
             f"rounds within Sol's budget (task means): fresh {statistics.fmean(r['fresh_rounds'] for r in rows):.1f}, kept {statistics.fmean(r['kept_rounds'] for r in rows):.1f}"]
    for r in sorted(rows, key=lambda r: r["task"]):
        lines.append(f"  {r['task']:22} Sol {r['sol'][0]:5.1f}  fresh {r['fresh'][0]:5.1f} ({r['fresh_rounds']:4.1f} rounds)  kept {r['kept'][0]:5.1f} ({r['kept_rounds']:4.1f} rounds)  "
                     f"diff {r['kept'][0] - r['fresh'][0]:+5.1f}  cost to match: fresh {_median_ratio([r['fresh_ratio']])}, kept {_median_ratio([r['kept_ratio']])}")
    return "\n".join(lines)


def main(paths: list[Path], out_dir: Path | None = None) -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    rows = rows_from(sorted(p for p in paths if not p.name.endswith(".match.json")), root / "figures" / "cost-parity")
    load_fonts()
    plt.rcParams.update({
        "font.family": ["Spline Sans", "DejaVu Sans"], "font.size": 10.5,
        "figure.facecolor": CANVAS, "axes.facecolor": CANVAS, "savefig.facecolor": CANVAS,
        "text.color": INK, "axes.edgecolor": MUTED, "axes.linewidth": 0.8, "axes.labelcolor": INK_2, "axes.labelweight": "medium",
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2, "legend.labelcolor": INK_2,
    })
    out = out_dir or root / "figures"
    print(summary(rows))
    return [equal_cost(rows, out), equal_score(rows, out)]


if __name__ == "__main__":
    args = sys.argv[1:]
    out_dir = Path(args.pop(args.index("--out-dir") + 1)) if "--out-dir" in args else None
    args = [a for a in args if a != "--out-dir"]
    for path in main([Path(p) for p in args], out_dir):
        print(path)
