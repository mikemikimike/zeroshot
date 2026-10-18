#!/usr/bin/env bash
# Runs the audit agent read-only. The agent never receives a GitHub or Actions credential.
set -euo pipefail

work="$RUNNER_TEMP/issue-audit"
schema="$GITHUB_ACTION_PATH/verdict.schema.json"
bin="$work/harness/bin"
limit="${AUDIT_TIMEOUT_MINUTES:-10}m"
model_args=()

agent_env=(env -u GITHUB_TOKEN -u GH_TOKEN -u ACTIONS_RUNTIME_TOKEN -u ACTIONS_RESULTS_URL
  -u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_URL)

case "$AUDIT_HARNESS" in
  claude)
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && agent_env+=(-u ANTHROPIC_API_KEY)
    [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && agent_env+=(-u CLAUDE_CODE_OAUTH_TOKEN)
    [ -n "${AUDIT_MODEL:-}" ] && model_args=(--model "$AUDIT_MODEL")
    "${agent_env[@]}" -u OPENAI_API_KEY timeout "$limit" "$bin/claude" -p \
      --output-format json \
      --json-schema "$(cat "$schema")" \
      --permission-mode dontAsk \
      --no-session-persistence \
      --setting-sources project \
      --strict-mcp-config \
      --max-budget-usd "${AUDIT_BUDGET:-1}" \
      "${model_args[@]}" \
      --tools Read Grep Glob \
      <"$work/prompt.md" >"$work/result.json"
    ;;
  codex)
    export CODEX_HOME="$work/codex-home"
    install -d -m 700 "$CODEX_HOME"
    printenv OPENAI_API_KEY | "$bin/codex" login --with-api-key >/dev/null
    [ -n "${AUDIT_MODEL:-}" ] && model_args=(--model "$AUDIT_MODEL")
    "${agent_env[@]}" -u OPENAI_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN -u ANTHROPIC_API_KEY \
      timeout "$limit" "$bin/codex" exec \
      --sandbox read-only \
      --ephemeral \
      --skip-git-repo-check \
      --ignore-user-config \
      --output-schema "$schema" \
      --output-last-message "$work/result.json" \
      "${model_args[@]}" \
      - <"$work/prompt.md" >/dev/null
    ;;
  *)
    echo "::error::harness must be claude or codex" >&2
    exit 1
    ;;
esac
