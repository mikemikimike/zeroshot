---
description: >-
  Re-audits an issue when its author replies while the issue audit waits for answers
  (zeroshot:needs-info).
on:
  issue_comment:
    types: [created]
  # Reporters may have any role; the condition below limits runs to the issue author answering.
  roles: all
# Runs only when ISSUE_AUDIT_ENABLED is `true`, for a reply from the issue author on an open issue
# that is waiting for answers.
if: >-
  vars.ISSUE_AUDIT_ENABLED == 'true' &&
  !github.event.issue.pull_request &&
  github.event.issue.state == 'open' &&
  github.event.comment.user.type != 'Bot' &&
  github.event.comment.user.login == github.event.issue.user.login &&
  contains(github.event.issue.labels.*.name, 'zeroshot:needs-info')
permissions:
  contents: read
  issues: read
# Uses the same engine and credential as issue-audit.md.
engine: claude
timeout-minutes: 10
max-turns: 30
user-rate-limit:
  max-runs-per-window: 5
imports:
  - shared/zeroshot-issue-audit.md
---

The issue author replied to the audit's questions on issue #${{ github.event.issue.number }}. Apply
the issue audit checklist again, taking their answers into account.
