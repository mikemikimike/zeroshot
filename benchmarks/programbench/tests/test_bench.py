"""Unit tests that need neither Docker nor an API key: ``python -m unittest discover -s tests``.

Requires Python 3.12 (the runner image); the ProgramBench-dependent tests also need programbench.
"""

from __future__ import annotations

import copy
import io
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import accounting, audit, config, graphs, images, report  # noqa: E402
from bench.attempt import run_files, snapshot_label  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENT = config.load("experiments/luna-xhigh-svgbob-v1.json")
SELECTION = ROOT / "experiments" / "selection" / "task-draw.json"
EXPERIMENT_KEYS = {"id", "description", "task", "model", "arms", "order", "limits", "resources", "eval", "pricing"}


def _tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _rollout(prompt_text: str, rounds: list[tuple[int, int]], continuation: dict[int, tuple[int, int]] | None = None) -> bytes:
    """A Codex transcript with one graph execution per round, each starting with the node prompt;
    cumulative usage grows by (input, output) per turn. ``continuation`` adds a second Codex turn
    (prompted "Continue", as after a provider error) to the given round."""
    lines = []
    total_in = total_out = 0

    def turn(message: str, tokens_in: int, tokens_out: int) -> None:
        nonlocal total_in, total_out
        total_in += tokens_in
        total_out += tokens_out
        lines.append({"type": "event_msg", "payload": {"type": "task_started"}})
        lines.append({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": message}]}})
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_in // 2, "cache_write_input_tokens": 0, "output_tokens": total_out}}}})
        lines.append({"type": "event_msg", "payload": {"type": "task_complete"}})

    for index, (tokens_in, tokens_out) in enumerate(rounds):
        turn("Execute this graph node\nAuthored instructions:\n" + prompt_text, tokens_in, tokens_out)
        if continuation and index in continuation:
            turn("Continue", *continuation[index])
    return "\n".join(json.dumps(line) for line in lines).encode()


class GraphTests(unittest.TestCase):
    def test_builder_node_is_identical_across_arms(self):
        loop = run_files(EXPERIMENT, "loop")["graph.json"]
        single = run_files(EXPERIMENT, "single")["graph.json"]
        self.assertEqual(loop["root"]["children"][0]["body"]["children"][0], single["root"]["children"][0])

    def test_prompts_are_frozen_generic_text(self):
        loop = run_files(EXPERIMENT, "loop")["graph.json"]
        build, check = loop["root"]["children"][0]["body"]["children"]
        self.assertEqual(build["instructions"], config.prompt("builder"))
        self.assertEqual(check["instructions"], config.prompt("checker"))
        for text in (build["instructions"], check["instructions"]):
            for word in ("ProgramBench", "svgbob", "SVG", "reverse", "executable"):
                self.assertNotIn(word, text)

    def test_prompt_study_runs_both_roles_in_one_build_node(self):
        combined = config.prompt("builder-checker")
        self.assertIn(config.prompt("builder"), combined)  # verbatim, as concatenated as possible
        self.assertIn(config.prompt("checker"), combined)
        for word in ("ProgramBench", "svgbob", "SVG", "reverse", "executable"):
            self.assertNotIn(word, combined)
        for path in sorted(ROOT.glob("experiments/luna-xhigh-*-prompt.json")):
            exp = config.load(path)
            loop = config.load(f"experiments/{exp.id.removesuffix('-prompt')}-v1.json")
            files, plain = run_files(exp, "single"), run_files(loop, "single")
            build = files["graph.json"]["root"]["children"][0]
            self.assertEqual((build["instructions"], build["timeoutMs"]), (combined, 6 * 3600 * 1000))
            plain_build = plain["graph.json"]["root"]["children"][0]
            self.assertEqual({**build, "instructions": None, "timeoutMs": None}, {**plain_build, "instructions": None, "timeoutMs": None})
            self.assertEqual(files["input.json"], plain["input.json"])
            self.assertEqual(files["runtime.json"], plain["runtime.json"])

    def test_loop_stops_on_accept_and_feeds_back_diagnostics(self):
        loop = run_files(EXPERIMENT, "loop")["graph.json"]["root"]["children"][0]
        self.assertEqual(loop["until"]["value"], {"name": "check", "source": "signal", "field": "verdict"})
        self.assertEqual(loop["maxIterations"], EXPERIMENT.max_iterations)
        self.assertEqual(loop["body"]["children"][1]["writeBindings"][0]["target"], ["feedback"])
        self.assertEqual(loop["promotedStatePaths"], [["feedback"]])

    def test_worker_loop_is_the_builder_alone_repeated(self):
        exp = config.load("experiments/luna-xhigh-svgbob-worker-loop.json")
        files, plain = run_files(exp, "loop"), run_files(EXPERIMENT, "loop")
        loop = files["graph.json"]["root"]["children"][0]
        self.assertEqual([c["name"] for c in loop["body"]["children"]], ["build"])
        self.assertEqual(loop["body"]["children"][0], plain["graph.json"]["root"]["children"][0]["body"]["children"][0])  # the same build node
        self.assertEqual((loop["until"], loop["maxIterations"]), (None, 50))
        self.assertEqual(files["graph.json"]["root"]["children"][1], {"kind": "fail", "name": "unverified", "reason": "unverified"})
        self.assertEqual(files["input.json"], {**plain["input.json"], "feedback": "Continue."})
        self.assertEqual(files["graph.json"]["initialInput"], {"kind": "record", "fields": {"feedback": {"type": {"kind": "string"}, "required": True}, "task": {"type": {"kind": "string"}, "required": True}}})
        self.assertEqual(files["runtime.json"], {**plain["runtime.json"], "nodes": {"build": plain["runtime.json"]["nodes"]["build"]}})

    def test_runtime_sessions(self):
        plan = graphs.runtime_plan("loop", "m", "xhigh")
        self.assertEqual(plan["nodes"]["build"]["sessionScope"], "node_instance")
        self.assertEqual(plan["nodes"]["check"]["sessionScope"], "execution")
        self.assertEqual(set(graphs.runtime_plan("single", "m", "xhigh")["nodes"]), {"build"})
        kept = graphs.runtime_plan("loop", "m", "xhigh", "node_instance")
        self.assertEqual(kept["nodes"]["check"]["sessionScope"], "node_instance")
        self.assertEqual({**kept, "nodes": None}, {**plan, "nodes": None})

    def test_task_statement_is_upstream_minus_scaffold(self):
        task = config.prompt("task")
        self.assertIn("Make sure that you have a `./compile.sh` file", task)
        for scaffold in ("COMPLETE_TASK_AND_SUBMIT", "bash tool", "system_information", "helpful assistant"):
            self.assertNotIn(scaffold, task)
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, str(root / "scripts/build_task_prompt.py"), str(root / "prompts/upstream/mini-swe-agent-programbench.yaml"), f"{tmp}/task.md"], check=True)
            self.assertEqual(Path(tmp, "task.md").read_text(), (root / "prompts/task.md").read_text())


