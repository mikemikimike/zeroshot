"""Experiment configuration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ROOT

ARMS = ("loop", "single")
# The checker's provider session: a fresh one for every execution, or one kept across the loop's rounds
# (as the builder's always is), so the checker remembers what it rejected and the fixes it asked for.
CHECK_SESSION_SCOPES = ("execution", "node_instance")
# Instructions for the build node (prompts/<name>.md). builder-checker runs the loop's two roles in
# one session (the builder's and checker's prompts verbatim); only a single arm may use it.
BUILD_PROMPTS = ("builder", "builder-checker")
# What a worker-only loop's builder is told every round (prompts/<name>.md): a bare "Continue.", or a
# self-review that asks it to check its own work against the task again and fix what it finds.
FEEDBACK_PROMPTS = ("continue", "self-review")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The agents run in Codex against OpenAI's API; the egress proxy's allowlist holds that one host.
HARNESS, PROVIDER, SECRET_ENV, API_HOST = "codex", "openai", "OPENAI_API_KEY", "api.openai.com"
# Where ProgramBench task images put the reference executable. An experiment may move it out of the
# workspace (task.reference_path), where the agent can run it but not overwrite, move or delete it.
UPSTREAM_REFERENCE = "/workspace/executable"
# The task statement's mentions of the reference, rewritten when it moves. The build target
# (`compile.sh` producing `./executable` in the workspace root) is not one of them.
REFERENCE_MENTIONS = (
    ("The reference `./executable` is for", "The reference `{ref}` is for"),
    ("(`cp ./executable ./executable`)", "(`cp {ref} ./executable`)"),
    ("All information about the reference `./executable` must", "All information about the reference `{ref}` must"),
    ("You MUST NOT decompile `./executable` or use disassemblers", "You MUST NOT decompile `{ref}` or use disassemblers"),
    ("tracing/instrumentation tools on `./executable`", "tracing/instrumentation tools on `{ref}`"),
    ("applies ONLY to the reference `./executable`.", "applies ONLY to the reference `{ref}`."),
    ("The executable is located at `./executable` in the workspace root.", "The executable is located at `{ref}`."),
    ("delegate to the reference `./executable`", "delegate to the reference `{ref}`"),
    ("Do NOT decompile the reference `./executable`", "Do NOT decompile the reference `{ref}`"),
    ("you MUST NOT decompile `./executable` or perform", "you MUST NOT decompile `{ref}` or perform"),
)


@dataclass(frozen=True)
class AttemptSpec:
    index: int
    arm: str

    @property
    def label(self) -> str:
        return f"{self.index:02d}-{self.arm}"


@dataclass(frozen=True)
class Experiment:
    path: Path
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def instance_id(self) -> str:
        return self.raw["task"]["instance_id"]

    @property
    def task_image(self) -> str:
        return self.raw["task"]["image"]

    @property
    def reference_path(self) -> str:
        return self.raw["task"].get("reference_path", UPSTREAM_REFERENCE)

    @property
    def doc_fixes(self) -> list[dict[str, str]]:
        """Exact-text corrections to the task's bundled documentation (``file``, ``old``, ``new``)."""
        return self.raw["task"].get("doc_fixes", [])

    @property
    def fidelity_reference(self) -> dict[str, str]:
        """The published submission the smoke test re-scores on this task."""
        return self.raw["task"]["fidelity_reference"]

    @property
    def model(self) -> str:
        return self.raw["model"]["id"]

    @property
    def effort(self) -> str:
        return self.raw["model"]["effort"]

    @property
    def limits(self) -> dict[str, int]:
        return self.raw["limits"]

    @property
    def resources(self) -> dict[str, Any]:
        return self.raw["resources"]

    @property
    def reap_orphans(self) -> bool:
        """Whether `codex` runs under agent/codex-reaper.c, which reaps the processes orphaned below
        Codex (Zeroshot 10.8.0's controller leaves those from other process groups as zombies)."""
        return self.raw["resources"].get("reap_orphans", False)

    @property
    def build_prompt(self) -> str:
        """The prompt file whose text is the build node's instructions."""
        return self.raw.get("prompts", {}).get("build", "builder")

    @property
    def max_iterations(self) -> int:
        return self.raw["arms"]["loop"]["max_iterations"]

    @property
    def worker_only(self) -> bool:
        """A loop arm without its checker: the build node alone, repeated in its own session."""
        return bool(self.raw["arms"].get("loop", {}).get("worker_only", False))

    @property
    def feedback_prompt(self) -> str | None:
        """The prompt file whose text a worker-only loop's builder receives as feedback every round
        (the graph writes none)."""
        return self.raw.get("prompts", {}).get("feedback")

    @property
    def check_session_scope(self) -> str:
        """``execution`` (a fresh checker session every round) or ``node_instance`` (one session kept
        across the loop's rounds); the runtime plan's ``sessionScope`` for the check node."""
        return self.raw["arms"].get("loop", {}).get("check_session_scope", "execution")

    @property
    def pricing(self) -> dict[str, Any]:
        return self.raw["pricing"]

    def attempts(self) -> list[AttemptSpec]:
        return [AttemptSpec(index + 1, arm) for index, arm in enumerate(self.raw["order"])]

    def digest(self) -> str:
        """Hash of the experiment config and every file of the benchmark that shapes a run."""
        h = hashlib.sha256()
        h.update(json.dumps(self.raw, sort_keys=True).encode())
        for path in code_files():
            h.update(str(path.relative_to(ROOT)).encode())
            h.update(path.read_bytes())
        return h.hexdigest()


