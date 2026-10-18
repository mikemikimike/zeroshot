'use strict';

const { roleAtLeast } = require('./config');
const { isBotUser, latestAudit } = require('./state');

function skip(reason) {
  return { audit: false, reason };
}

function run(kind, round, reason) {
  return { audit: true, kind, round, reason };
}

function hasLabel(issue, name) {
  const wanted = name.toLowerCase();
  return (issue.labels ?? []).some(
    (label) => (typeof label === 'string' ? label : label?.name)?.toLowerCase() === wanted
  );
}

function sameUser(left, right) {
  return Boolean(left && right && left.id === right.id && left.login === right.login);
}

function firstLine(body) {
  const line = String(body ?? '')
    .split(/\r?\n/)
    .find((candidate) => candidate.trim() !== '');
  return (line ?? '').trim();
}

function isCommand(body, command) {
  const line = firstLine(body);
  return line === command || line.startsWith(`${command} `);
}

function labelModeEnabled(config) {
  return config.triggerMode === 'label' || config.triggerMode === 'both';
}

function authorModeEnabled(config) {
  return config.triggerMode === 'trusted-authors' || config.triggerMode === 'both';
}

// A follow-up round needs a previous needs-information audit, the awaiting label and a round left.
function followUp(issue, comments, config, source) {
  const latest = latestAudit(comments);
  if (!latest || latest.verdict !== 'needs-information') {
    return skip(`${source} by the author, but the issue is not awaiting answers`);
  }
  if (!hasLabel(issue, config.needsInformationLabel)) {
    return skip(`${source} by the author, but the ${config.needsInformationLabel} label is absent`);
  }
  if (latest.round >= config.maxRounds) {
    return skip(
      `${source} by the author, but all ${config.maxRounds} automatic rounds are used; ` +
        `a maintainer can comment ${config.command}`
    );
  }
  return run('follow-up', latest.round + 1, `${source} by the author while awaiting answers`);
}

/**
 * Decides whether an event starts an audit. Runs before any model credential is touched.
 *
 * @param {object} input
 * @param {string} input.eventName GitHub event name.
 * @param {object} input.payload Webhook payload.
 * @param {object} input.issue Fresh issue state from the API.
 * @param {object[]} input.comments Issue comments in creation order.
 * @param {object} input.config Parsed action configuration.
 * @param {(login: string) => Promise<string>} input.getRole Actual repository role lookup.
 */
async function decideTrigger({ eventName, payload, issue, comments, config, getRole }) {
  const sender = payload.sender;
  if (isBotUser(sender)) return skip('events sent by bots never trigger an audit');
  if (!issue || issue.pull_request || payload.issue?.pull_request) {
    return skip('pull requests are not audited');
  }
  if (issue.state !== 'open') return skip('closed issues are not audited');

  const action = payload.action;
  if (eventName === 'issues' && action === 'opened') {
    if (!authorModeEnabled(config)) return skip('trusted-author audits are disabled');
    if (isBotUser(issue.user)) return skip('issues opened by bots are not audited');
    const role = await getRole(issue.user.login);
    if (!roleAtLeast(role, config.minimumAuthorRole)) {
      return skip(`author role ${role} is below ${config.minimumAuthorRole}`);
    }
    if (
      labelModeEnabled(config) &&
      hasLabel(issue, config.auditLabel) &&
      roleAtLeast(role, config.maintainerRole)
    ) {
      return skip(`the ${config.auditLabel} label event audits this issue`);
    }
    return run('opened', 1, `opened by an author with role ${role}`);
  }

  if (eventName === 'issues' && action === 'labeled') {
    if (payload.label?.name?.toLowerCase() !== config.auditLabel.toLowerCase()) {
      return skip('the applied label is not the audit label');
    }
    if (!labelModeEnabled(config)) return skip('label audits are disabled');
    const role = await getRole(sender.login);
    if (!roleAtLeast(role, config.maintainerRole)) {
      return skip(`label applied by role ${role}, below ${config.maintainerRole}`);
    }
    return run('label', 1, `${config.auditLabel} label applied by a maintainer`);
  }

  if (eventName === 'issues' && action === 'edited') {
    if (!sameUser(sender, issue.user)) return skip('only author edits start follow-up rounds');
    if (!payload.changes?.body && !payload.changes?.title) {
      return skip('the edit changed neither title nor body');
    }
    return followUp(issue, comments, config, 'issue edited');
  }

  if (eventName === 'issue_comment' && action === 'created') {
    const comment = payload.comment;
    if (!comment || isBotUser(comment.user)) return skip('bot comments never trigger an audit');
    if (isCommand(comment.body, config.command)) {
      const role = await getRole(comment.user.login);
      if (roleAtLeast(role, config.maintainerRole)) {
        return run('command', 1, `${config.command} requested by a maintainer`);
      }
      if (!sameUser(comment.user, issue.user)) {
        return skip(`${config.command} requested by role ${role}, below ${config.maintainerRole}`);
      }
    }
    if (!sameUser(comment.user, issue.user)) {
      return skip('only author comments start follow-up rounds');
    }
    return followUp(issue, comments, config, 'commented');
  }

  return skip(`event ${eventName}.${action ?? 'unknown'} does not trigger an audit`);
}

module.exports = { decideTrigger, hasLabel, isCommand };