class ConfigTests(unittest.TestCase):
    def _load(self, raw):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(raw, f)
        try:
            return config.load(f.name)
        finally:
            os.unlink(f.name)

    def test_order_must_match_repeats(self):
        raw = copy.deepcopy(EXPERIMENT.raw)
        raw["order"] = raw["order"][:-1]
        with self.assertRaises(ValueError):
            self._load(raw)

    def test_task_image_must_be_pinned(self):
        raw = copy.deepcopy(EXPERIMENT.raw)
        raw["task"]["image"] = raw["task"]["image"].split("@")[0]
        with self.assertRaises(ValueError):
            self._load(raw)

    def test_model_takes_an_id_and_an_effort_only(self):
        for model in ({**EXPERIMENT.raw["model"], "harness": "codex"}, {**EXPERIMENT.raw["model"], "provider": "openai"}, {"id": "gpt-5.6-luna", "effort": "ultra"}):
            with self.assertRaises(ValueError, msg=model):
                self._load({**EXPERIMENT.raw, "model": model})
        plan = run_files(EXPERIMENT, "loop")["runtime.json"]
        self.assertEqual((plan["harness"], plan["provider"], plan["nodes"]["build"]["model"]), ("codex", "openai", "gpt-5.6-luna"))

    def test_reference_can_move_out_of_the_workspace_only(self):
        self.assertEqual(EXPERIMENT.reference_path, "/reference/executable")
        self.assertEqual(len(EXPERIMENT.doc_fixes), 4)
        for bad in ("/workspace/ref/executable", "reference/executable"):
            with self.assertRaises(ValueError):
                self._load({**EXPERIMENT.raw, "task": {**EXPERIMENT.raw["task"], "reference_path": bad}})
        with self.assertRaises(ValueError):
            self._load({**EXPERIMENT.raw, "task": {**EXPERIMENT.raw["task"], "doc_fixes": [{"file": "../etc/passwd", "old": "a", "new": "b"}]}})

    def test_task_statement_points_at_the_moved_reference(self):
        upstream = self._load({**EXPERIMENT.raw, "task": {k: v for k, v in EXPERIMENT.raw["task"].items() if k not in ("reference_path", "doc_fixes")}})
        self.assertEqual(config.task_statement(upstream), config.prompt("task"))  # unchanged upstream wording
        moved = config.task_statement(EXPERIMENT)
        self.assertNotIn("reference `./executable`", moved)
        self.assertNotIn("decompile `./executable`", moved)
        self.assertEqual(moved.count("`/reference/executable`"), 9)
        self.assertEqual(moved.count("`./executable`"), 1)  # the build target
        self.assertIn("produces an executable `./executable` in the workspace root", moved)
        self.assertIn("(`cp /reference/executable ./executable`)", moved)

    def test_round_schedule_must_start_at_build_1(self):
        self.assertEqual(EXPERIMENT.max_iterations, 50)
        for bad in ([2, 3], [1, 3, 2], [1, 1, 2], []):
            with self.assertRaises(ValueError, msg=bad):
                self._load({**EXPERIMENT.raw, "eval": {**EXPERIMENT.raw["eval"], "rounds": bad}})

    def test_every_experiment_pins_a_published_submission_of_its_task(self):
        for path in sorted(ROOT.glob("experiments/*.json")):
            exp = config.load(path)
            self.assertTrue(exp.fidelity_reference["archive_url"].endswith(f"/{exp.instance_id}/submission.tar.gz"), exp.id)
        ditaa, svgbob = config.load("experiments/luna-xhigh-ditaa-v1.json"), config.load("experiments/luna-xhigh-svgbob-v1.json")
        wrong_task = {**ditaa.raw["task"]["fidelity_reference"], "archive_url": svgbob.fidelity_reference["archive_url"]}
        for bad in (wrong_task, {**wrong_task, "archive_sha256": "abc"}, {"archive_url": ditaa.fidelity_reference["archive_url"]}):
            with self.assertRaises(ValueError):
                self._load({**ditaa.raw, "task": {**ditaa.raw["task"], "fidelity_reference": bad}})
        with self.assertRaises(ValueError):  # the smoke test's scoring check needs one
            self._load({**ditaa.raw, "task": {k: v for k, v in ditaa.raw["task"].items() if k != "fidelity_reference"}})

    def test_the_rest_of_an_experiment_is_validated(self):
        raw = EXPERIMENT.raw
        prices = raw["pricing"]["usd_per_million_tokens"]
        for bad in (
            {**raw, "id": "Bad Id"},
            {**raw, "arms": {"solo": {"repeats": 5}}, "order": ["solo"] * 5},
            {**raw, "order": ["single"] * 5},
            {**raw, "limits": {**raw["limits"], "attempt_seconds": 0}},
            {**raw, "arms": {"loop": {"repeats": 5, "max_iterations": 101}}},
            {**raw, "pricing": {**raw["pricing"], "usd_per_million_tokens": {**prices, "input": -1}}},
            {**raw, "resources": {k: v for k, v in raw["resources"].items() if k != "cpus"}},
            {**raw, "eval": {k: v for k, v in raw["eval"].items() if k != "workers"}},
        ):
            with self.assertRaises(ValueError, msg=bad):
                self._load(bad)
        self.assertEqual(config.node_of("nothing like a node prompt"), "other")
        self.assertEqual((config.node_of(f"Authored instructions:\n{config.prompt('builder')}"), config.node_of(f"Authored instructions:\n{config.prompt('checker')}")), ("build", "check"))

    def test_every_task_has_a_loop_sol_single_workers_and_a_smoke_test_on_one_protocol(self):
        loops = sorted(ROOT.glob("experiments/luna-xhigh-*-v1.json"))
        self.assertEqual(len(loops), 20)
        reference_luna, reference_sol = config.load("experiments/luna-xhigh-calcurse-v1.json"), config.load("experiments/sol-xhigh-calcurse-single.json")
        for path in loops:
            luna = config.load(path)
            task = luna.id.removeprefix("luna-xhigh-").removesuffix("-v1")
            sol = config.load(f"experiments/sol-xhigh-{task}-single.json")
            smoke = config.load(f"experiments/smoke-{task}.json")
            self.assertEqual((luna.model, luna.effort, sol.model, sol.effort), ("gpt-5.6-luna", "xhigh", "gpt-5.6-sol", "xhigh"))
            self.assertEqual((luna.raw["arms"], luna.raw["order"]), ({"loop": {"repeats": 5, "max_iterations": 50}}, ["loop"] * 5))
            self.assertEqual((sol.raw["arms"], sol.raw["order"]), ({"single": {"repeats": 5}}, ["single"] * 5))
            self.assertEqual(sol.raw["task"], luna.raw["task"])
            self.assertEqual(smoke.raw["task"], luna.raw["task"])
            # The general adjustment on every task: the reference moves. svgbob's documentation is also corrected.
            self.assertEqual(luna.reference_path, "/reference/executable")
            self.assertEqual(len(luna.doc_fixes), 4 if task == "svgbob" else 0)
            for exp, ref in ((luna, reference_luna), (sol, reference_sol)):
                for key in ("arms", "order", "limits", "eval", "pricing", "model"):
                    self.assertEqual(exp.raw[key], ref.raw[key], (exp.id, key))
                self.assertEqual({k: v for k, v in exp.resources.items() if k != "reap_orphans"}, {k: v for k, v in ref.resources.items() if k != "reap_orphans"}, exp.id)
                self.assertEqual(set(exp.raw), EXPERIMENT_KEYS, exp.id)
            self.assertEqual(luna.raw["eval"]["rounds"], [1])  # build 1 and the final workspace; `bench parity` and `bench match` score the rest
            # A single worker is round 1's build: the same node, the same input.
            self.assertEqual(run_files(sol, "single")["input.json"], run_files(luna, "loop")["input.json"])
            self.assertEqual(run_files(sol, "single")["graph.json"]["root"]["children"][0], run_files(luna, "loop")["graph.json"]["root"]["children"][0]["body"]["children"][0])
            self.assertTrue(luna.fidelity_reference["archive_url"].endswith(f"/{luna.instance_id}/submission.tar.gz"))
            self.assertEqual((smoke.raw["arms"], smoke.raw["order"], smoke.raw["eval"]["rounds"]), ({"loop": {"repeats": 1, "max_iterations": 3}}, ["loop"], [1, 2, 3]))
            self.assertEqual((smoke.model, set(smoke.raw)), ("gpt-5.6-luna", EXPERIMENT_KEYS))
        sol_smoke = config.load("experiments/smoke-svgbob-sol.json")
        self.assertEqual((sol_smoke.model, sol_smoke.raw["task"], sol_smoke.raw["pricing"]), ("gpt-5.6-sol", EXPERIMENT.raw["task"], reference_sol.raw["pricing"]))

    def test_the_checker_session_study_changes_only_the_checker_session(self):
        paths = sorted(ROOT.glob("experiments/luna-xhigh-*-checker-resumed.json"))
        self.assertEqual(len(paths), 20)
        for path in paths:
            exp = config.load(path)
            loop = config.load(f"experiments/{exp.id.removesuffix('-checker-resumed')}-v1.json")
            self.assertEqual((exp.check_session_scope, loop.check_session_scope), ("node_instance", "execution"))
            self.assertEqual(set(exp.raw), EXPERIMENT_KEYS)
            for key in ("task", "model", "order", "limits", "resources", "eval", "pricing"):
                self.assertEqual(exp.raw[key], loop.raw[key], key)
            self.assertEqual(exp.raw["arms"], {"loop": {**loop.raw["arms"]["loop"], "check_session_scope": "node_instance"}})
            ours, theirs = run_files(exp, "loop"), run_files(loop, "loop")
            self.assertEqual((ours["graph.json"], ours["input.json"]), (theirs["graph.json"], theirs["input.json"]))  # the same graph, the same input
            self.assertEqual(ours["runtime.json"]["nodes"]["check"]["sessionScope"], "node_instance")
            self.assertEqual(ours["runtime.json"]["nodes"]["build"], theirs["runtime.json"]["nodes"]["build"])
            self.assertEqual({**ours["runtime.json"], "nodes": None}, {**theirs["runtime.json"], "nodes": None})
            self.assertIn(loop.id, exp.raw["description"])
        smoke, plain = config.load("experiments/smoke-svgbob-checker-resumed.json"), config.load("experiments/smoke-svgbob.json")
        self.assertEqual(smoke.check_session_scope, "node_instance")
        self.assertEqual({k: v for k, v in smoke.raw.items() if k not in ("id", "description", "arms")}, {k: v for k, v in plain.raw.items() if k not in ("id", "description", "arms")})
        raw = EXPERIMENT.raw
        for bad in ({**raw, "arms": {"loop": {**raw["arms"]["loop"], "check_session_scope": "fresh"}}}, {**raw, "arms": {"loop": {**raw["arms"]["loop"], "resume_checker": True}}}):
            with self.assertRaises(ValueError, msg=bad):
                self._load(bad)

    def test_the_worker_only_study_removes_the_checker_and_nothing_else(self):
        paths = sorted(ROOT.glob("experiments/luna-xhigh-*-worker-loop.json"))
        self.assertEqual(len(paths), 20)
        for path in paths:
            exp = config.load(path)
            loop = config.load(f"experiments/{exp.id.removesuffix('-worker-loop')}-v1.json")
            self.assertTrue(exp.worker_only and not loop.worker_only)
            self.assertEqual((set(exp.raw), exp.raw["prompts"], exp.feedback), (EXPERIMENT_KEYS | {"prompts"}, {"feedback": "Continue."}, "Continue."))
            for key in ("task", "model", "order", "limits", "resources", "eval", "pricing"):
                self.assertEqual(exp.raw[key], loop.raw[key], key)
            self.assertEqual(exp.raw["arms"], {"loop": {**loop.raw["arms"]["loop"], "worker_only": True}})
            self.assertEqual(run_files(exp, "loop")["input.json"], {**run_files(loop, "loop")["input.json"], "feedback": "Continue."})
            self.assertIn(loop.id, exp.raw["description"])
        smoke, plain = config.load("experiments/smoke-svgbob-worker-loop.json"), config.load("experiments/smoke-svgbob.json")
        self.assertTrue(smoke.worker_only)
        self.assertEqual({k: v for k, v in smoke.raw.items() if k not in ("id", "description", "arms", "prompts")}, {k: v for k, v in plain.raw.items() if k not in ("id", "description", "arms")})
        raw = EXPERIMENT.raw
        for bad in ({**raw, "arms": {"loop": {**raw["arms"]["loop"], "worker_only": "yes"}}},
                    {**raw, "arms": {"loop": {**raw["arms"]["loop"], "worker_only": True, "check_session_scope": "node_instance"}}},
                    {**raw, "prompts": {"feedback": "Continue."}},  # feedback without a worker-only loop
                    {**raw, "arms": {"loop": {**raw["arms"]["loop"], "worker_only": True}}, "prompts": {"feedback": " "}}):
            with self.assertRaises(ValueError, msg=bad):
                self._load(bad)

    def test_keifu_runs_codex_under_the_reaper(self):
        # keifu is a terminal UI: its agents drive it through pseudo-terminals and tmux, which leave
        # orphaned processes behind; agent/codex-reaper.c reaps them and ends what a node leaves running.
        for path in sorted(ROOT.glob("experiments/*.json")):
            exp = config.load(path)
            self.assertEqual(exp.reap_orphans, "keifu" in exp.id, exp.id)
        keifu = config.load("experiments/luna-xhigh-keifu-v1.json")
        self.assertEqual(run_files(keifu, "loop")["graph.json"], run_files(config.load("experiments/luna-xhigh-dust-v1.json"), "loop")["graph.json"])
        with self.assertRaises(ValueError):
            self._load({**keifu.raw, "resources": {**keifu.raw["resources"], "reap_orphans": "yes"}})

    def test_the_sampled_tasks_follow_the_documented_draw(self):
        selection = json.loads(SELECTION.read_text())
        rule = selection["criteria"]
        for task in selection["pool"]:
            self.assertTrue(rule["solx_min"] <= task["solx"] <= rule["solx_max"], task["iid"])
            self.assertGreaterEqual(task["best"] - task["solx"], rule["best_minus_solx_min"], task["iid"])
            self.assertTrue(task["tests"] <= rule["tests_max"] and task["branches"] >= rule["branches_min"], task["iid"])
            self.assertNotIn(task["iid"], rule["excluded"])
        rng, drawn = random.Random(selection["draw"]["seed"]), []
        for language, count in selection["draw"]["quota"].items():
            drawn += rng.sample(sorted(t["iid"] for t in selection["pool"] if t["lang"] == language), count)
        self.assertEqual(drawn, selection["chosen"])
        self.assertEqual(len(set(drawn)), 15)
        by_hand = {"ivanceras__svgbob.6d00ad9", "stathissideris__ditaa.f2286c4", "lfos__calcurse.49180d5", "mgechev__revive.201451e", "facebookresearch__fasttext.1142dc4"}
        self.assertTrue(by_hand <= set(rule["excluded"]))
        loops = {config.load(p).instance_id for p in ROOT.glob("experiments/luna-xhigh-*-v1.json")}
        self.assertEqual(loops, by_hand | set(drawn))

    def test_prompt_study_changes_only_the_build_instructions_and_time_limit(self):
        paths = sorted(ROOT.glob("experiments/luna-xhigh-*-prompt.json"))
        parity_loops = sorted(json.loads(p.read_text())["luna"] for p in ROOT.glob("figures/cost-parity/luna-xhigh-*.json") if not p.name.endswith(".match.json"))
        self.assertEqual(len(paths), 20)
        loops = []
        for path in paths:
            exp = config.load(path)
            loop_id = exp.id.removesuffix("-prompt") + "-v1"
            loops.append(loop_id)
            loop = config.load(f"experiments/{loop_id}.json")
            self.assertEqual((exp.build_prompt, set(exp.raw)), ("builder-checker", EXPERIMENT_KEYS | {"prompts"}))
            self.assertEqual((exp.raw["arms"], exp.raw["order"]), ({"single": {"repeats": 5}}, ["single"] * 5))
            for key in ("task", "model", "pricing", "resources"):
                self.assertEqual(exp.raw[key], loop.raw[key], key)
            self.assertEqual(exp.raw["eval"], {**loop.raw["eval"], "rounds": [1]})
            self.assertEqual(exp.limits, {**loop.limits, "build_timeout_ms": 6 * 3600 * 1000, "attempt_seconds": 6 * 3600 + 900})
            self.assertIn(loop_id, exp.raw["description"])
        self.assertEqual(sorted(loops), parity_loops)  # the 20 loops of the cost-parity study
        raw = config.load(paths[0]).raw
        with self.assertRaises(ValueError):
            self._load({**raw, "prompts": {"build": "task"}})
        with self.assertRaises(ValueError):  # the loop's builder keeps the builder prompt
            self._load({**raw, "arms": {"loop": {"repeats": 5, "max_iterations": 50}}, "order": ["loop"] * 5})

    def test_attempts_reach_their_proxy_by_a_short_alias(self):
        # A container name can exceed a DNS label's 63 characters and then does not resolve.
        long_name = "zsbench-sol-xhigh-ascii-image-converter-single-01-single-net"
        self.assertGreater(len(f"{long_name}-proxy"), 63)
        network = images.Network(long_name, "proxy-image")
        self.assertEqual(network.proxy_url, "http://egress-proxy:8888")
        self.assertLessEqual(len(images.PROXY_ALIAS), 63)
        self.assertIn("HTTPS_PROXY=http://egress-proxy:8888", images.container_env(network))

    def test_parity_workspace_is_the_last_build_within_budget(self):
        from bench import parity

        spend = [0.3, 0.8, 1.5, 2.2]
        self.assertEqual(parity.parity_workspace(spend, 1.6), "build-3")
        self.assertEqual(parity.parity_workspace(spend, 0.8), "build-2")  # at the budget counts as within it
        self.assertEqual(parity.parity_workspace(spend, 0.1), "build-1")  # build 1 alone costs more
        self.assertEqual(parity.parity_workspace(spend, 3.0), "final")  # the run stopped before reaching the budget
        self.assertEqual(parity.parity_workspace([], 1.0), "final")

    def test_parity_costs_add_each_build_and_the_checks_before_it(self):
        from bench import accounting, parity

        pricing = {"usd_per_million_tokens": {"input": 1.0, "cached_input": 0.1, "cache_write": 1.0, "output": 10.0}}
        with tempfile.TemporaryDirectory() as tmp:
            archive = _tar(Path(tmp, "trajectories.tar.gz"), {
                ".codex/sessions/rollout-1-a.jsonl": _rollout(config.prompt("builder"), [(1000, 100), (2000, 200), (3000, 300)]),
                ".codex/sessions/rollout-2-b.jsonl": _rollout(config.prompt("checker"), [(500, 50), (700, 70)]),
            })
            spend = parity.cumulative_costs(archive, pricing)
            sessions = {s["node"]: s["rounds"] for s in accounting.sessions(archive)}
        price = lambda tokens: accounting.cost(tokens, pricing)
        builds, checks = sessions["build"], sessions["check"]
        self.assertEqual(len(spend), 3)
        self.assertAlmostEqual(spend[0], price(builds[0]))
        self.assertAlmostEqual(spend[1] - spend[0], price(builds[1]) + price(checks[0]))
        self.assertAlmostEqual(spend[2] - spend[1], price(builds[2]) + price(checks[1]))

    def test_parity_excludes_incomplete_runs_and_runs_with_disqualifying_audits(self):
        from bench import parity

        clean = {"state": "complete", "commands": {"rule_counts": {"sudo": 2}, "web_search_calls": 0}, "harness_unchanged": True, "codex_config_unchanged": True}
        self.assertEqual(parity._disqualified(clean), [])
        self.assertEqual(parity._disqualified({**clean, "state": "error"}), ["state error"])
        self.assertEqual(parity._disqualified({**clean, "commands": {"rule_counts": {"process_environment_read": 1}, "web_search_calls": 1}}), ["audit: process_environment_read", "audit: web search"])
        self.assertEqual(parity._disqualified({**clean, "harness_unchanged": None}), ["harness files changed or unchecked"])
        self.assertEqual(parity._disqualified({**clean, "codex_config_unchanged": False}), ["Codex config changed"])
        self.assertTrue(set(audit.DISQUALIFYING) <= set(audit.COMMAND_RULES))
        for rule in audit.DISQUALIFYING:
            self.assertEqual(parity._disqualified({**clean, "commands": {"rule_counts": {rule: 1}, "web_search_calls": 0}}), [f"audit: {rule}"])
        score = {"score": 0.5, "passed": 50, "scored_tests": 100, "rerun_plugin_pinned": True}
        self.assertIsNone(parity._score_problem(score, 100, "ab" * 32))
        self.assertEqual(parity._score_problem({"score": None}, 100, None), "not scored")
        self.assertEqual(parity._score_problem({**score, "scored_tests": 99}, 100, None), "scored 99 of 100 tests")
        self.assertEqual(parity._score_problem({**score, "rerun_plugin_pinned": False}, 100, None), "evaluation failed: pinned pytest-rerunfailures was not active")
        self.assertEqual(parity._score_problem({**score, "executable_hash": "ab" * 32}, 100, "ab" * 32), "the workspace builds the reference executable")
        # A workspace that does not compile scores 0 on every test, as on the leaderboard, and stays in.
        self.assertIsNone(parity._score_problem({**score, "score": 0.0, "passed": 0, "error_code": "compile_failed", "rerun_plugin_pinned": None}, 100, None))

    def test_sol_runs_are_the_single_workers_with_their_exclusions(self):
        from bench import parity

        def single(label, passed, cost, **extra):
            return {"label": label, "arm": "single", "state": "complete", "commands": {}, "harness_unchanged": True, "codex_config_unchanged": True,
                    "rounds": {"final": {"score": passed / 100, "passed": passed, "scored_tests": 100, "rerun_plugin_pinned": True}}, "cost_usd": {"total": cost},
                    "reference_copies_in_final": [], **extra}

        attempts = [single("01-single", 50, 3.0), single("02-single", 70, 5.0, reference_copies_in_final=["./executable"]), {**single("03-loop", 90, 9.0), "arm": "loop"}]
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "summary.json").write_text(json.dumps({"attempts": attempts, "expected_scored_tests": 100}))
            runs = parity.sol_runs(Path(tmp))
        self.assertEqual([(r["run"], r["passed"], r["cost_usd"], r["excluded"]) for r in runs], [("01-single", 50, 3.0, []), ("02-single", 70, 5.0, ["the workspace contains the reference executable"])])

    def test_match_finds_the_first_budget_whose_deliverables_reach_sol(self):
        from bench import match

        runs = {"a": [1.0, 2.0, 3.0], "b": [1.5, 2.5, 3.5]}
        scores = {("a", 1): 40.0, ("b", 1): 50.0, ("a", 2): 60.0, ("b", 2): 70.0, ("a", 3): 80.0, ("b", 3): 80.0}
        # At $1.00 run b has no build yet (scores 0); at $1.50: (40 + 50) / 2 = 45; at $2.00: (60 + 50) / 2 = 55.
        self.assertEqual(match.first_match(runs, scores, 45.0), ("reached", 1.5, {"a": 1, "b": 1}))
        self.assertEqual(match.first_match(runs, scores, 55.0), ("reached", 2.0, {"a": 2, "b": 1}))
        self.assertEqual(match.first_match(runs, scores, 90.0)[0], "never")
        # A deliverable that has not been scored stops the search there.
        partial = {k: v for k, v in scores.items() if k != ("b", 2)}
        self.assertEqual(match.first_match(runs, partial, 75.0), ("unknown", 2.5, [("b", 2)]))
        # A later dip does not matter: the first budget that reaches the target counts.
        dip = {**scores, ("a", 3): 10.0}
        self.assertEqual(match.first_match(runs, dip, 65.0), ("reached", 2.5, {"a": 2, "b": 2}))

    def test_parity_verdict_compares_means_and_intervals(self):
        from bench import parity

        self.assertIn("robustly", parity.verdict([50, 52, 51, 53, 52], [40, 41, 42, 40, 41])["verdict"])
        overlapping = parity.verdict([50, 30, 60, 45, 52], [44, 46, 45, 43, 47])
        self.assertEqual(overlapping["verdict"], "Luna outperforms (higher mean; intervals overlap)")
        self.assertTrue(parity.verdict([40, 41, 39, 40, 42], [50, 51, 52, 50, 49])["verdict"].startswith("Sol is ahead robustly"))
        named = parity.verdict([40, 41, 39, 40, 42], [50, 51, 52, 50, 49], ("The loop", "The single session"))
        self.assertTrue(named["verdict"].startswith("The single session is ahead robustly"))

    def test_parallel_runners_only_on_request(self):
        from unittest import mock

        from bench import __main__ as cli

        with mock.patch.object(cli, "docker", return_value="aaaaaaaaaaaa\nbbbbbbbbbbbb\n"), mock.patch.object(cli.socket, "gethostname", return_value="aaaaaaaaaaaa"):
            with mock.patch.dict(os.environ, {"ZSBENCH_ALLOW_PARALLEL": ""}):
                for command in ("run", "eval", "cleanup"):
                    with self.assertRaises(SystemExit):
                        cli._refuse_while_another_runner_runs(command)
            with mock.patch.dict(os.environ, {"ZSBENCH_ALLOW_PARALLEL": "1"}), mock.patch.object(cli, "log"):
                cli._refuse_while_another_runner_runs("run")
                cli._refuse_while_another_runner_runs("eval")
                with self.assertRaises(SystemExit):
                    cli._refuse_while_another_runner_runs("cleanup")  # would remove a live runner's attempts

    def test_the_proxy_allowlist_is_the_model_api_host(self):
        self.assertEqual(images.proxy_filter(), (config.ROOT / "proxy" / "filter").read_bytes())
        self.assertEqual(images.proxy_filter(), b"^api\\.openai\\.com$\n")

    def test_digest_covers_code_but_not_tests_or_results(self):
        names = {str(p.relative_to(config.ROOT)) for p in config.code_files()}
        self.assertTrue({"bench/attempt.py", "bench/evaluate.py", "requirements.lock", "agent/Dockerfile"} <= names)
        self.assertFalse(any(n.startswith(("tests/", "results/")) or "/." in f"/{n}" for n in names))


