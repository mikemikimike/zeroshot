# Issue audit

A GitHub Action that checks whether new issues are ready for implementation. A coding agent (Claude
Code or Codex, on your own credentials) reads the issue and the repository. It then either labels the
issue `ready` or labels it `needs-info` and asks the reporter a few focused questions, each with a
suggested default. When the reporter answers, the issue is audited again.

It applies the same task-shaping rules as the Zeroshot operator skill: observable success, material
constraints, verifiability, and only questions that change the outcome. It runs no Zeroshot graph and
needs no Zeroshot installation.

## Usage

```yaml
name: Issue audit

on:
  issues:
    types: [opened, reopened, edited, labeled]
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  group: issue-audit-${{ github.event.issue.number }}

jobs:
  audit:
    if: ${{ !github.event.issue.pull_request && github.event.sender.type != 'Bot' }}
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      contents: read
      issues: write
    steps:
      - uses: the-open-engine/zeroshot/.github/actions/issue-audit@main
        with:
          claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Pin `@main` to a commit SHA for reproducible behavior. For Codex, set `harness: codex` and
`openai-api-key`. Create a Claude Code OAuth token with `claude setup-token`, or pass
`anthropic-api-key` instead.

## Which issues are audited

Every event is checked before any credential is used or any model is called. Skipped events cost a
few API reads.

| `trigger`                    | Audited automatically                                   | Otherwise                                |
| ---------------------------- | ------------------------------------------------------- | ---------------------------------------- |
| `trusted`                    | Issues opened by authors with at least `min-permission` | Never                                    |
| `label`                      | Issues where someone with Triage access applied `audit` | Never                                    |
| `trusted-or-label` (default) | Issues from trusted authors                             | Waits until a maintainer applies `audit` |

`min-permission` (default `write`) is checked against the author's actual repository role, not the
event's author association, so private organization members are recognized.

- **Follow-ups:** after a `needs-info` verdict, a comment or edit by the issue author starts the next
  round. Comments from anyone else do not. After `max-rounds` (default 3) automatic rounds, only a
  maintainer can restart.
- **Command:** a comment starting with `/audit` from someone with `min-permission` audits again,
  regardless of the round limit.
- **Ignored:** pull requests, closed issues, and events sent by bots.

## Security

- The agent runs read-only: Claude Code gets only `Read`, `Grep` and `Glob`; Codex runs in its
  read-only sandbox. Checkout does not persist Git credentials.
- The agent never receives the GitHub token or Actions tokens. It returns a structured verdict, and a
  separate fixed step validates it and writes the labels and comment.
- Issue text is passed as untrusted data. The verdict is bounded and sanitized before posting:
  hidden HTML comments and images are removed, and `@` mentions cannot notify anyone.
- `max-budget-usd` (Claude Code only) and `timeout-minutes` bound each audit's cost and time.

On public repositories anyone can open issues, so keep the default `trusted-or-label` or `label`
trigger.

## Inputs

See [`action.yml`](action.yml) for every input and its default. Harness versions default to the
versions pinned in the Zeroshot target image.
