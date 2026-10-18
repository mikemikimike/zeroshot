'use strict';

const { marker } = require('./state');
const {
  MAX_DEFAULT_LENGTH,
  MAX_QUESTION_LENGTH,
  MAX_SUMMARY_LENGTH,
  sanitizeInline,
} = require('./verdict');

const MAX_COMMENT_LENGTH = 60000;

function restartHint(config) {
  const label =
    config.triggerMode === 'trusted-authors'
      ? ''
      : ` or removing and re-applying the \`${config.auditLabel}\` label`;
  return `A maintainer can request a new audit by commenting \`${config.command}\`${label}.`;
}

/** Renders the comment for a validated verdict. Agent text is sanitized here. */
function renderComment(verdict, { round, config }) {
  const lines = [marker(round, verdict.verdict)];
  const summary = sanitizeInline(verdict.summary, MAX_SUMMARY_LENGTH);
  if (verdict.verdict === 'ready') {
    lines.push(
      '### Issue audit: ready for implementation',
      '',
      summary,
      '',
      `Status: labeled \`${config.readyLabel}\`. No further information is needed.`
    );
  } else {
    const finalRound = round >= config.maxRounds;
    lines.push(
      `### Issue audit: needs information (round ${round} of ${config.maxRounds})`,
      '',
      summary,
      '',
      `Status: labeled \`${config.needsInformationLabel}\`. Each question has a suggested ` +
        'default; reply "accept defaults" to accept them all, or answer the questions you ' +
        'want to change.',
      ''
    );
    verdict.questions.forEach((question, index) => {
      lines.push(
        `${index + 1}. **Question:** ${sanitizeInline(question.question, MAX_QUESTION_LENGTH)}`,
        `   **Suggested default:** ${sanitizeInline(question.suggested_default, MAX_DEFAULT_LENGTH)}`
      );
    });
    lines.push('');
    if (finalRound) {
      lines.push(
        `This was the final automatic audit round. Replies are no longer audited automatically. ` +
          restartHint(config)
      );
    } else {
      lines.push(
        `When the issue author comments or edits the issue, audit round ${round + 1} of ` +
          `${config.maxRounds} runs automatically.`
      );
    }
  }
  const body = lines.join('\n');
  if (body.length > MAX_COMMENT_LENGTH) throw new Error('rendered comment exceeds its bound');
  return body;
}

/** Computes the label changes that put the issue in exactly one audit state. */
function labelChanges(verdict, config) {
  const ready = verdict.verdict === 'ready';
  return {
    add: [ready ? config.readyLabel : config.needsInformationLabel],
    remove: [ready ? config.needsInformationLabel : config.readyLabel, config.auditLabel],
  };
}

module.exports = { labelChanges, renderComment, restartHint };
