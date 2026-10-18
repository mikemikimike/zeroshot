'use strict';

// Defaults must match the harness versions pinned in docker/zeroshot-target/Dockerfile.
const DEFAULT_CLAUDE_CODE_VERSION = '2.1.276';
const DEFAULT_CODEX_VERSION = '0.155.0';

const HARNESSES = ['claude-code', 'codex'];
const TRIGGER_MODES = ['trusted-authors', 'label', 'both'];
const ROLES = ['none', 'read', 'triage', 'write', 'maintain', 'admin'];

const DEFAULTS = Object.freeze({
  harness: 'claude-code',
  model: '',
  'claude-code-version': DEFAULT_CLAUDE_CODE_VERSION,
  'codex-version': DEFAULT_CODEX_VERSION,
  'trigger-mode': 'both',
  'minimum-author-role': 'write',
  'maintainer-role': 'write',
  'audit-label': 'audit',
  'ready-label': 'ready-for-implementation',
  'needs-information-label': 'needs-information',
  command: '/audit',
  'max-rounds': '3',
  'max-questions': '3',
  'timeout-minutes': '10',
  'max-turns': '30',
  'max-budget-usd': '1',
});

const VERSION_PATTERN = /^\d{1,4}\.\d{1,4}\.\d{1,6}(?:-[0-9A-Za-z][0-9A-Za-z.-]{0,63})?$/;
const LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$/;
const COMMAND_PATTERN = /^\/[a-z][a-z0-9-]{0,31}$/;
// Opaque identifier: passed through unchanged, only rejected when it cannot be a single argument.
// eslint-disable-next-line no-control-regex
const MODEL_REJECT_PATTERN = /[\u0000-\u001f\u007f]/;

class ConfigError extends Error {}

function envName(input) {
  return `ISSUE_AUDIT_${input.toUpperCase().replace(/-/g, '_')}`;
}

function readInput(env, input) {
  const raw = env[envName(input)];
  if (raw === undefined || raw.trim() === '') return DEFAULTS[input];
  return raw.trim();
}

function oneOf(input, value, allowed) {
  if (!allowed.includes(value)) {
    throw new ConfigError(
      `${input} must be one of ${allowed.join(', ')}; got ${JSON.stringify(value)}`
    );
  }
  return value;
}

function boundedInteger(input, value, min, max) {
  if (!/^\d{1,4}$/.test(value)) throw new ConfigError(`${input} must be an integer`);
  const parsed = Number(value);
  if (parsed < min || parsed > max) {
    throw new ConfigError(`${input} must be between ${min} and ${max}; got ${parsed}`);
  }
  return parsed;
}

function boundedAmount(input, value, min, max) {
  if (!/^\d{1,3}(?:\.\d{1,2})?$/.test(value)) {
    throw new ConfigError(`${input} must be a decimal amount with at most two decimals`);
  }
  const parsed = Number(value);
  if (parsed < min || parsed > max) {
    throw new ConfigError(`${input} must be between ${min} and ${max}; got ${parsed}`);
  }
  return parsed;
}

function matching(input, value, pattern, description) {
  if (!pattern.test(value)) throw new ConfigError(`${input} must be ${description}`);
  return value;
}

function parseModel(value) {
  if (value === '') return null;
  if (value.length > 200 || MODEL_REJECT_PATTERN.test(value) || value.startsWith('-')) {
    throw new ConfigError('model must be at most 200 characters without control characters');
  }
  return value;
}

function parseConfig(env) {
  const config = {
    harness: oneOf('harness', readInput(env, 'harness'), HARNESSES),
    model: parseModel(readInput(env, 'model')),
    claudeCodeVersion: matching(
      'claude-code-version',
      readInput(env, 'claude-code-version'),
      VERSION_PATTERN,
      'an exact version such as 2.1.276'
    ),
    codexVersion: matching(
      'codex-version',
      readInput(env, 'codex-version'),
      VERSION_PATTERN,
      'an exact version such as 0.155.0'
    ),
    triggerMode: oneOf('trigger-mode', readInput(env, 'trigger-mode'), TRIGGER_MODES),
    minimumAuthorRole: oneOf(
      'minimum-author-role',
      readInput(env, 'minimum-author-role'),
      ROLES.slice(1)
    ),
    maintainerRole: oneOf('maintainer-role', readInput(env, 'maintainer-role'), ROLES.slice(2)),
    auditLabel: matching('audit-label', readInput(env, 'audit-label'), LABEL_PATTERN, 'a label'),
    readyLabel: matching('ready-label', readInput(env, 'ready-label'), LABEL_PATTERN, 'a label'),
    needsInformationLabel: matching(
      'needs-information-label',
      readInput(env, 'needs-information-label'),
      LABEL_PATTERN,
      'a label'
    ),
    command: matching(
      'command',
      readInput(env, 'command'),
      COMMAND_PATTERN,
      'a slash command such as /audit'
    ),
    maxRounds: boundedInteger('max-rounds', readInput(env, 'max-rounds'), 1, 10),
    maxQuestions: boundedInteger('max-questions', readInput(env, 'max-questions'), 1, 10),
    timeoutMinutes: boundedInteger('timeout-minutes', readInput(env, 'timeout-minutes'), 1, 60),
    maxTurns: boundedInteger('max-turns', readInput(env, 'max-turns'), 1, 200),
    maxBudgetUsd: boundedAmount('max-budget-usd', readInput(env, 'max-budget-usd'), 0.01, 100),
  };
  const labels = [config.auditLabel, config.readyLabel, config.needsInformationLabel];
  if (new Set(labels.map((label) => label.toLowerCase())).size !== labels.length) {
    throw new ConfigError('audit-label, ready-label and needs-information-label must differ');
  }
  return config;
}

// Credentials are read only by the agent step, after the trigger decision.
function parseCredentials(harness, env) {
  const value = (name) => (env[name] ?? '').trim();
  if (harness === 'claude-code') {
    const oauthToken = value('ISSUE_AUDIT_CLAUDE_CODE_OAUTH_TOKEN');
    const apiKey = value('ISSUE_AUDIT_ANTHROPIC_API_KEY');
    if (oauthToken && apiKey) {
      throw new ConfigError(
        'provide either claude-code-oauth-token or anthropic-api-key, not both'
      );
    }
    if (oauthToken) return { name: 'CLAUDE_CODE_OAUTH_TOKEN', value: oauthToken };
    if (apiKey) return { name: 'ANTHROPIC_API_KEY', value: apiKey };
    throw new ConfigError('claude-code requires claude-code-oauth-token or anthropic-api-key');
  }
  const apiKey = value('ISSUE_AUDIT_OPENAI_API_KEY');
  if (!apiKey) throw new ConfigError('codex requires openai-api-key');
  return { name: 'OPENAI_API_KEY', value: apiKey };
}

function roleRank(role) {
  const rank = ROLES.indexOf(role);
  return rank === -1 ? 0 : rank;
}

function roleAtLeast(role, minimum) {
  return roleRank(role) >= roleRank(minimum);
}

module.exports = {
  ConfigError,
  DEFAULTS,
  DEFAULT_CLAUDE_CODE_VERSION,
  DEFAULT_CODEX_VERSION,
  ROLES,
  envName,
  parseConfig,
  parseCredentials,
  roleAtLeast,
};
