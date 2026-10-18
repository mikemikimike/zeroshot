'use strict';

// Every published audit comment starts with this marker. Only bot-authored comments count, so
// reporters cannot forge or reset the round history by pasting a marker.
const MARKER_PATTERN =
  /^<!-- zeroshot-issue-audit round=(\d{1,2}) verdict=(ready|needs-information) -->/;

function marker(round, verdict) {
  return `<!-- zeroshot-issue-audit round=${round} verdict=${verdict} -->`;
}

function isBotUser(user) {
  if (!user || typeof user.login !== 'string') return true;
  return user.type === 'Bot' || user.login.endsWith('[bot]');
}

function parseMarker(comment) {
  if (!comment || !isBotUser(comment.user) || typeof comment.body !== 'string') return null;
  const match = MARKER_PATTERN.exec(comment.body);
  if (!match) return null;
  return { round: Number(match[1]), verdict: match[2], commentId: comment.id };
}

// Returns the most recent audit published on the issue, if any.
function latestAudit(comments) {
  let latest = null;
  for (const comment of comments) {
    const parsed = parseMarker(comment);
    if (parsed) latest = parsed;
  }
  return latest;
}

function isAuditComment(comment) {
  return parseMarker(comment) !== null;
}

module.exports = { isAuditComment, isBotUser, latestAudit, marker, parseMarker };
