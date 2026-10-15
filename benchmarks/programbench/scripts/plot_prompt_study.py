"""Prompt study: GPT-5.6 Luna's loop against the same model prompted with both roles in one session,
from the JSONs that `bench parity` and `bench match` write (`<loop>.json` and `<loop>.match.json`).

    python3 scripts/plot_prompt_study.py figures/prompt-study/*.json [--out-dir DIR]

Writes these figures to figures/ (or DIR):
- prompt-study-equal-cost.png: per task, the sessions against the loop at the sessions' mean cost
  (means with 95% bootstrap intervals; rows by the loop's lead);
- prompt-study-equal-score.png: per task, the loop's cost to reach the sessions' mean score, as a
  share of their mean cost; tasks the loop never gets there give the gap at equal cost instead;
- prompt-study-three-way.png: per task, GPT-5.6 Sol's single shot, the sessions and the loop at Sol's
  single-shot cost (the cost-parity study's figures/cost-parity/<loop>.json); with --single-shot luna,
  GPT-5.6 Luna's own single shot instead (the loop's build 1, as scored at build 1), and with
  --single-shot both, both single shots (prompt-study-four-way.png);
- repeatability.png: each run's score minus the mean of its setup's runs on the same task, pooled over
  the tasks, for those four setups;
- repeatability-cost.png: the share of runs within 5 points of their task's mean against each setup's
  model cost (the loop after its first build, at Sol's cost and after all its rounds).
- performance-cost.png: the mean score over the tasks against the same costs.

Needs matplotlib; fonts as in plotstyle.py.
"""
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
from matplotlib import font_manager
from matplotlib.lines import Line2D

import plot_parity
from bench.parity import bootstrap_interval
from plotstyle import CANVAS, HAIRLINE, INK, INK_2, MUTED, RUST, load_fonts

LEGEND = ("GPT-5.6 Luna (xhigh), one prompted session", "GPT-5.6 Luna (xhigh), loop at the same cost")
TITLE = "Zeroshot's loop outperforms one session\nprompted with both roles at equal cost\nin {count} of {n} sampled ProgramBench tasks"


def task_of(o: dict) -> str:
    return o["luna"].removeprefix("luna-xhigh-").removesuffix("-v1")  # luna-xhigh-<task>-v1