class CodexConfigTests(unittest.TestCase):
    def test_rendered_config_keeps_credentials_web_and_memory_out(self):
        import tomllib

        rendered = images.codex_config({"PATH": "/usr/bin", "CARGO_HOME": "/usr/local/cargo", "HOME": "/root"})
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["web_search"], "disabled")
        self.assertFalse(parsed["features"]["shell_snapshot"])
        self.assertFalse(parsed["memories"]["generate_memories"])
        self.assertFalse(parsed["memories"]["use_memories"])
        policy = parsed["shell_environment_policy"]
        self.assertFalse(policy["ignore_default_excludes"])
        self.assertTrue({"*KEY*", "*TOKEN*", "*PROXY*"} <= set(policy["exclude"]))
        self.assertEqual(policy["set"]["CARGO_HOME"], "/usr/local/cargo")
        self.assertEqual(policy["set"]["TMPDIR"], "/tmp")
        self.assertNotIn("HOME", policy["set"])
        self.assertEqual(parsed["projects"]["/workspace"]["trust_level"], "untrusted")


class AccountingTests(unittest.TestCase):
    PRICING = EXPERIMENT.pricing

    def test_cost_splits_cache_reads_and_writes(self):
        tokens = {"inputTokens": 1_000_000, "cacheReadInputTokens": 900_000, "cacheCreationInputTokens": 50_000, "outputTokens": 10_000}
        expected = (50_000 * 0.2 + 900_000 * 0.02 + 50_000 * 0.25 + 10_000 * 1.2) / 1e6
        self.assertAlmostEqual(accounting.cost(tokens, self.PRICING), expected)

    def test_resumed_builder_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tar(Path(tmp, "trajectories.tar.gz"), {
                ".codex/sessions/rollout-a.jsonl": _rollout(config.prompt("builder"), [(1000, 100), (300, 20)]),
                ".codex/sessions/rollout-b.jsonl": _rollout(config.prompt("checker"), [(400, 40)]),
            })
            usage = accounting.usage(Path(tmp))
        self.assertEqual(usage["nodes"]["build"]["inputTokens"], 1300)
        self.assertEqual(usage["nodes"]["check"]["inputTokens"], 400)
        self.assertEqual(usage["nodes"]["total"]["outputTokens"], 160)
        self.assertEqual(usage["first_build_round"]["inputTokens"], 1000)
        self.assertEqual(sorted((s["node"], s["rounds"]) for s in usage["sessions"]), [("build", 2), ("check", 1)])

    def test_a_builder_checker_session_is_the_build_node(self):
        # The combined prompt quotes the checker's instructions; it still runs as the build node.
        with tempfile.TemporaryDirectory() as tmp:
            _tar(Path(tmp, "trajectories.tar.gz"), {".codex/sessions/rollout-a.jsonl": _rollout(config.prompt("builder-checker"), [(1000, 100)])})
            usage = accounting.usage(Path(tmp))
        self.assertEqual([s["node"] for s in usage["sessions"]], ["build"])
        self.assertEqual(usage["first_build_round"]["inputTokens"], 1000)
        self.assertEqual(audit._node_of(f"Authored instructions:\n{config.prompt('builder-checker')}"), "build")
        self.assertEqual(audit._node_of(f"Authored instructions:\n{config.prompt('checker')}"), "check")

    def test_a_continuation_turn_belongs_to_its_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tar(Path(tmp, "trajectories.tar.gz"), {
                ".codex/sessions/rollout-a.jsonl": _rollout(config.prompt("builder"), [(1000, 100), (300, 20)], continuation={0: (4000, 50)}),
            })
            usage = accounting.usage(Path(tmp))
        self.assertEqual(usage["first_build_round"]["inputTokens"], 5000)
        self.assertEqual(usage["nodes"]["build"]["inputTokens"], 5300)
        self.assertEqual(usage["sessions"][0]["rounds"], 2)

    def test_first_build_round_comes_from_the_earliest_builder_thread(self):
        # After a build error Zeroshot starts a new builder thread; only the earliest holds build 1.
        with tempfile.TemporaryDirectory() as tmp:
            _tar(Path(tmp, "trajectories.tar.gz"), {
                ".codex/sessions/rollout-2-b.jsonl": _rollout(config.prompt("builder"), [(700, 70)]),
                ".codex/sessions/rollout-1-a.jsonl": _rollout(config.prompt("builder"), [(1000, 100)]),
            })
            usage = accounting.usage(Path(tmp))
        self.assertEqual(usage["first_build_round"]["inputTokens"], 1000)
        self.assertEqual(usage["nodes"]["build"]["inputTokens"], 1700)

    def test_ledger_view_double_counts_resumed_sessions(self):
        events = [
            {"kind": "node_started", "reference": {"node": "build", "execution": 1}},
            {"kind": "token_usage_observed", "execution": 1, "usage": {"inputTokens": 1000, "outputTokens": 100}},
            {"kind": "node_started", "reference": {"node": "build", "execution": 3}},
            {"kind": "token_usage_observed", "execution": 3, "usage": {"inputTokens": 1300, "outputTokens": 120}},
        ]
        self.assertEqual(accounting.usage_from_events(events)["build"]["inputTokens"], 2300)  # truth: 1300


