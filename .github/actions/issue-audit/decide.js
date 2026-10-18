'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { createClient } = require('./lib/github');
const { buildPrompt } = require('./lib/prompt');
const { main, recordConfig, setOutputs, workdir, writePrivate } = require('./lib/runtime');
const { isBotUser } = require('./lib/state');
const { decideTrigger } = require('./lib/trigger');
const { verdictSchema } = require('./lib/verdict');

function skip(reason) {
  console.log(`No audit: ${reason}`);
  setOutputs({ audit: 'false', reason, round: '0', kind: '' });
}

main(async () => {
  const config = recordConfig(process.env);
  const eventName = process.env.GITHUB_EVENT_NAME;
  const payload = JSON.parse(fs.readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
  const number = payload.issue?.number;

  // Cheap rejections first, so ignored events make no API calls.
  if (isBotUser(payload.sender)) return skip('events sent by bots never trigger an audit');
  if (!Number.isInteger(number)) return skip(`event ${eventName} has no issue`);
  if (payload.issue.pull_request) return skip('pull requests are not audited');

  const client = createClient({
    token: process.env.ISSUE_AUDIT_GITHUB_TOKEN,
    apiUrl: process.env.GITHUB_API_URL,
    repository: process.env.GITHUB_REPOSITORY,
  });
  const issue = await client.getIssue(number);
  const comments = await client.listComments(number);
  const decision = await decideTrigger({
    eventName,
    payload,
    issue,
    comments,
    config,
    getRole: client.getRole,
  });
  if (!decision.audit) return skip(decision.reason);

  const dir = workdir();
  const boundary = crypto.randomBytes(16).toString('hex');
  writePrivate(
    path.join(dir, 'prompt.md'),
    buildPrompt({ issue, comments, config, round: decision.round, boundary })
  );
  writePrivate(path.join(dir, 'schema.json'), JSON.stringify(verdictSchema()));
  writePrivate(
    path.join(dir, 'context.json'),
    JSON.stringify({ issueNumber: number, round: decision.round, kind: decision.kind })
  );
  console.log(`Audit round ${decision.round}: ${decision.reason}`);
  setOutputs({
    audit: 'true',
    reason: decision.reason,
    round: String(decision.round),
    kind: decision.kind,
  });
});
