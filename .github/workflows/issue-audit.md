---
description: >-
  Audits issues for implementation readiness: labels them zeroshot:ready, or asks the reporter
  focused questions with suggested defaults and labels them zeroshot:needs-info.
on:
  issues:
    types: [opened, reopened, labeled]
    names: ['zeroshot:audit']
  # Checks the actor's actual repository role: the issue author for new issues, or whoever applied
  # the zeroshot:audit label. Replies from reporters are handled by issue-audit-followup.md.
  roles: [admin, maintainer, write]
  reaction: eyes
# Runs only when the ISSUE_AUDIT_ENABLED repository variable is `true`.
if: vars.ISSUE_AUDIT_ENABLED == 'true' && github.event.issue.state == 'open'
permissions:
  contents: read
  issues: read
# Uses the ANTHROPIC_API_KEY secret. For keyless setup with Anthropic workload identity federation,
# see the pull request that added this workflow.
engine: claude
timeout-minutes: 10
max-turns: 30
imports:
  - shared/zeroshot-issue-audit.md
---

Apply the issue audit checklist to issue #${{ github.event.issue.number }}.