class ArchiveTests(unittest.TestCase):
    def test_tar_reports_its_own_status_and_archives_must_be_gzip(self):
        from bench import attempt

        original = attempt.docker_to_file
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, "a.tar.gz")

            def fake(stderr: str, content: bytes):
                def docker_to_file(args, path, timeout=None, ok_codes=(0,)):
                    path.write_bytes(content)
                    return stderr
                return docker_to_file

            try:
                attempt.docker_to_file = fake("tar: ./x: file changed as we read it\nzsbench-tar-status=1\n", b"\x1f\x8b rest")
                self.assertEqual(attempt.archive("c", "agent", ["tar"], dest), (1, "tar: ./x: file changed as we read it\n"))
                attempt.docker_to_file = fake("Error response from daemon: container is not running\n", b"")
                with self.assertRaisesRegex(RuntimeError, "tar did not run"):
                    attempt.archive("c", "agent", ["tar"], dest)
                self.assertFalse(dest.exists())
                attempt.docker_to_file = fake("zsbench-tar-status=0\n", b"not gzip")
                with self.assertRaisesRegex(RuntimeError, "not gzip"):
                    attempt.archive("c", "agent", ["tar"], dest)
                self.assertFalse(dest.exists())
            finally:
                attempt.docker_to_file = original


