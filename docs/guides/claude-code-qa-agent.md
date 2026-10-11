---
title: Use Claude Code as a QA agent
description: Run Claude Code through independent acceptance and code review with a bounded repair loop in zeroshot.
---

# Use Claude Code as a QA agent

Define the checks before Claude Code starts writing. In this example, one Claude Code session
implements a health endpoint, a second checks the requested behavior, and a third reviews the code.
If either reviewer rejects, the worker receives both reviews and tries again. Both reviewers must
accept in the same pass; the graph fails after four unsuccessful passes.

The acceptance criteria include starting the service and making an HTTP request. That gives the QA
agent observable behavior to check alongside the tests.

## Prepare the repository

You need [zeroshot installed](../getting-started/install.md), Claude Code installed and signed in,
and a Git repository containing an HTTP service with documented setup, start, and test commands.
This example adds `GET /health`; choose another small change if the service already has it.

The commands below use a POSIX shell. Start from your application's repository and create a separate
worktree for the task:

```sh
git worktree add -b qa-health ../qa-health
cd ../qa-health
```

The new worktree starts from the current commit; it does not include uncommitted changes. Install
the application's dependencies there using its documented setup commands. Local runs use that
worktree and its installed tools. See [runtime environments](runtime-environments.md) for Docker
and Cloud setup.

## Set the acceptance criteria

Create `~/claude-code-qa/` and save these three files there, outside the worktree:

| File                                                                   | Purpose                                     |
| ---------------------------------------------------------------------- | ------------------------------------------- |
| [input.json](../assets/claude-code-qa/input.json)                      | Task and acceptance criteria                |
| [review-loop.graph.json](../assets/review-loop/review-loop.graph.json) | Worker, parallel reviewers, and repair loop |
| [runtime.json](../assets/claude-code-qa/runtime.json)                  | Claude Code sessions for all three agents   |

```json title="input.json"
--8<-- "assets/claude-code-qa/input.json"
```

Before running, adapt this task to your application. Include its exact startup and test commands,
the local URL and port, and any required fixtures or services. Keep credentials out of these files.
For a bug fix, specify the input that reproduces the bug and the expected result.

## Choose the Claude Code model

```json title="runtime.json"
--8<-- "assets/claude-code-qa/runtime.json"
```

Replace every `YOUR_MODEL_ID` with a model identifier your installed Claude Code accepts. The
worker and reviewers can use different models. Local Claude/Anthropic runs reuse Claude Code's
existing login; this example does not require an API key.

The worker continues its own conversation when sent back for repair. Each reviewer starts a fresh
session on every pass. Both reviewers inspect the same worktree, with instructions not to edit it.
Their feedback goes back to the worker through explicit graph state. These are separate sessions,
not separate filesystem sandboxes.

## Validate the graph and input

From the application's `qa-health` worktree, run:

```sh
zeroshot run \
  --title "QA: health endpoint" \
  --graph ~/claude-code-qa/review-loop.graph.json \
  --runtime-config ~/claude-code-qa/runtime.json \
  --input ~/claude-code-qa/input.json \
  --validate-only
```

A valid request returns `{"valid":true}`. This checks the graph, input, and runtime bindings without
starting an agent. It does not check whether the model is available or whether the application can
start.

## Run and inspect the reviews

Remove `--validate-only` to start:

```sh
zeroshot run \
  --title "QA: health endpoint" \
  --graph ~/claude-code-qa/review-loop.graph.json \
  --runtime-config ~/claude-code-qa/runtime.json \
  --input ~/claude-code-qa/input.json
```

Open another terminal and run `zeroshot ui`. Visit `http://127.0.0.1:4173/ui/`, open **Runs**, and
select the task to inspect the node transcripts. The [observation guide](observe-and-control.md)
also covers CLI logs and status.

In the acceptance review, look for the observed HTTP response and test results. A rejection should
identify a failed criterion or missing evidence that the worker can act on. After repair, both
reviewers run again. A successful run means they accepted under the instructions you supplied; it
does not establish coverage beyond those checks.

This graph keeps the changes in your worktree and has no Git delivery step. Review the result there.

## Change what the QA agent checks

The downloadable graph's `acceptance` node checks behavior; its `code` node checks correctness,
safety, integration, and maintainability. Edit their `instructions` to match your repository. For
example, require the acceptance reviewer to reproduce a webhook retry, verify one resulting booking,
and report the database query used as evidence.

[Build a review loop](review-loop.md) explains the graph's feedback bindings and termination rules.
Use that guide when adding another reviewer or changing the retry limit. For a workflow with PR or
merge delivery, start from the built-in `software-change` template described in
[Run a first task](../getting-started/first-run.md).
