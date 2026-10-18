# Zeroshot issue audit action

A reusable GitHub Action that checks whether a new issue is ready for implementation. A coding agent
(Claude Code or Codex) reads the issue and the repository, then the action either labels the issue
ready or labels it as needing information and asks the reporter a few focused questions. Each
question has a suggested default the reporter can accept. When the reporter answers, the issue is
audited again.

Adopting it does not require installing Zeroshot or running a Zeroshot graph. You only need a short
workflow and your own model credential.

## Audit criteria

The audit uses the task-shaping rules of the Zeroshot operator skill
([`npm/zeroshot/skills/zeroshot/SKILL.md`](../../../npm/zeroshot/skills/zeroshot/SKILL.md), "Shape
the task"). An issue is ready when a separate worker could implement it and an independent verifier
could judge the result:

- the desired behavior and observable success are clear;
- material constraints and behavior that must remain unchanged are known;
- the scope is one useful outcome.

The agent fills gaps from repository evidence and asks only about choices that would materially
change the outcome. It never asks for approval, priorities or timelines.

## Adopt

Add `.github/workflows/issue-audit.yml` to your repository and store the model credential as a
repository secret:

```yaml
name: Issue Audit

on:
  issues:
    types: [opened, edited, labeled]
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  group: issue-audit-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  audit:
    if: ${{ !github.event.issue.pull_request && github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      issues: write
    steps:
      - uses: the-open-engine/zeroshot/.github/actions/issue-audit@<commit-sha>
        with:
          harness: claude-code
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Pin the action to a full commit SHA. For Codex, use `harness: codex` with
`openai-api-key: ${{ secrets.OPENAI_API_KEY }}`. For the Anthropic API, use
`anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}` instead of the OAuth token.

The action checks out the repository's default branch into `.zeroshot-issue-audit/repository`
inside the workspace, so no checkout step is needed. It sets up Node.js 22 for its own scripts. Labels that do not exist yet are created when
first applied.

This repository's own workflow, [`.github/workflows/issue-audit.yml`](../../workflows/issue-audit.yml),
does nothing until the repository variable `ZEROSHOT_ISSUE_AUDIT_ENABLED` is `true`. It reads the
Claude Code OAuth token from the `ZEROSHOT_ISSUE_AUDIT_CLAUDE_CODE_OAUTH_TOKEN` secret.

## Inputs

| Input                     | Default                    | Meaning                                                                                                |
| ------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------ |
| `github-token`            | `github.token`             | Token for the decide and publish steps. Never reaches the agent.                                       |
| `harness`                 | `claude-code`              | `claude-code` or `codex`.                                                                              |
| `model`                   | harness default            | Model identifier, passed to the harness unchanged.                                                     |
| `claude-code-oauth-token` |                            | Claude Code OAuth token. Use this or `anthropic-api-key`.                                              |
| `anthropic-api-key`       |                            | Anthropic API key for `claude-code`.                                                                   |
| `openai-api-key`          |                            | OpenAI API key, required for `codex`.                                                                  |
| `claude-code-version`     | `2.1.276`                  | Exact `@anthropic-ai/claude-code` version.                                                             |
| `codex-version`           | `0.155.0`                  | Exact `@openai/codex` version.                                                                         |
| `trigger-mode`            | `both`                     | `trusted-authors`, `label` or `both`.                                                                  |
| `minimum-author-role`     | `write`                    | Minimum role (`read`, `triage`, `write`, `maintain`, `admin`) of authors whose new issues are audited. |
| `maintainer-role`         | `write`                    | Minimum role (`triage` or higher) to use the audit label or command.                                   |
| `audit-label`             | `audit`                    | Label a maintainer applies to request an audit.                                                        |
| `ready-label`             | `ready-for-implementation` | Label for issues ready for implementation.                                                             |
| `needs-information-label` | `needs-information`        | Label for issues awaiting answers from their author.                                                   |
| `command`                 | `/audit`                   | Maintainer comment command that requests a new audit.                                                  |
| `max-rounds`              | `3`                        | Automatic audit rounds (1–10) before a maintainer must restart.                                        |
| `max-questions`           | `3`                        | Questions per needs-information verdict (1–10).                                                        |
| `timeout-minutes`         | `10`                       | Wall-clock limit for the agent (1–60).                                                                 |
| `max-turns`               | `30`                       | Claude Code turn limit (1–200).                                                                        |
| `max-budget-usd`          | `1`                        | Claude Code spend limit per audit in US dollars.                                                       |

The default harness versions match the versions pinned in
[`docker/zeroshot-target/Dockerfile`](../../../docker/zeroshot-target/Dockerfile). Invalid inputs
fail the job before any audit.

Outputs: `audited`, `reason`, `round` and `verdict`.

## Triggers

The trigger decision runs first, before any model credential is read or any model is called.

| Event                                                                  | Audits when                                                                                                                    |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Issue opened                                                           | `trigger-mode` is `trusted-authors` or `both` and the author's actual repository role is at least `minimum-author-role`.       |
| `audit-label` applied                                                  | `trigger-mode` is `label` or `both` and the person applying it has at least `maintainer-role`.                                 |
| Issue author comments, or edits the issue title or body                | The latest audit asked questions, the issue still has `needs-information-label`, and fewer than `max-rounds` rounds were used. |
| A comment whose first line is `command`, from a `maintainer-role` user | Always, even after the round cap. It starts a new count at round 1.                                                            |

Roles come from the repository collaborator permission API, not from the event's author association.
Pull requests, closed issues and events sent by bots never trigger an audit. An issue opened with
the audit label already applied by a maintainer is audited once, by the label event.

Rounds are counted from the hidden marker at the start of each audit comment. Only bot-authored
comments count, so pasting a marker cannot change the count. Applying the audit label or using the
command restarts at round 1. The comment for the final automatic round says how a maintainer can
restart.

## Results

- **Ready:** the action adds `ready-label`, removes `needs-information-label` and `audit-label`, and
  posts a short summary.
- **Needs information:** the action adds `needs-information-label`, removes `ready-label` and
  `audit-label`, and posts at most `max-questions` questions, each with a suggested default. The
  heading shows the round, such as "round 1 of 3".

## Security model

- **Separated steps.** A deterministic step decides whether to audit and builds the prompt. The agent
  step receives only the model credential. A separate deterministic step validates the verdict and
  performs every GitHub write. The GitHub token is never passed to the agent step.
- **Allowlisted agent environment.** The harness runs with only `PATH`, a private `HOME` and
  `TMPDIR`, locale settings, and the selected credential. Job-level environment variables, the
  GitHub token and runner variables are dropped.
- **Read-only checkout.** The repository is checked out with `persist-credentials: false` and made
  read-only before the agent runs. Claude Code gets only the `Read`, `Grep` and `Glob` tools, no MCP
  servers and no repository settings. Codex runs in its `read-only` sandbox without web search.
- **Untrusted issue text.** Issue titles, bodies and comments are bounded, stripped of hidden
  characters and fenced by a random boundary. The prompt tells the agent to treat them as data.
- **Validated output.** The verdict must match a strict schema: a known verdict, a bounded summary
  and, for needs-information, 1 to `max-questions` questions with suggested defaults. Anything else,
  including output containing a supplied credential or a credential-shaped value, fails the job
  without posting.
- **Sanitized comments.** Before posting, agent text is collapsed to one line per field, bounded,
  stripped of control, bidirectional, zero-width and tag characters, has its Markdown and HTML
  syntax escaped, and has `@` replaced with a full-width `＠`. Agent output cannot mention people,
  format links, embed images or HTML, or hide content in HTML comments.
- **Bounded cost and time.** The agent stops at `timeout-minutes`. Claude Code also stops at
  `max-turns` and `max-budget-usd`. Codex has no spend limit, so the timeout and the bounded prompt
  bound its cost. Rounds are capped by `max-rounds`.

The agent can read files the runner user can read. Use GitHub-hosted runners or dedicated
self-hosted runners without other secrets on disk.