class RerunLimitTests(unittest.TestCase):
    def test_an_attempt_that_errors_twice_is_not_run_a_third_time(self):
        from bench.attempt import Attempt

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            spec = EXPERIMENT.attempts()[0]
            attempt = Attempt(EXPERIMENT, spec, results, "image", "proxy", {})
            attempt.dir.mkdir(parents=True)
            (attempt.dir / "attempt.json").write_text(json.dumps({"state": "error"}))
            self.assertFalse(attempt._rerun_limit_reached())  # first error: re-run once
            earlier = attempt.dir.with_name(f"{attempt.dir.name}.discarded-1")
            earlier.mkdir()
            (earlier / "attempt.json").write_text(json.dumps({"state": "stopped"}))
            self.assertFalse(attempt._rerun_limit_reached())  # an operator stop does not count
            (earlier / "attempt.json").write_text(json.dumps({"state": "error"}))
            self.assertTrue(attempt._rerun_limit_reached())


class SnapshotLabelTests(unittest.TestCase):
    def test_labels_follow_rounds_and_retries(self):
        self.assertEqual(snapshot_label("build", 1, set()), "build-1")
        self.assertEqual(snapshot_label("check", 1, {"build-1"}), "check-1")
        self.assertEqual(snapshot_label("check", 1, {"build-1", "check-1"}), "check-1.2")
        self.assertEqual(snapshot_label("build", 2, {"build-1", "check-1", "check-1.2"}), "build-2")


class AuditTests(unittest.TestCase):
    def test_command_rules(self):
        rules = audit.COMMAND_RULES
        self.assertTrue(rules["reference_binary_analysis"].search("objdump -d ./executable"))
        self.assertTrue(rules["binary_instrumentation"].search("LD_PRELOAD=./dump.so ./executable"))
        self.assertTrue(rules["binary_instrumentation"].search('os.environ["LD_AUDIT"] = "./audit.so"'))
        self.assertTrue(rules["binary_instrumentation"].search("LD_LIBRARY_PATH=./fakelibc ./executable"))
        for harmless in ("./executable < in.txt > out.svg", "unset LD_PRELOAD", "env -u LD_PRELOAD ./executable", "LD_LIBRARY_PATH=/opt/lib cc -o executable main.c"):
            self.assertFalse(rules["binary_instrumentation"].search(harmless), harmless)
        self.assertTrue(rules["reference_binary_moved_or_copied"].search("mv /workspace/executable /tmp/ref"))
        self.assertFalse(rules["reference_binary_moved_or_copied"].search("./executable -s 'x' > out.svg"))
        self.assertTrue(rules["network_fetch"].search("cargo install svgbob_cli"))
        self.assertTrue(rules["model_api_calls"].search("curl https://api.openai.com/v1/responses"))
        self.assertTrue(rules["model_api_calls"].search("curl -s http://localhost:11434/v1/chat/completions"))
        self.assertTrue(rules["credential_probe"].search("echo ${OPENAI_API_KEY:0:8}"))
        self.assertTrue(rules["proxy_usage"].search("HTTPS_PROXY=http://zsbench-x-proxy:8888 curl x"))
        self.assertTrue(rules["proxy_usage"].search("curl http://egress-proxy:8888/x"))
        self.assertTrue(rules["proxy_usage"].search("curl -x 192.0.2.2:8888 https://example.com"))
        self.assertFalse(rules["proxy_usage"].search("curl -s http://localhost:8888/render"))
        self.assertTrue(rules["process_environment_read"].search("cat /proc/123/environ"))
        self.assertTrue(rules["process_environment_read"].search("open(os.path.join('/proc', p, 'environ'))"))
        self.assertFalse(rules["process_environment_read"].search("python3 -c 'import os; print(os.environ)'"))
        for reading in ("ps eww", "  ps auxe", "sudo ps e", "true && ps axe", "x=$(ps e)", "timeout 5 ps e", "watch -n1 ps e", 'os.system("ps eww")', "cat /proc/$PPID/mem", "tr '\\0' '\\n' < /proc/1/environ"):
            self.assertTrue(rules["process_environment_read"].search(reading), reading)
        # A process reading its own /proc entry sees only what it already has (a program started with `env -i`).
        for own in ("env -i A='a b' python3 -c 'print(open(\"/proc/self/environ\",\"rb\").read())'", "tr '\\0' '\\n' </proc/self/environ | grep '^LC_CTYPE='", "cat /proc/thread-self/environ"):
            self.assertFalse(rules["process_environment_read"].search(own), own)
        # A checker's comparison script in which `ps` is a list of subprocess results.
        comparison = "    ps=[]\n    for exe in ['/reference/executable','./executable']:\n        ps.append(run(exe))\n    a,b=ps\n    same=(a.stdout==b.stdout)\n"
        for listing in ("ps -ef", "ps aux | grep svgbob", "ps -efww", "ps -eo pid,cmd", "ps -u agent", comparison, "for ps in groups:\n    each(ps)", "# ps entries"):
            self.assertFalse(rules["process_environment_read"].search(listing), listing)
        self.assertTrue(rules["harness_internals"].search("ls /opt/codex/bin"))
        self.assertTrue(rules["harness_internals"].search("zeroshot list"))
        self.assertTrue(rules["sudo"].search("sudo apt-get install foo"))
        self.assertFalse(rules["sudo"].search("echo pseudo"))

    def test_commands_are_the_scripts_codex_ran(self):
        def completed(script, output=""):
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["/bin/bash", "-lc", script], "aggregated_output": output}}}

        def js(code):
            return {"type": "response_item", "payload": {"type": "custom_tool_call", "input": code}}

        records = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Authored instructions:\n" + config.prompt("builder")}]}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            # Code-mode JavaScript: identifiers such as r2 or strings are not commands.
            js('const r2 = await tools.exec_command({cmd:"./executable --help"}); const strings = [1];'),
            completed("./executable --help", "usage"),
            js('await tools.exec_command({cmd:"strings ./executable | head"})'),
            completed("strings ./executable | head", "strings: ./executable: Permission denied"),
            # Never reported as completed: still audited from the call itself.
            js("await tools.exec_command({cmd:'cat /proc/1/environ'})"),
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "FileChange", "changes": {"/tmp/dump.c": {"type": "add", "content": "long r = ptrace(PTRACE_PEEKTEXT, pid, 0, 0);"}}}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            archive = _tar(Path(tmp, "t.tar.gz"), {".codex/sessions/rollout-a.jsonl": "\n".join(json.dumps(r) for r in records).encode()})
            result = audit.command_audit(archive)
        self.assertEqual(result["commands"], 3)
        self.assertEqual(result["rule_counts"]["reference_binary_analysis"], 1)
        self.assertEqual(result["rule_counts"]["reference_binary_analysis_denied"], 1)
        self.assertEqual(result["rule_counts"]["process_environment_read"], 1)
        self.assertEqual(result["rule_counts"]["binary_instrumentation"], 1)
        self.assertEqual(result["rule_counts_by_round"]["build.round1"]["process_environment_read"], 1)

    def test_loaded_agents_md_is_detected(self):
        # The two records Codex 0.155.0 writes when it loads a workspace AGENTS.md.
        world = {"type": "world_state", "payload": {"full": True, "state": {"agents_md": {"directory": "/workspace", "text": "x"}}}}
        message = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /workspace\n\n<INSTRUCTIONS>x"}]}}
        user_level = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>x"}]}}
        self.assertTrue(audit.loaded_agents_md(world))
        self.assertTrue(audit.loaded_agents_md(message))
        self.assertTrue(audit.loaded_agents_md(user_level))
        self.assertFalse(audit.loaded_agents_md({"type": "world_state", "payload": {"full": True, "state": {"agents_md": {}}}}))

    def test_turns_continue_across_builder_threads(self):
        # A failed build makes Zeroshot start a new builder thread; its first turn is round 2.
        def session(script):
            return "\n".join(json.dumps(r) for r in [
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Authored instructions:\n" + config.prompt("builder")}]}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["/bin/bash", "-lc", script], "aggregated_output": ""}}},
            ]).encode()

        with tempfile.TemporaryDirectory() as tmp:
            archive = _tar(Path(tmp, "t.tar.gz"), {
                ".codex/sessions/rollout-2-b.jsonl": session("zeroshot list"),
                ".codex/sessions/rollout-1-a.jsonl": session("ls"),
            })
            result = audit.command_audit(archive)
        self.assertEqual(result["rule_counts_by_round"], {"build.round2": {"harness_internals": 1}})

    def test_git_objects_scan_tolerates_absolute_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "ws")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.txt").write_text("marker-123")
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-qm", "x"], check=True)
            (repo / ".venv" / "bin").mkdir(parents=True)
            os.symlink("/usr/bin/python3", repo / ".venv" / "bin" / "python3")
            archive = Path(tmp, "ws.tar.gz")
            subprocess.run(["tar", "-czf", str(archive), "-C", str(repo), "."], check=True)
            self.assertIn(b"marker-123", audit._git_objects(archive))

    def test_build_artifacts_are_not_source_edits(self):
        for path in ("__pycache__/svgbob.cpython-310.pyc", "target/release/foo", "src/x.o"):
            self.assertTrue(audit.BUILD_ARTIFACT.search(path), path)
        for path in ("svgbob.py", "src/main.rs", "compile.sh", "targets.txt"):
            self.assertFalse(audit.BUILD_ARTIFACT.search(path), path)

    def test_proxy_log_parsing(self):
        log = "\n".join([
            'CONNECT   Jan 01 00:00:00.430 [1]: Request (file descriptor 4): CONNECT example.com:443 HTTP/1.1',
            'NOTICE    Jan 01 00:00:00.430 [1]: Proxying refused on filtered domain "example.com"',
            'CONNECT   Jan 01 00:00:00.606 [1]: Request (file descriptor 4): CONNECT api.openai.com:443 HTTP/1.1',
            'CONNECT   Jan 01 00:00:00.610 [1]: Established connection to host "api.openai.com" using file descriptor 5.',
        ])
        parsed = audit.proxy_audit(log)
        self.assertEqual(parsed["established"], {"api.openai.com": 1})
        self.assertEqual(parsed["refused"], {"example.com": 1})

    def test_checker_edits_compare_each_check_with_the_snapshot_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            snaps = Path(tmp, "snapshots")
            snaps.mkdir()
            _tar(snaps / "build-1.tar.gz", {"./src/a.rs": b"one", "./.git/HEAD": b"ref"})
            _tar(snaps / "check-1.tar.gz", {"./src/a.rs": b"one", "./.git/HEAD": b"ref2", "./target/x.o": b"obj"})
            _tar(snaps / "build-2.tar.gz", {"./src/a.rs": b"two", "./.git/HEAD": b"ref2"})
            _tar(snaps / "check-2.tar.gz", {"./src/a.rs": b"edited by the checker", "./.git/HEAD": b"ref2"})
            rounds = audit.checker_edits(Path(tmp), ["build-1", "check-1", "build-2", "check-2"])
        self.assertEqual([(r["check"], r["compared_with"]) for r in rounds], [("check-1", "build-1"), ("check-2", "build-2")])
        self.assertEqual((rounds[0]["workspace_changes"], rounds[0]["artifact_changes"], rounds[0]["git_metadata_changes"]), ([], 1, 1))
        self.assertEqual(rounds[1]["workspace_changes"], ["src/a.rs"])

    def test_secret_scan_finds_key_in_archives_nested_archives_and_git_objects(self):
        fake = "sk-test-" + "x" * 40
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp, "results")
                root.mkdir()
                _tar(root / "a.tar.gz", {"inner.txt": f"leaked {fake}".encode()})
                inner = _tar(Path(tmp, "inner.tar.gz"), {"deep.txt": fake.encode()})
                _tar(root / "b.tar.gz", {"nested.tar.gz": inner.read_bytes()})
                repo = Path(tmp, "repo")
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                (repo / "f.txt").write_text(fake)
                subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
                subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], check=True)
                (repo / "f.txt").unlink()
                with tarfile.open(root / "c.tar.gz", "w:gz") as tar:
                    tar.add(repo, arcname=".")
                (root / "clean.txt").write_text("nothing here")
                hits = audit.secret_scan(root)["literal_key_hits"]
        finally:
            if previous is None:
                del os.environ["OPENAI_API_KEY"]
            else:
                os.environ["OPENAI_API_KEY"] = previous
        self.assertIn("a.tar.gz!inner.txt", hits)
        self.assertIn("b.tar.gz!nested.tar.gz!deep.txt", hits)
        self.assertIn("c.tar.gz!(git objects)", hits)
        self.assertFalse(any(h.startswith("clean") for h in hits))


