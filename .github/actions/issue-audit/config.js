'use strict';

const TRIGGERS = new Set(['trusted', 'label', 'trusted-or-label']);
const PERMISSION_RANK = { none: 0, read: 1, triage: 2, write: 3, maintain: 4, admin: 5 };
const COMMENT_MARKER = '<!-- zeroshot-issue-audit:v1';

function positiveInteger(name, value, max) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1 || number > max) {
    throw new Error(`${name} must be an integer from 1 to ${max}, got ${JSON.stringify(value)}`);
  }
  return number;
}

function label(name, value) {
  const text = String(value || '').trim();
  if (!text || text.length > 50) throw new Error(`${name} must be 1-50 characters`);
  return text;
}

function readConfig(env = process.env) {
  const trigger = String(env.AUDIT_TRIGGER || 'trusted-or-label').trim();
  if (!TRIGGERS.has(trigger)) {
    throw new Error(`trigger must be one of ${[...TRIGGERS].join(', ')}, got ${trigger}`);
  }
  const minPermission = String(env.AUDIT_MIN_PERMISSION || 'write').trim();
  if (!(minPermission in PERMISSION_RANK) || minPermission === 'none') {
    throw new Error(`min-permission must be read, triage, write, maintain or admin`);
  }
  const command = String(env.AUDIT_COMMAND || '/audit').trim();
  if (!/^\/[a-z][a-z0-9-]{0,30}$/.test(command)) {
    throw new Error('command must look like /audit');
  }
  return {
    trigger,
    minPermission,
    command,
    auditLabel: label('audit-label', env.AUDIT_LABEL || 'audit'),
    readyLabel: label('ready-label', env.AUDIT_READY_LABEL || 'ready'),
    needsInfoLabel: label('needs-info-label', env.AUDIT_NEEDS_INFO_LABEL || 'needs-info'),
    maxRounds: positiveInteger('max-rounds', env.AUDIT_MAX_ROUNDS || 3, 10),
    maxQuestions: positiveInteger('max-questions', env.AUDIT_MAX_QUESTIONS || 3, 5),
  };
}

function hasPermission(role, minimum) {
  return (PERMISSION_RANK[role] ?? 0) >= PERMISSION_RANK[minimum];
}

module.exports = { COMMENT_MARKER, PERMISSION_RANK, hasPermission, readConfig };