def equal_score(pairs: list[tuple[dict, dict]], out: Path, title: str | None = None, name: str = "prompt-study-equal-score.png",
                other: str = "the sessions", other_cost: str = "the sessions' cost") -> Path:
    """``other`` names the single-shot arm the loop is matched against; ``title`` gets ``median`` (percent) and ``n``."""
    rows = []
    for parity, match in pairs:
        reached = match["outcome"] == "reached"
        c = parity["comparisons"]["promotional"]
        rows.append({"task": task_of(match), "ratio": match["cost_ratio"] if reached else math.inf, "delta": c["luna_mean"] - c["sol_mean"]})
    rows.sort(key=lambda r: (r["ratio"], -r["delta"]))
    n = len(rows)
    median = statistics.median(r["ratio"] for r in rows)
    longest = max([r["ratio"] for r in rows if not math.isinf(r["ratio"])] or [1.0])
    right = max(1.2, longest * 1.1)
    fig, ax = plt.subplots(figsize=(8.6, 0.34 * n + 2.0), dpi=200)
    ax.axvline(1, color=INK, lw=1, ls=(0, (4, 3)), zorder=1)
    ax.text(1 + right * 0.01, n - 0.45, other_cost, fontsize=8.6, color=INK_2, va="bottom")
    box = {"facecolor": CANVAS, "edgecolor": "none", "pad": 0.8}
    for i, r in enumerate(rows):
        y = n - 1 - i
        if math.isinf(r["ratio"]):
            ax.text(right * 0.007, y, f"not reached ({r['delta']:+.1f} points at equal cost)".replace("-", "−"), va="center", fontsize=8.6, color=MUTED, bbox=box, zorder=3)
            continue
        color = RUST if r["ratio"] < 1 else MUTED
        ax.barh(y, r["ratio"], color=color, height=0.62, zorder=2)
        ax.text(r["ratio"] + right * 0.014, y, f"{100 * r['ratio']:.0f}%", va="center", fontsize=8.6, color=RUST if r["ratio"] < 1 else INK_2, bbox=box, zorder=3)
    ax.set_yticks(range(n), [r["task"] for r in reversed(rows)], fontsize=9)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, right)
    ax.set_ylim(-1.0, n + 0.3)
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.5 if right <= 3 else 1.0))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{100 * v:.0f}%"))
    ax.set_xlabel(f"The loop's cost to reach {other}' mean score, as a share of {other_cost}" if other.endswith("s") else f"The loop's cost to reach {other}'s mean score, as a share of {other_cost}")
    ax.spines[["top", "right", "left"]].set_visible(False)
    if title is not None and not math.isinf(median):
        title = title.format(median=100 * median, n=n)
    elif math.isinf(median):
        title = f"On most of {n} sampled ProgramBench tasks, the loop never reaches\nthe score of {other}"
    else:
        title = f"The loop reaches the score of one session prompted with both roles\nfor {100 * median:.0f}% of its cost (median of {n} sampled ProgramBench tasks)"
    ax.set_title(title, loc="left", fontsize=14, pad=12, fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", color=INK)
    fig.tight_layout()
    path = out / name
    fig.savefig(path)
    return path


def luna_single_shots(parity: dict, match: dict) -> list[float]:
    """Each loop run's build 1 (byte-identical to a single shot of the builder prompt), scored on all tests."""
    out = []
    for r in parity["luna_runs"]:
        build1 = r["promotional"]["workspace"] == "build-1"
        if r["excluded"] or (build1 and r["promotional"]["excluded"]) or r["run"] not in match["runs"]:
            continue
        out.append(match["runs"][r["run"]]["pass_pct"]["1"])
    return out


LIGHT_RUST = "#DB8373"  # RUST mixed with the canvas: the loop's model without the loop


def three_way(pairs: list[tuple[dict, dict]], cost_parity: dict[str, dict], single_shot: str, out: Path) -> Path:
    """Per task: single shots, one session with both roles in its prompt, and the loop at Sol's single-shot
    cost. ``single_shot`` is "sol" (Sol's single shot), "luna" (Luna's own) or "both"."""
    rows = []
    for parity, match in pairs:
        cp = cost_parity[parity["luna"]]["comparisons"]["promotional"]
        session = parity["comparisons"]["promotional"]
        shots = luna_single_shots(parity, match)
        rows.append({"task": task_of(parity), "sol": (cp["sol_mean"], cp["sol_ci"]), "luna": (statistics.fmean(shots), bootstrap_interval(shots)),
                     "session": (session["sol_mean"], session["sol_ci"]), "loop": (cp["luna_mean"], cp["luna_ci"])})
    base = "luna" if single_shot == "luna" else "sol"
    rows.sort(key=lambda r: -(r["loop"][0] - r[base][0]))
    n = len(rows)
    wins = {key: sum(r[key][0] > r[base][0] for r in rows) for key in ("luna", "session", "loop")}
    labels = {
        "sol": ("GPT-5.6 Sol (xhigh), single shot", INK, "o"),
        "luna": ("GPT-5.6 Luna (xhigh), single shot", INK if base == "luna" else LIGHT_RUST, "o"),
        "session": ("GPT-5.6 Luna (xhigh), single shot with both roles in its prompt", MUTED, "s"),
        "loop": (f"GPT-5.6 Luna (xhigh), review loop at {'the cost of Sol' + chr(39) + 's single shot' if base == 'luna' else 'the same model cost as Sol'}", RUST, "D"),
    }
    keys = {"sol": ["sol", "session", "loop"], "luna": ["luna", "session", "loop"], "both": ["sol", "luna", "session", "loop"]}[single_shot]
    if single_shot == "luna":
        title = (f"A small model outperforms its own single shot\nin {wins['loop']} of {n} sampled ProgramBench tasks\n"
                 f"with a review loop, in {wins['session']} of {n} with a role prompt")
    elif single_shot == "sol":
        title = (f"A small model outperforms a single-shot large\nmodel in {wins['loop']} of {n} sampled ProgramBench tasks\n"
                 f"with a review loop, in {wins['session']} of {n} with a role prompt")
    else:
        shots = (f"but in {wins['luna']} of {n} as a single shot,\nwith or without a role prompt" if wins["luna"] == wins["session"]
                 else f"but in {wins['luna']} of {n} as a single shot\nand in {wins['session']} of {n} with a role prompt")
        title = f"A small model outperforms a single-shot large model\nin {wins['loop']} of {n} sampled ProgramBench tasks\nwith a review loop, {shots}"
    descriptions = dict(plot_parity.TASKS)
    spacing = 0.2 if len(keys) == 4 else 0.25
    per_task = 0.95 if len(keys) == 4 else 0.8
    fig, ax = plt.subplots(figsize=(9.6, per_task * n + 2.4 + 0.4 * (len(keys) - 3)), dpi=200)
    offsets = {key: spacing * (i - (len(keys) - 1) / 2) for i, key in enumerate(keys)}
    for i, r in enumerate(rows):
        y = n - 1 - i
        for key in keys:
            (mean, ci), (_, color, marker), dy = r[key], labels[key], offsets[key]
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
    handles = [Line2D([], [], color=labels[key][1], marker=labels[key][2], lw=2.4, ms=6.5, mec=CANVAS, label=labels[key][0]) for key in reversed(keys)]
    ax.legend(handles=handles, loc="lower left", frameon=False, fontsize=9.3, bbox_to_anchor=(0.0, 1.0), ncol=1, handlelength=2.2)
    ax.set_title(title, loc="left", fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", fontsize=16, color=INK, pad=20 * len(keys) + 2, linespacing=1.15)
    fig.tight_layout()
    path = out / {"sol": "prompt-study-three-way.png", "luna": "prompt-study-three-way-luna.png", "both": "prompt-study-four-way.png"}[single_shot]
    fig.savefig(path)
    return path


def run_scores(pairs: list[tuple[dict, dict]], cost_parity: dict[str, dict]) -> dict[str, dict[str, list[float]]]:
    """Per task, each setup's run scores (percent of hidden tests passed): Sol's and Luna's single shots,
    the sessions, and the loop at Sol's single-shot cost."""
    out = {}
    for parity, match in pairs:
        cp = cost_parity[parity["luna"]]
        n = cp["scored_tests"]
        out[task_of(parity)] = {
            "sol": [100 * r["passed"] / n for r in cp["sol_runs"] if not r["excluded"]],
            "luna": luna_single_shots(parity, match),
            "session": [100 * r["passed"] / n for r in parity["sol_runs"] if not r["excluded"]],
            "loop": [100 * r["promotional"]["passed"] / n for r in cp["luna_runs"] if not r["excluded"] and not r["promotional"]["excluded"]],
        }
    return out


def _swarm(values: list[float], width: float, step: float) -> list[float]:
    """Horizontal offsets that spread points sharing a value band side by side (a simple beeswarm)."""
    offsets, taken = [0.0] * len(values), {}
    for i in sorted(range(len(values)), key=lambda i: abs(values[i])):
        band = round(values[i] / width)
        k = taken.get(band, 0)
        taken[band] = k + 1
        offsets[i] = step * ((k + 1) // 2) * (1 if k % 2 else -1)
    return offsets


def repeatability(pairs: list[tuple[dict, dict]], cost_parity: dict[str, dict], out: Path, band: float = 5.0, title: str | None = None,
                  name: str = "repeatability.png") -> Path:
    """Each run's score minus the mean of its setup's runs on the same task, pooled over tasks."""
    scores = run_scores(pairs, cost_parity)
    setups = (("sol", "GPT-5.6 Sol\nsingle shot", INK), ("luna", "GPT-5.6 Luna\nsingle shot", LIGHT_RUST),
              ("session", "GPT-5.6 Luna\nsingle shot with\nboth roles in its prompt", MUTED), ("loop", "GPT-5.6 Luna\nreview loop\nat Sol's cost", RUST))
    deviations = {key: [(task, x - statistics.fmean(runs[key])) for task, runs in scores.items() for x in runs[key]] for key, _, _ in setups}
    within = {key: 100 * sum(abs(d) <= band for _, d in devs) / len(devs) for key, devs in deviations.items()}
    # The loop's lead over Sol in that share, with a percentile bootstrap over the tasks (bench.parity's seed and resamples).
    per_task = {key: [100 * statistics.fmean(abs(d) <= band for task, d in devs if task == t) for t in scores] for key, devs in deviations.items()}
    lead = [a - b for a, b in zip(per_task["loop"], per_task["sol"])]
    low, high = bootstrap_interval(lead)
    print(f"[repeatability] within {band:g} points: " + ", ".join(f"{k} {v:.1f}%" for k, v in within.items())
          + f"; loop minus Sol {statistics.fmean(lead):+.1f} points (95% bootstrap interval over {len(lead)} tasks: {low:+.1f} to {high:+.1f})")
    fig, ax = plt.subplots(figsize=(9.6, 7.4), dpi=200)
    ax.axhspan(-band, band, color=HAIRLINE, alpha=0.55, lw=0, zorder=0)
    ax.axhline(0, color=MUTED, lw=0.8, zorder=1)
    for x, (key, _, color) in enumerate(setups):
        devs = deviations[key]
        offsets = _swarm([d for _, d in devs], width=0.9, step=0.032)
        ax.scatter([x + o for o in offsets], [d for _, d in devs], s=22, color=color, edgecolor=CANVAS, linewidth=0.6, zorder=3)
        ax.text(x, 41.5, f"{within[key]:.0f}%", ha="center", va="bottom", fontsize=15, color=color, fontweight="semibold", fontfamily=["Fraunces", "DejaVu Serif"])
        ax.text(x, 40.3, f"within ±{band:g} points", ha="center", va="top", fontsize=8.6, color=INK_2)
        labels: list[list] = []  # outliers named once per task and side, nudged apart
        for (task, d), o in sorted(zip(devs, offsets), key=lambda item: item[0][1]):
            if abs(d) <= 15 or (labels and labels[-1][1] == task and abs(labels[-1][0] - d) < 3):
                continue
            labels.append([d, task, x + o])
        for i in range(1, len(labels)):
            labels[i][0] = max(labels[i][0], labels[i - 1][0] + 1.7)
        for y, task, at in labels:
            ax.annotate(task, (at, y), xytext=(7, 0), textcoords="offset points", va="center", fontsize=8, color=INK_2)
    ax.set_xticks(range(len(setups)), [label for _, label, _ in setups], fontsize=9.5, linespacing=1.25)
    ax.tick_params(axis="x", length=0)
    ax.set_xlim(-0.6, len(setups) - 0.4)
    ax.set_ylim(-35, 48)
    ax.set_yticks(range(-30, 31, 10))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.0f}".replace("-", "\u2212") if v else "0"))
    ax.set_ylabel("Run's score minus the mean of its 5 runs on that task\n(points of hidden tests passed)", labelpad=8)
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    ax.spines["left"].set_bounds(-35, 35)
    others = sorted(within[key] for key in ("sol", "luna", "session"))
    title = (title or "The review loop makes runs more repeatable: {loop:.0f}% land\nwithin {band:g} points of their task's mean, against "
             "{low:.0f}% to {high:.0f}%\nfor single shots on {n} sampled ProgramBench tasks").format(loop=within["loop"], band=band, low=others[0], high=others[-1], n=len(scores))
    ax.set_title(title, loc="left", fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", fontsize=15, color=INK, pad=14, linespacing=1.15)
    fig.tight_layout()
    path = out / name
    fig.savefig(path)
    return path


def repeatability_by_cost(pairs: list[tuple[dict, dict]], cost_parity: dict[str, dict], out: Path, band: float = 5.0) -> Path:
    """Share of runs within ``band`` points of their setup's task mean, against each setup's model cost
    per run as a share of Sol's single-shot cost on the same task; the loop at its first build, at Sol's
    cost and after all its rounds."""
    devs: dict[str, list[float]] = {}
    shares: dict[str, list[float]] = {}
    for parity, match in pairs:
        cp = cost_parity[parity["luna"]]
        n, budget = cp["scored_tests"], cp["budgets_usd"]["promotional"]
        excluded = {r["run"] for r in parity["luna_runs"] if r["excluded"] or (r["promotional"]["workspace"] == "build-1" and r["promotional"]["excluded"])}
        loop_runs = {run: v for run, v in match["runs"].items() if run not in excluded}
        setups = {
            "first": ([v["pass_pct"]["1"] for v in loop_runs.values()], [v["cumulative_usd"][0] for v in loop_runs.values()]),
            "session": ([100 * r["passed"] / n for r in parity["sol_runs"]], [r["cost_usd"] for r in parity["sol_runs"]]),
            "sol": ([100 * r["passed"] / n for r in cp["sol_runs"]], [r["cost_usd"] for r in cp["sol_runs"]]),
            "parity": ([100 * r["promotional"]["passed"] / n for r in cp["luna_runs"]], [r["promotional"]["cost_usd"] for r in cp["luna_runs"]]),
            "final": ([v["pass_pct"][str(len(v["cumulative_usd"]))] for v in match["runs"].values()], [v["cumulative_usd"][-1] for v in match["runs"].values()]),
        }
        for key, (values, costs) in setups.items():
            mean = statistics.fmean(values)
            devs.setdefault(key, []).extend(x - mean for x in values)
            shares.setdefault(key, []).append(statistics.fmean(costs) / budget)
    within = {key: 100 * sum(abs(d) <= band for d in values) / len(values) for key, values in devs.items()}
    cost = {key: statistics.median(values) for key, values in shares.items()}
    fig, ax = plt.subplots(figsize=(9.6, 6.4), dpi=200)
    ax.axvline(1, color=MUTED, lw=0.9, ls=(0, (3, 3)), zorder=1)
    ax.text(1.03, 52.6, "Sol's single-shot cost", fontsize=8.6, color=INK_2, va="bottom")
    loop = ("first", "parity", "final")
    ax.plot([cost[k] for k in loop], [within[k] for k in loop], color=RUST, lw=1.4, ls=(0, (2, 3)), zorder=2)  # measured at the markers only
    ax.scatter([cost[k] for k in loop], [within[k] for k in loop], s=70, color=RUST, marker="D", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    ax.scatter([cost["sol"]], [within["sol"]], s=80, color=INK, marker="o", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    ax.scatter([cost["session"]], [within["session"]], s=70, color=MUTED, marker="s", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    notes = (
        ("first", "GPT-5.6 Luna's loop after its first build (= Luna's single shot)", (12, -9), "left", RUST),
        ("parity", "Luna's loop at Sol's cost", (-12, 10), "right", RUST),
        ("final", "after all its rounds", (10, -4), "left", RUST),
        ("sol", "GPT-5.6 Sol, single shot", (-12, 0), "right", INK),
        ("session", "Luna, single shot with both\nroles in its prompt", (10, -20), "left", MUTED),
    )
    for key, text, offset, ha, color in notes:
        ax.annotate(f"{text}: {within[key]:.0f}%", (cost[key], within[key]), xytext=offset, textcoords="offset points", ha=ha, va="center", fontsize=9, color=color, linespacing=1.2)
    ax.annotate("", xy=(1.0, within["parity"] - 0.6), xytext=(1.0, within["sol"] + 0.6), arrowprops={"arrowstyle": "-|>", "color": RUST, "lw": 1.2}, zorder=3)
    ax.set_xscale("log")
    ax.set_xlim(0.03, 3)
    ax.set_xticks([0.05, 0.1, 0.25, 0.5, 1, 2])
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{100 * v:g}%"))
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_ylim(52, 92)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_xlabel("Model cost per run, as a share of Sol's single-shot cost on the same task (median over tasks, log scale)")
    ax.set_ylabel(f"Runs within {band:g} points of their task's mean")
    ax.grid(color=HAIRLINE, lw=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    title = (f"At the same model cost, the review loop's runs land closer\ntogether than a single-shot large model's: {within['parity']:.0f}% within {band:g} points\n"
             f"of their task's mean, against {within['sol']:.0f}% ({len(pairs)} sampled ProgramBench tasks)")
    ax.set_title(title, loc="left", fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", fontsize=14, color=INK, pad=12, linespacing=1.15)
    fig.tight_layout()
    path = out / "repeatability-cost.png"
    fig.savefig(path)
    return path


def performance_by_cost(pairs: list[tuple[dict, dict]], cost_parity: dict[str, dict], out: Path, title: str | None = None,
                        name: str = "performance-cost.png") -> Path:
    """Mean score over the tasks against each setup's model cost per run as a share of Sol's single-shot
    cost on the same task; the loop after its first build, at Sol's cost and after all its rounds."""
    shares: dict[str, list[float]] = {}
    per_task: dict[str, list[float]] = {}
    for parity, match in pairs:
        cp = cost_parity[parity["luna"]]
        n, budget = cp["scored_tests"], cp["budgets_usd"]["promotional"]
        excluded = {r["run"] for r in parity["luna_runs"] if r["excluded"] or (r["promotional"]["workspace"] == "build-1" and r["promotional"]["excluded"])}
        loop_runs = {run: v for run, v in match["runs"].items() if run not in excluded}
        setups = {
            "first": ([v["pass_pct"]["1"] for v in loop_runs.values()], [v["cumulative_usd"][0] for v in loop_runs.values()]),
            "session": ([100 * r["passed"] / n for r in parity["sol_runs"]], [r["cost_usd"] for r in parity["sol_runs"]]),
            "sol": ([100 * r["passed"] / n for r in cp["sol_runs"]], [r["cost_usd"] for r in cp["sol_runs"]]),
            "parity": ([100 * r["promotional"]["passed"] / n for r in cp["luna_runs"]], [r["promotional"]["cost_usd"] for r in cp["luna_runs"]]),
            "final": ([v["pass_pct"][str(len(v["cumulative_usd"]))] for v in match["runs"].values()], [v["cumulative_usd"][-1] for v in match["runs"].values()]),
        }
        for key, (values, costs) in setups.items():
            per_task.setdefault(key, []).append(statistics.fmean(values))
            shares.setdefault(key, []).append(statistics.fmean(costs) / budget)
    mean = {key: statistics.fmean(values) for key, values in per_task.items()}
    cost = {key: statistics.median(values) for key, values in shares.items()}
    beats = lambda a, b: sum(x > y for x, y in zip(per_task[a], per_task[b]))
    n_tasks = len(pairs)
    fig, ax = plt.subplots(figsize=(9.6, 6.4), dpi=200)
    ax.axvline(1, color=MUTED, lw=0.9, ls=(0, (3, 3)), zorder=1)
    ax.text(1.03, 46.3, "Sol's single-shot cost", fontsize=8.6, color=INK_2, va="bottom")
    loop = ("first", "parity", "final")
    ax.plot([cost[k] for k in loop], [mean[k] for k in loop], color=RUST, lw=1.4, ls=(0, (2, 3)), zorder=2)  # measured at the markers only
    ax.scatter([cost[k] for k in loop], [mean[k] for k in loop], s=70, color=RUST, marker="D", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    ax.scatter([cost["sol"]], [mean["sol"]], s=80, color=INK, marker="o", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    ax.scatter([cost["session"]], [mean["session"]], s=70, color=MUTED, marker="s", edgecolor=CANVAS, linewidth=1.2, zorder=4)
    notes = (
        ("first", "GPT-5.6 Luna's loop after its first build (= Luna's single shot): {m:.1f}%", (12, -6), "left", RUST),
        ("parity", f"Luna's loop at Sol's cost: {{m:.1f}}%\n(ahead of Sol on {beats('parity', 'sol')} of {n_tasks} tasks)", (-12, 14), "right", RUST),
        ("final", "after all its rounds: {m:.1f}%", (10, -4), "left", RUST),
        ("sol", "GPT-5.6 Sol, single shot: {m:.1f}%", (12, 0), "left", INK),
        ("session", f"Luna, single shot with both roles in its prompt: {{m:.1f}}%\n(ahead of its plain single shot on {beats('session', 'first')} of {n_tasks} tasks)", (10, -20), "left", MUTED),
    )
    for key, text, offset, ha, color in notes:
        ax.annotate(text.format(m=mean[key]), (cost[key], mean[key]), xytext=offset, textcoords="offset points", ha=ha, va="center", fontsize=9, color=color, linespacing=1.2)
    ax.annotate("", xy=(1.0, mean["parity"] - 0.35), xytext=(1.0, mean["sol"] + 0.35), arrowprops={"arrowstyle": "-|>", "color": RUST, "lw": 1.2}, zorder=3)
    ax.set_xscale("log")
    ax.set_xlim(0.03, 3)
    ax.set_xticks([0.05, 0.1, 0.25, 0.5, 1, 2])
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{100 * v:g}%"))
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_ylim(46, 76)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_xlabel("Model cost per run, as a share of Sol's single-shot cost on the same task (median over tasks, log scale)")
    ax.set_ylabel(f"Hidden tests passed (mean over {n_tasks} tasks of 5 runs each)")
    ax.grid(color=HAIRLINE, lw=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    title = (title or "At the same model cost, a small model's review loop outperforms\na single-shot large model in {loop_wins} of {n} sampled ProgramBench tasks;\n"
             "both roles in one prompt leave it at its single-shot level").format(loop_wins=beats("parity", "sol"), n=n_tasks, **mean)
    ax.set_title(title, loc="left", fontfamily=["Fraunces", "DejaVu Serif"], fontweight="semibold", fontsize=14, color=INK, pad=12, linespacing=1.15)
    fig.tight_layout()
    path = out / name
    fig.savefig(path)
    return path


def main(paths: list[Path], out_dir: Path | None = None, single_shot: str = "sol") -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    out = out_dir or root / "figures"
    parity_paths = [p for p in paths if not p.name.endswith(".match.json")]
    pairs = [(json.loads(p.read_text()), json.loads(p.with_name(p.name.removesuffix(".json") + ".match.json").read_text())) for p in parity_paths]
    first = plot_parity.main(parity_paths, out, legend=LEGEND, title=TITLE, name="prompt-study-equal-cost.png")
    load_fonts()
    cost_parity = {o["luna"]: json.loads((root / "figures" / "cost-parity" / f"{o['luna']}.json").read_text()) for o, _ in pairs}
    return [first, equal_score(pairs, out), three_way(pairs, cost_parity, single_shot, out), repeatability(pairs, cost_parity, out),
            repeatability_by_cost(pairs, cost_parity, out), performance_by_cost(pairs, cost_parity, out)]


if __name__ == "__main__":
    args = sys.argv[1:]
    out_dir = Path(args.pop(args.index("--out-dir") + 1)) if "--out-dir" in args else None
    single_shot = args.pop(args.index("--single-shot") + 1) if "--single-shot" in args else "sol"
    args = [a for a in args if a not in ("--out-dir", "--single-shot")]
    for path in main([Path(p) for p in args], out_dir, single_shot):
        print(path)
