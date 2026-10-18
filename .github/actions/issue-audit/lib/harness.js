'use strict';

const { spawn } = require('node:child_process');

const { VerdictError, extractJson } = require('./verdict');

const READ_ONLY_TOOLS = 'Read,Grep,Glob';
const DENIED_TOOLS = 'Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task';
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
const KILL_GRACE_MS = 5000;

function packageSpec(config) {
  return config.harness === 'claude-code'
    ? `@anthropic-ai/claude-code@${config.claudeCodeVersion}`
    : `@openai/codex@${config.codexVersion}`;
}

function binaryName(config) {
  return config.harness === 'claude-code' ? 'claude' : 'codex';
}

/**
 * Builds the complete agent environment from an allowlist. Nothing from the Actions environment
 * reaches the agent except PATH; the GitHub token and runner tokens are never included.
 */
function agentEnv({ path, home, tmp, extra = {} }) {
  return {
    PATH: path,
    HOME: home,
    TMPDIR: tmp,
    LANG: 'C.UTF-8',
    CI: 'true',
    NO_COLOR: '1',
    ...extra,
  };
}

function claudeInvocation({ config, schema }) {
  const args = [
    '--print',
    '--output-format',
    'json',
    '--tools',
    READ_ONLY_TOOLS,
    '--allowedTools',
    READ_ONLY_TOOLS,
    '--disallowedTools',
    DENIED_TOOLS,
    '--setting-sources',
    'user',
    '--strict-mcp-config',
    '--no-session-persistence',
    '--max-turns',
    String(config.maxTurns),
    '--max-budget-usd',
    String(config.maxBudgetUsd),
    '--json-schema',
    JSON.stringify(schema),
  ];
  if (config.model) args.push('--model', config.model);
  return {
    args,
    env: {
      DISABLE_AUTOUPDATER: '1',
      DISABLE_TELEMETRY: '1',
      DISABLE_ERROR_REPORTING: '1',
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    },
  };
}

function codexLoginInvocation() {
  return { args: ['login', '--with-api-key'] };
}

function codexInvocation({ config, schemaPath, outputPath }) {
  const args = [
    'exec',
    '--sandbox',
    'read-only',
    '--skip-git-repo-check',
    '--ephemeral',
    '--ignore-rules',
    '--color',
    'never',
    '-c',
    'shell_environment_policy.inherit="core"',
    '-c',
    'web_search="disabled"',
    '--output-schema',
    schemaPath,
    '--output-last-message',
    outputPath,
  ];
  if (config.model) args.push('--model', config.model);
  args.push('-');
  return { args };
}

/** Parses `claude --print --output-format json` output into a raw verdict value. */
function parseClaudeOutput(stdout) {
  let result;
  try {
    result = JSON.parse(String(stdout).trim());
  } catch {
    throw new VerdictError('Claude Code output is not JSON');
  }
  if (result === null || typeof result !== 'object' || result.type !== 'result') {
    throw new VerdictError('Claude Code output is not a result object');
  }
  if (result.is_error === true || result.subtype !== 'success') {
    throw new VerdictError(`Claude Code did not succeed (${String(result.subtype).slice(0, 64)})`);
  }
  const cost = Number.isFinite(result.total_cost_usd) ? result.total_cost_usd : null;
  const so = result.structured_output;
  if (so !== null && typeof so === 'object' && !Array.isArray(so)) {
    return { value: so, text: JSON.stringify(so), costUsd: cost };
  }
  if (typeof result.result !== 'string') throw new VerdictError('Claude Code returned no result');
  return { value: extractJson(result.result), text: result.result, costUsd: cost };
}

/** Parses the Codex last-message file into a raw verdict value. */
function parseCodexOutput(lastMessage) {
  const text = String(lastMessage ?? '');
  if (text.trim() === '') throw new VerdictError('Codex returned no final message');
  return { value: extractJson(text), text, costUsd: null };
}

/** Runs a process in its own group with a hard deadline and bounded captured output. */
function runProcess(command, args, { cwd, env, input = '', timeoutMs }) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env,
      detached: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const stdout = [];
    const stderr = [];
    let size = 0;
    let timedOut = false;
    let overflowed = false;
    let killTimer = null;

    const killGroup = (signal) => {
      try {
        process.kill(-child.pid, signal);
      } catch {
        // The group already exited.
      }
    };
    const stop = () => {
      killGroup('SIGTERM');
      killTimer = setTimeout(() => killGroup('SIGKILL'), KILL_GRACE_MS);
      killTimer.unref();
    };
    const collect = (chunks) => (chunk) => {
      size += chunk.length;
      if (size > MAX_OUTPUT_BYTES) {
        if (!overflowed) {
          overflowed = true;
          stop();
        }
        return;
      }
      chunks.push(chunk);
    };
    const deadline = setTimeout(() => {
      timedOut = true;
      stop();
    }, timeoutMs);

    child.stdout.on('data', collect(stdout));
    child.stderr.on('data', collect(stderr));
    child.on('error', (error) => {
      clearTimeout(deadline);
      reject(error);
    });
    child.on('close', (code, signal) => {
      clearTimeout(deadline);
      if (killTimer) clearTimeout(killTimer);
      killGroup('SIGKILL');
      resolve({
        code,
        signal,
        timedOut,
        overflowed,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      });
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

module.exports = {
  DENIED_TOOLS,
  READ_ONLY_TOOLS,
  agentEnv,
  binaryName,
  claudeInvocation,
  codexInvocation,
  codexLoginInvocation,
  packageSpec,
  parseClaudeOutput,
  parseCodexOutput,
  runProcess,
};
