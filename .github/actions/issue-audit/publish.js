'use strict';

const fs = require('node:fs');

const { COMMENT_MARKER, readConfig } = require('./config');
const { labelNames } = require('./gate');
const { client, readEvent, repoFromEnv } = require('./github');

const MAX_FIELD_CHARS = 1200;
const LABEL_COLORS = { ready: '0e8a16', needsInfo: 'd876e3' };

// Agent output is untrusted: drop hidden comments and images, and keep mentions from pinging.
function clean(text) {
  const value = String(text ?? '')
    .replace(/<!--[\s\S]*?(-->|$)/g, '')
    .replace(/!\[/g, '[')
    .replace(/@(?=[A-Za-z0-9-])/g, '@​')
    .replace(/\r/g, '')
    .trim();
  return value.length > MAX_FIELD_CHARS ? `${value.slice(0, MAX_FIELD_CHARS)}…` : value;
}

function harnessResult(raw, harness) {
  const parsed = JSON.parse(raw);
  if (harness !== 'claude') return parsed;
  if (parsed.is_error || parsed.subtype !== 'success') {
    throw new Error(`Claude Code did not finish successfully (${parsed.subtype || 'error'})`);
  }
  if (!parsed.structured_output) throw new Error('Claude Code returned no structured output');
  return parsed.structured_output;
}

function validateVerdict(value, config) {
  if (!value || typeof value !== 'object') throw new Error('verdict must be an object');
  if (!['ready', 'needs_info'].includes(value.verdict)) throw new Error('invalid verdict');
  const summary = clean(value.summary);
  if (!summary) throw new Error('summary must not be empty');
  const questions = (Array.isArray(value.questions) ? value.questions : [])
    .map((item) => ({ question: clean(item?.question), recommended: clean(item?.recommended) }))
    .filter((item) => item.question);
  if (value.verdict === 'needs_info' && questions.length === 0) {
    throw new Error('needs_info requires at least one question');
  }
  return {
    verdict: value.verdict,
    summary,
    questions: value.verdict === 'ready' ? [] : questions.slice(0, config.maxQuestions),
  };
}

function renderComment(verdict, { round, config }) {
  const marker = `${COMMENT_MARKER} round=${round} verdict=${verdict.verdict} -->`;
  if (verdict.verdict === 'ready') {
    return `${marker}\n**Ready for implementation.**\n\n${verdict.summary}\n`;
  }
  const questions = verdict.questions
    .map(
      (item, index) =>
        `${index + 1}. ${item.question}` +
        (item.recommended ? `\n   *Suggested default:* ${item.recommended}` : '')
    )
    .join('\n');
  const next =
    round >= config.maxRounds
      ? `This was the last automatic round. A maintainer can comment \`${config.command}\` to audit again.`
      : 'Reply in this thread, or agree with the suggested defaults, and the issue is audited again.';
  return [
    marker,
    'Thanks for the issue. A few questions before it is ready for implementation:',
    '',
    verdict.summary,
    '',
    questions,
    '',
    `<sub>${next}</sub>`,
    '',
  ].join('\n');
}

function planLabels(verdict, currentLabels, config) {
  const want = verdict.verdict === 'ready' ? config.readyLabel : config.needsInfoLabel;
  const drop = verdict.verdict === 'ready' ? config.needsInfoLabel : config.readyLabel;
  return {
    add: currentLabels.includes(want) ? [] : [want],
    remove: currentLabels.includes(drop) ? [drop] : [],
  };
}

async function ensureLabel(github, base, name, color) {
  const result = await github.request('POST', `${base}/labels`, { name, color });
  if (result.status !== 201 && result.status !== 422) {
    throw new Error(`creating label ${name} failed with HTTP ${result.status}`);
  }
}

async function main(env = process.env) {
  const config = readConfig(env);
  const event = readEvent(env);
  const { owner, repo } = repoFromEnv(env);
  const raw = fs.readFileSync(env.AUDIT_RESULT_FILE, 'utf8');
  const verdict = validateVerdict(harnessResult(raw, env.AUDIT_HARNESS), config);
  const github = client({ token: env.GITHUB_TOKEN, apiUrl: env.GITHUB_API_URL });
  const base = `/repos/${owner}/${repo}`;
  const number = event.issue.number;
  const issue = await github.ok('GET', `${base}/issues/${number}`);
  const labels = planLabels(verdict, labelNames(issue), config);

  await ensureLabel(github, base, config.readyLabel, LABEL_COLORS.ready);
  await ensureLabel(github, base, config.needsInfoLabel, LABEL_COLORS.needsInfo);
  for (const name of labels.remove) {
    const result = await github.request(
      'DELETE',
      `${base}/issues/${number}/labels/${encodeURIComponent(name)}`
    );
    if (result.status >= 300 && result.status !== 404) {
      throw new Error(`removing label ${name} failed with HTTP ${result.status}`);
    }
  }
  if (labels.add.length > 0) {
    await github.ok('POST', `${base}/issues/${number}/labels`, { labels: labels.add });
  }
  const round = Number(env.AUDIT_ROUND) || 1;
  await github.ok('POST', `${base}/issues/${number}/comments`, {
    body: renderComment(verdict, { round, config }),
  });
  process.stdout.write(`issue-audit: ${verdict.verdict} on #${number} (round ${round})\n`);
}

module.exports = { clean, harnessResult, planLabels, renderComment, validateVerdict };

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`issue-audit publish failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