def code_digest() -> str:
    """Hash of every benchmark file that shapes a run or its scoring (without an experiment)."""
    h = hashlib.sha256()
    for path in code_files():
        h.update(str(path.relative_to(ROOT)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def code_files() -> list[Path]:
    """Files that shape a run: everything in the benchmark except results, tests and hidden files."""
    skip = {"results", "tests", "__pycache__"}
    return sorted(
        p for p in ROOT.rglob("*")
        if p.is_file() and not skip & set(p.relative_to(ROOT).parts) and not any(part.startswith(".") for part in p.relative_to(ROOT).parts) and p.suffix != ".pyc"
    )


def load(path: str | Path) -> Experiment:
    path = Path(path)
    if not path.is_absolute() and not path.exists():
        path = ROOT / path
    raw = json.loads(path.read_text())
    _validate(raw)
    return Experiment(path.resolve(), raw)


def _validate(raw: dict[str, Any]) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", raw["id"]):
        raise ValueError("experiment id must be lowercase letters, digits, dots and dashes")
    if "@sha256:" not in raw["task"]["image"]:
        raise ValueError("task image must be pinned by digest")
    reference = raw["task"].get("reference_path", UPSTREAM_REFERENCE)
    if not reference.startswith("/") or (reference != UPSTREAM_REFERENCE and (reference + "/").startswith("/workspace/")):
        raise ValueError("task.reference_path must be absolute and, when moved, outside /workspace")
    for fix in raw["task"].get("doc_fixes", []):
        if set(fix) != {"file", "old", "new"} or fix["file"].startswith("/") or ".." in Path(fix["file"]).parts or not fix["old"] or fix["old"] == fix["new"]:
            raise ValueError(f"invalid doc fix: {fix}")
    fidelity = raw["task"].get("fidelity_reference")
    if not isinstance(fidelity, dict) or (
        set(fidelity) != {"submission", "commit", "archive_url", "archive_sha256", "registry_commit"}
        or not fidelity["archive_url"].endswith(f"/{raw['task']['instance_id']}/submission.tar.gz")
        or not re.fullmatch(r"[0-9a-f]{64}", fidelity["archive_sha256"])
    ):
        raise ValueError("task.fidelity_reference must pin a published submission archive of this task by sha256")
    if raw["model"]["effort"] not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    if set(raw["model"]) != {"id", "effort"}:
        raise ValueError("model takes an id and an effort; the agents always run in Codex against OpenAI's API")
    if not isinstance(raw["resources"].get("reap_orphans", False), bool):
        raise ValueError("resources.reap_orphans is true or false")
    for key in ("concurrency", "cpus", "memory"):
        if key not in raw["resources"]:
            raise ValueError(f"resources.{key} is required")
    for key in ("workers", "docker_cpus"):
        if key not in raw["eval"]:
            raise ValueError(f"eval.{key} is required")
    arms = raw["arms"]
    prompts = raw.get("prompts", {})
    if set(prompts) - {"build", "feedback"} or prompts.get("build", "builder") not in BUILD_PROMPTS or (prompts.get("build", "builder") != "builder" and "loop" in arms):
        raise ValueError(f"prompts.build must be one of {BUILD_PROMPTS}, and only a single-arm experiment may change it")
    worker_only = bool(arms.get("loop", {}).get("worker_only", False))
    if worker_only != ("feedback" in prompts) or (worker_only and prompts["feedback"] not in FEEDBACK_PROMPTS):
        raise ValueError(f"a worker-only loop names the feedback its builder receives every round, prompts.feedback, one of {FEEDBACK_PROMPTS}; no other experiment does")
    if set(arms) - set(ARMS):
        raise ValueError(f"unknown arms: {set(arms) - set(ARMS)}")
    if "loop" in arms and (set(arms["loop"]) - {"repeats", "max_iterations", "check_session_scope", "worker_only"} or arms["loop"].get("check_session_scope", "execution") not in CHECK_SESSION_SCOPES):
        raise ValueError(f"arms.loop takes repeats, max_iterations, check_session_scope (one of {CHECK_SESSION_SCOPES}) and worker_only")
    if "loop" in arms and (not isinstance(arms["loop"].get("worker_only", False), bool) or (arms["loop"].get("worker_only") and "check_session_scope" in arms["loop"])):
        raise ValueError("arms.loop.worker_only is true or false, and a loop without a checker has no checker session")
    if "single" in arms and set(arms["single"]) != {"repeats"}:
        raise ValueError("arms.single takes repeats only")
    order = raw["order"]
    for arm in ARMS:
        if arm in arms and order.count(arm) != arms[arm]["repeats"]:
            raise ValueError(f"order lists {order.count(arm)} {arm} runs but repeats is {arms[arm]['repeats']}")
    if set(order) - set(arms):
        raise ValueError("order names an arm that is not configured")
    limits = raw["limits"]
    for key in ("attempt_seconds", "build_timeout_ms", "check_timeout_ms"):
        if int(limits[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    if "loop" in arms and not 1 <= arms["loop"]["max_iterations"] <= 100:
        raise ValueError("max_iterations must be between 1 and 100")
    schedule = raw["eval"].get("rounds")
    if schedule is not None and (not schedule or schedule[0] != 1 or schedule != sorted(set(schedule)) or not all(isinstance(r, int) and r >= 1 for r in schedule)):
        raise ValueError("eval.rounds must be an ascending list of distinct round numbers starting at 1 (build 1 is always scored)")
    prices = raw["pricing"]["usd_per_million_tokens"]
    for key in ("input", "cached_input", "cache_write", "output"):
        if float(prices[key]) < 0:
            raise ValueError("prices must be non-negative")


def pins() -> dict[str, Any]:
    return json.loads((ROOT / "pins.json").read_text())


def prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text().strip()


def node_of(first_prompt: str) -> str:
    """The graph node a session ran, from its first node prompt. The builder-checker prompt quotes
    both roles' instructions and is the single arm's build node, so it is matched first."""
    if prompt("builder-checker") in first_prompt:
        return "build"
    if prompt("checker") in first_prompt:
        return "check"
    if prompt("builder") in first_prompt:
        return "build"
    return "other"


def task_statement(exp: Experiment) -> str:
    """The task statement, pointing at the reference wherever the experiment puts it."""
    text = prompt("task")
    if exp.reference_path == UPSTREAM_REFERENCE:
        return text
    for old, new in REFERENCE_MENTIONS:
        if text.count(old) != 1:
            raise ValueError(f"task statement does not contain {old!r} exactly once")
        text = text.replace(old, new.format(ref=exp.reference_path))
    return text