class ReportTests(unittest.TestCase):
    def test_discarded_attempts_are_reported_not_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            discarded = Path(tmp, "attempts", "03-loop.discarded-1700000000")
            discarded.mkdir(parents=True)
            (discarded / "attempt.json").write_text(json.dumps({"label": "03-loop", "state": "error", "error": "RuntimeError: boom", "wall_seconds": 12}))
            entries = report._discarded(Path(tmp), EXPERIMENT.pricing)
        self.assertEqual(entries, [{"directory": "03-loop.discarded-1700000000", "label": "03-loop", "state": "error", "error": "RuntimeError: boom", "force_stopped": None, "wall_seconds": 12, "cost_usd": None}])

    def test_a_missing_codex_home_probe_counts_as_not_checked(self):
        meta = {"home_surfaces": ["./config.toml 00"], "snapshots": {"build-1": {"home_surfaces": ["./config.toml 00"]}, "check-1": {"probe_errors": {"home_surfaces": "x"}}}, "home_surfaces_end": ["./config.toml 00"]}
        self.assertEqual(report.home_changes(meta), ["not checked after check-1"])
        self.assertEqual(report.home_changes({}), ["not checked at the start"])

    def test_files_left_in_codex_home_are_reported(self):
        meta = {"home_surfaces": [], "snapshots": {"build-1": {"home_surfaces": ["./AGENTS.md 0123456789abcdef"]}}, "home_surfaces_end": []}
        self.assertEqual(report.home_changes(meta), ["./AGENTS.md 0123456789abcdef"])

    def test_markdown_lists_runs_arms_and_audits(self):
        attempt = {
            "label": "01-loop", "arm": "loop", "state": "complete", "error": None, "terminal": {"status": "failed", "reason": "unverified"}, "force_stopped": None,
            "builds": 3, "checks": 3, "build_outcomes": ["verified"] * 3, "verdicts": ["rejected"] * 3, "wall_seconds": 600,
            "rounds": {"build-1": {"score": 0.5, "passed": 50, "scored_tests": 100}, "final": {"score": 0.6, "passed": 60, "scored_tests": 100}},
            "score_final": 0.6, "score_first_build": 0.5, "snapshots": {}, "tokens": {"nodes": {}, "sessions": []}, "cost_usd": {"total": 1.5}, "cost_first_build_usd": 0.5,
            "commands": {"rule_counts": {"sudo": 1}, "web_search_calls": 0}, "checker_edits": [], "reference_sha256": "ab" * 32, "harness_unchanged": True, "home_changes": [],
            "reference_available_after_build_1": True, "reference_copies_in_final": [], "reference_copies_in_first_build": [], "codex_config_unchanged": True,
        }
        summary = {
            "experiment": "x", "description": "d", "model": "m", "effort": "xhigh", "expected_scored_tests": 100, "attempts": [attempt], "discarded_attempts": [],
            "arms": report._arms([attempt]), "secrets": {"checked_literal_key": True, "literal_key_hits": []}, "egress": {"established": {"api.openai.com": 3}, "refused": {}},
            "provenance": {"vcs_ref": "abc", "vcs_dirty": "false"}, "scored_by": {"vcs_ref": "abc", "vcs_dirty": "false"},
        }
        text = report.markdown(summary)
        self.assertIn("| 01-loop | loop | complete | 3 | rejected, rejected, rejected | 50.0% | 60.0% | 10 | 1.50 | sudo |", text)
        self.assertIn("| loop | 1 | 1 | 1 | 60.0% | 60.0% | 0 | $1.50 (n=1) |", text)
        self.assertIn("Egress: connected {'api.openai.com': 3}; refused {}.", text)


@unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
class ReaperTests(unittest.TestCase):
    """agent/codex-reaper.c, compiled against a stand-in for Codex."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.codex = self.dir / "codex"
        self.reaper = self.dir / "codex-reaper"
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", f'-DCODEX="{self.codex}"', "-o", str(self.reaper), str(Path(__file__).resolve().parent.parent / "agent" / "codex-reaper.c")], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _codex(self, script: str) -> None:
        self.codex.write_text("#!/bin/sh\n" + script)
        self.codex.chmod(0o755)

    def test_orphans_in_other_sessions_are_reaped_and_the_exit_code_passes_through(self):
        # An orphan in its own session (as a program started in a pseudo-terminal) is adopted by
        # the reaper, not by an outer subreaper, and does not stay a zombie once it exits.
        self._codex(
            "sh -c 'setsid sh -c \"sleep 0.6\" & echo $! > \"$0.orphan\"; exit 0' \"$0\"\n"
            "sleep 0.3\necho \"adopted_by=$(ps -o ppid= -p $(cat \"$0.orphan\") | tr -d ' ')\"\necho \"reaper=$PPID\"\n"
            "sleep 0.7\necho \"zombies=$(ps -o stat= --ppid $PPID | grep -c Z)\"\nexit 7\n"
        )
        result = subprocess.run([str(self.reaper), "exec", "--json"], capture_output=True, text=True, timeout=20)
        out = dict(line.split("=", 1) for line in result.stdout.split())
        self.assertEqual(result.returncode, 7)
        self.assertEqual(out["adopted_by"], out["reaper"])
        self.assertEqual(out["zombies"], "0")

    def test_processes_the_node_leaves_running_end_with_it(self):
        # A detached sleeper and a session that keeps creating orphans (as a task's reference program
        # left running in a tmux server does): both end before the reaper exits, so nothing is handed
        # back to Zeroshot's controller.
        self._codex(
            "setsid sh -c 'sleep 30' & echo $! > \"$0.sleeper\"\n"
            "setsid sh -c 'while :; do sh -c \"sleep 0.05 &\"; sleep 0.02; done' & echo $! > \"$0.spawner\"\n"
            "sleep 0.3\nexit 3\n"
        )
        started = time.time()
        result = subprocess.run([str(self.reaper)], timeout=30)
        self.assertEqual(result.returncode, 3)
        self.assertLess(time.time() - started, 5)
        sessions = {int(Path(f"{self.codex}.{name}").read_text()) for name in ("sleeper", "spawner")}
        left = []
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat.read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[3]) in sessions:  # field 6 of /proc/<pid>/stat: the session id
                left.append(stat.parent.name)
        self.assertEqual(left, [])

    def test_the_reaper_dies_the_way_codex_dies(self):
        self._codex("kill -TERM $$\nsleep 5\n")
        self.assertEqual(subprocess.run([str(self.reaper)], timeout=20).returncode, -signal.SIGTERM)

    def test_codex_dies_with_the_reaper_and_group_signals_reach_codex(self):
        self._codex("echo $$ > \"$(dirname \"$0\")/pid\"\nexec sleep 30\n")
        for kill in (lambda proc: proc.kill(), lambda proc: os.killpg(proc.pid, signal.SIGTERM)):
            (self.dir / "pid").unlink(missing_ok=True)
            proc = subprocess.Popen([str(self.reaper)], start_new_session=True)
            deadline = time.time() + 10
            while not (self.dir / "pid").exists() and time.time() < deadline:
                time.sleep(0.05)
            codex = int((self.dir / "pid").read_text())
            kill(proc)
            proc.wait(timeout=10)
            time.sleep(0.3)
            self.assertFalse(Path(f"/proc/{codex}").exists() and "Z" not in Path(f"/proc/{codex}/stat").read_text().split()[2], "Codex outlived the reaper")


class SecretTests(unittest.TestCase):
    def test_the_key_file_is_loaded_into_this_process_only_and_the_environment_wins(self):
        from unittest import mock

        from bench import util

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "openai.env")
            path.write_text('OPENAI_API_KEY="sk-test-' + "y" * 40 + '"\nOTHER=1\n')
            with mock.patch.dict(os.environ):
                os.environ.pop("OPENAI_API_KEY", None)
                util.load_secret_file(str(path))
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test-" + "y" * 40)
                self.assertNotIn("OTHER", os.environ)
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "from-the-environment"}):
                util.load_secret_file(str(path))
                self.assertEqual(os.environ["OPENAI_API_KEY"], "from-the-environment")
            util.load_secret_file(str(Path(tmp, "missing.env")))  # no file, no error
        self.assertEqual(util.without_secrets({"OPENAI_API_KEY": "x", "PATH": "p"}), {"PATH": "p"})

    def test_a_key_in_the_results_blocks_publication(self):
        from bench import __main__ as cli

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            with self.assertRaises(SystemExit):
                cli._guard_secrets(results, {"secrets": {"literal_key_hits": ["attempts/01-loop/proxy.log"]}})
            self.assertIn("attempts/01-loop/proxy.log", (results / "DO-NOT-PUBLISH.txt").read_text())
            cli._guard_secrets(results, {"secrets": {"literal_key_hits": []}})
            self.assertFalse((results / "DO-NOT-PUBLISH.txt").exists())


class LoopFactsTests(unittest.TestCase):
    def test_loop_facts_come_from_the_summaries(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import loop_facts

        attempt = {"label": "01-loop", "arm": "loop", "state": "complete", "builds": 4, "checks": 4, "terminal": {"status": "succeeded"}, "force_stopped": None,
                   "wall_seconds": 1500, "cost_usd": {"build": 1.0, "check": 0.2, "total": 1.2}}
        self.assertEqual(loop_facts.run_facts(attempt), {"state": "complete", "builds": 4, "checks": 4, "accepted": True, "force_stopped": False, "wall_minutes": 25.0,
                                                         "cost_usd": 1.2, "cost_build_usd": 1.0, "cost_check_usd": 0.2, "usd_per_round": 0.3})
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / "luna-xhigh-t-v1").mkdir()
            (results / "luna-xhigh-t-v1" / "summary.json").write_text(json.dumps({"attempts": [attempt, {**attempt, "label": "02-single", "arm": "single"}]}))
            record = loop_facts.main(results, results / "loops.json", [])
        self.assertEqual(list(record["luna-xhigh-t-v1"]), ["01-loop"])


class WalltimeTests(unittest.TestCase):
    def test_minutes_run_from_submission_to_the_snapshot_after_each_build(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import walltime

        self.assertEqual(walltime.minutes_to_build({"submitted_at": 1000.0, "snapshots": {"build-2": {"started_at": 1300.0}}}, 2), 5.0)
        self.assertIsNone(walltime.minutes_to_build({"submitted_at": 1000.0, "snapshots": {}}, 1))
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)

            def attempt(experiment: str, run: str, builds: int) -> None:
                d = results / experiment / "attempts" / run
                d.mkdir(parents=True)
                (d / "attempt.json").write_text(json.dumps({"submitted_at": 1000.0, "snapshots": {f"build-{k}": {"started_at": 1000.0 + 600 * k} for k in range(1, builds + 1)}}))

            attempt("sol-xhigh-t-single", "01-single", 1)
            attempt("sol-xhigh-t-single", "02-single", 1)
            attempt("luna-xhigh-t-v1", "01-loop", 5)
            parity_dir = results / "luna-xhigh-t-v1" / "parity"
            parity_dir.mkdir()
            comparison = {"luna": "luna-xhigh-t-v1", "sol": "sol-xhigh-t-single", "sol_runs": [{"run": "01-single", "excluded": []}, {"run": "02-single", "excluded": ["state error"]}],
                          "luna_runs": [{"run": "01-loop", "excluded": [], "promotional": {"rounds": 3, "excluded": []}}]}
            (parity_dir / "sol-xhigh-t-single.json").write_text(json.dumps(comparison))
            (parity_dir / "sol-xhigh-t-single.match.json").write_text(json.dumps({"outcome": "reached", "deliverables": {"01-loop": 2}}))
            record = walltime.main(results, results / "walltime.json")
            self.assertEqual(record["tasks"]["t"], {"sol_build1": {"01-single": 10.0}, "loop_parity_build": {"01-loop": 30.0}, "loop_match_build": {"01-loop": 20.0}, "match_deliverables": {"01-loop": 2}})
            (parity_dir / "sol-xhigh-t-single.match.json").write_text(json.dumps({"outcome": "never", "deliverables": {"01-loop": 5}}))
            self.assertIsNone(walltime.main(results, results / "walltime.json")["tasks"]["t"]["loop_match_build"])


class EvalTests(unittest.TestCase):
    def test_a_test_run_stopped_at_the_time_limit_counts_as_failed_tests(self):
        from bench import evaluate

        install = {"step": "install_rerunfailures", "returncode": 0, "command": "pip install -q pytest-rerunfailures==16.4"}
        timeout = {"step": "run_tests", "returncode": -1, "exception_info": "Command timed out after 3600s", "output": ""}
        missing = {"step": "results_read", "returncode": 1, "exception_info": ""}
        finished = {"step": "run_tests", "returncode": 0, "output": "plugins: rerunfailures-16.4, timeout-2.4.0"}
        read = {"step": "results_read", "returncode": 0, "branch": "b"}
        errors = {"a": [{"error_code": "results_read_failed", "error_details": ""}]}
        # A task whose only test branch timed out twice (attempt and retry): no test run finished.
        hung = {"log": [install, timeout, missing, timeout, missing]}
        self.assertTrue(evaluate.test_runs_timed_out(hung))
        self.assertIsNone(evaluate.rerun_plugin_active(hung))
        self.assertIsNone(evaluate.infrastructure_error({"score": 0.0, "test_branch_errors": errors, "missing_results_explained": True, "test_runs_timed_out": True, "rerun_plugin_pinned": None}))
        # One branch finished with the pinned plugin, the other hung.
        mixed = {"log": [install, finished, read, timeout, missing, timeout, missing]}
        self.assertTrue(evaluate.test_runs_timed_out(mixed))
        self.assertTrue(evaluate.rerun_plugin_active(mixed))
        # No results after a test run that ended on its own (a crash, a failed install) stays an
        # infrastructure error, and so does any other branch error.
        crashed = {"log": [install, {**finished, "returncode": 2}, missing]}
        self.assertFalse(evaluate.test_runs_timed_out(crashed))
        self.assertEqual(evaluate.infrastructure_error({"score": 0.0, "test_branch_errors": errors, "test_runs_timed_out": False}), "test branch errors ['a']")
        other = {"a": [{"error_code": "run_tests_failed"}]}
        self.assertEqual(evaluate.infrastructure_error({"score": 0.0, "test_branch_errors": other, "test_runs_timed_out": True}), "test branch errors ['a']")
        self.assertEqual(evaluate.infrastructure_error({"score": 0.5, "rerun_plugin_pinned": False}), "pinned pytest-rerunfailures was not active")

    def test_branches_that_run_no_tests_leave_the_plugin_check_and_count_as_not_passed(self):
        from bench import evaluate

        install = {"step": "install_rerunfailures", "returncode": 0, "command": "pip install -q pytest-rerunfailures==16.4"}
        header = "=== test session starts ===\nplugins: timeout-2.4.0, rerunfailures-16.4\n"
        finished = {"step": "run_tests", "returncode": 1, "output": header + "=== 2 failed, 101 passed, 4 rerun in 3.1s ==="}
        read = {"step": "results_read", "returncode": 0, "branch": "b"}
        missing = {"step": "results_read", "returncode": 1, "exception_info": ""}
        # A branch with nothing to test never starts pytest.
        nothing = {"step": "run_tests", "returncode": 0, "output": "No subcommands found. No tests to run.\n"}
        self.assertTrue(evaluate.rerun_plugin_active({"log": [install, finished, read, nothing, read]}))
        self.assertIsNone(evaluate.rerun_plugin_active({"log": [install, nothing, read]}))
        # A run that ran tests without the pinned plugin still fails the check.
        unpinned = {**finished, "output": "=== test session starts ===\nplugins: rerunfailures-16.6.1\n=== 3 passed in 1s ==="}
        self.assertFalse(evaluate.rerun_plugin_active({"log": [install, finished, read, unpinned, read]}))
        # pytest failed to load one of the evaluation image's own plugins before any test ran, on the
        # attempt and on ProgramBench's retry: the branch's tests count as not passed, as for a timed-out branch.
        crash = {"step": "run_tests", "returncode": 1, "output": "Traceback (most recent call last):\n  File \"/usr/local/lib/python3.10/dist-packages/_pytest/config/__init__.py\", line 1583, in parse\n    self.pluginmanager.load_setuptools_entrypoints(\"pytest11\")\nFailed: Marks cannot be applied to fixtures.\n"}
        plugin_crash = {"log": [install, finished, read, crash, missing, crash, missing]}
        self.assertTrue(evaluate.rerun_plugin_active(plugin_crash))
        self.assertFalse(evaluate.test_runs_timed_out(plugin_crash))
        self.assertTrue(evaluate.missing_results_explained(plugin_crash))
        errors = {"b": [{"error_code": "results_read_failed", "error_details": "Could not find the file /workspace/eval/results.xml"}]}
        scored = {"score": 0.66, "test_branch_errors": errors, "missing_results_explained": True, "rerun_plugin_pinned": True}
        self.assertIsNone(evaluate.infrastructure_error(scored))
        # A crash after tests ran is not a plugin-load failure, and missing results stay an error.
        late = {**crash, "output": header + crash["output"]}
        self.assertFalse(evaluate.missing_results_explained({"log": [install, late, missing]}))
        self.assertEqual(evaluate.infrastructure_error({**scored, "missing_results_explained": False}), "test branch errors ['b']")
        # A score from an evaluation that predates missing_results_explained keeps the timed-out rule.
        self.assertIsNone(evaluate.infrastructure_error({"score": 0.0, "test_branch_errors": errors, "test_runs_timed_out": True}))
        self.assertEqual(evaluate.infrastructure_error({"score": 0.0, "test_branch_errors": errors, "test_runs_timed_out": False}), "test branch errors ['b']")

    def test_targets_skip_discarded_attempts(self):
        from bench import evaluate

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("01-loop", "01-loop.discarded-123"):
                d = Path(tmp, "attempts", name, "snapshots")
                d.mkdir(parents=True)
                _tar(d.parent / "submission.tar.gz", {"a": b"1"})
                _tar(d / "build-1.tar.gz", {"a": b"1"})
            labels = [label for label, _ in evaluate.targets(Path(tmp))]
        self.assertEqual(labels, ["01-loop__final", "01-loop__build-1"])

    def test_archive_identity_survives_copies_and_unreadable_evals_count_as_not_evaluated(self):
        from bench import evaluate

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp, "results")
            archive = _tar(Path(tmp, "a.tar.gz"), {"x": b"1"})
            copy = Path(tmp, "copy.tar.gz")
            copy.write_bytes(archive.read_bytes())
            self.assertEqual(evaluate.archive_id(archive), evaluate.archive_id(copy))
            attempt = results / "attempts" / "01-loop"
            attempt.mkdir(parents=True)
            _tar(attempt / "submission.tar.gz", {"x": b"1"})
            eval_json = results / "evals" / "01-loop__final" / EXPERIMENT.instance_id / f"{EXPERIMENT.instance_id}.eval.json"
            eval_json.parent.mkdir(parents=True)
            eval_json.write_text('{"test_results": [')  # killed mid-write
            original = evaluate.score_eval
            evaluate.score_eval = lambda path, iid, ignores: json.loads(path.read_text())
            try:
                scores = evaluate._scores(EXPERIMENT, results, {})
            finally:
                evaluate.score_eval = original
        self.assertEqual(scores["01-loop__final"]["error_code"], "not_evaluated")

    def test_a_round_schedule_scores_only_scheduled_builds_in_numeric_order(self):
        from bench import evaluate

        with tempfile.TemporaryDirectory() as tmp:
            snaps = Path(tmp, "attempts", "01-loop", "snapshots")
            snaps.mkdir(parents=True)
            for name in ("build-1", "check-1", "build-2", "build-10", "check-10", "build-11"):
                _tar(snaps / f"{name}.tar.gz", {"a": name.encode()})
            _tar(snaps.parent / "submission.tar.gz", {"a": b"final"})
            everything = [label for label, _ in evaluate.targets(Path(tmp))]
            scheduled = [label for label, _ in evaluate.targets(Path(tmp), [1, 2, 10])]
        self.assertEqual(everything, ["01-loop__final", "01-loop__build-1", "01-loop__check-1", "01-loop__build-2", "01-loop__build-10", "01-loop__check-10", "01-loop__build-11"])
        self.assertEqual(scheduled, ["01-loop__final", "01-loop__build-1", "01-loop__build-2", "01-loop__build-10"])

    def test_identical_workspaces_are_evaluated_once(self):
        from bench import evaluate

        def workspace(path: Path, files: dict[str, bytes], mtime: int, mode: int = 0o644) -> Path:
            with tarfile.open(path, "w:gz") as tar:
                for name, data in files.items():
                    info = tarfile.TarInfo(f"./{name}")
                    info.size, info.mtime, info.mode = len(data), mtime, mode
                    tar.addfile(info, io.BytesIO(data))
            return path

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp, "results")
            attempt = results / "attempts" / "01-loop"
            (attempt / "snapshots").mkdir(parents=True)
            workspace(attempt / "snapshots" / "build-1.tar.gz", {"a.py": b"v1"}, mtime=1)
            workspace(attempt / "snapshots" / "check-1.tar.gz", {"a.py": b"v1"}, mtime=2)  # same code, later snapshot
            workspace(attempt / "snapshots" / "build-2.tar.gz", {"a.py": b"v2"}, mtime=3)
            workspace(attempt / "submission.tar.gz", {"a.py": b"v2"}, mtime=4)
            self.assertNotEqual(evaluate.content_key(attempt / "snapshots" / "build-1.tar.gz"), evaluate.content_key(workspace(Path(tmp, "x.tar.gz"), {"a.py": b"v1"}, mtime=1, mode=0o755)))
            calls = []

            def fake_eval(exp, results, run_dirs, force):
                calls.append(sorted(d.name for d in run_dirs))
                for d in run_dirs:
                    (d / exp.instance_id / f"{exp.instance_id}.eval.json").write_text(json.dumps({"from": d.name}))
                return 0

            saved = evaluate._programbench_eval, evaluate.score_eval, evaluate.leaderboard_ignores
            evaluate._programbench_eval = fake_eval
            evaluate.score_eval = lambda path, iid, ignores: {"score": 0.5, "passed": 1, "scored_tests": 2, "rerun_plugin_pinned": True, "from": json.loads(path.read_text())["from"], "tests": {}}
            evaluate.leaderboard_ignores = lambda cache: {}
            try:  # without a round schedule, every snapshot is scored
                unscheduled = config.Experiment(EXPERIMENT.path, {**EXPERIMENT.raw, "eval": {k: v for k, v in EXPERIMENT.raw["eval"].items() if k != "rounds"}})
                scores = evaluate.evaluate(unscheduled, results, Path(tmp))
            finally:
                evaluate._programbench_eval, evaluate.score_eval, evaluate.leaderboard_ignores = saved
        self.assertEqual(calls, [["01-loop__build-1", "01-loop__final"]])  # two distinct workspaces, four archives
        self.assertEqual(scores["01-loop__check-1"]["from"], "01-loop__build-1")
        self.assertEqual(scores["01-loop__check-1"]["evaluated_as"], "01-loop__build-1")
        self.assertEqual(scores["01-loop__build-2"]["from"], "01-loop__final")
        self.assertNotIn("evaluated_as", scores["01-loop__final"])

    def test_rerun_plugin_is_pinned(self):
        try:
            from bench import pbeval
        except ImportError:
            self.skipTest("programbench not installed")
        seen = []
        original = pbeval._run_step
        pbeval._run_step = lambda self, command, **kw: seen.append(command)
        try:
            pbeval._pinned_run_step(None, "pip3 install -q --disable-pip-version-check pytest-rerunfailures", env=None)
        finally:
            pbeval._run_step = original
        self.assertEqual(seen, ["pip3 install -q --disable-pip-version-check pytest-rerunfailures==16.4"])


if __name__ == "__main__":
    unittest.main()
