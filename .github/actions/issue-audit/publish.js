'use strict';

const path = require('node:path');

const { createClient } = require('./lib/github');
const { labelChanges, renderComment } = require('./lib/render');
const { loadConfig, main, readJson, setOutputs, workdir } = require('./lib/runtime');
const { hasLabel } = require('./lib/trigger');
const { assertNoSecrets, validateVerdict } = require('./lib/verdict');

main(async () => {
  const config = loadConfig();
  const token = process.env.ISSUE_AUDIT_GITHUB_TOKEN;
  const dir = workdir();
  const context = readJson(path.join(dir, 'context.json'));
  // Re-validate: this step trusts nothing the agent step wrote.
  const verdict = validateVerdict(readJson(path.join(dir, 'verdict.json')), config);
  assertNoSecrets(JSON.stringify(verdict), [token]);
  if (!Number.isInteger(context.issueNumber) || !Number.isInteger(context.round)) {
    throw new Error('audit context is malformed');
  }
  if (context.round < 1 || context.round > config.maxRounds) {
    throw new Error('audit round is out of range');
  }

  const client = createClient({
    token,
    apiUrl: process.env.GITHUB_API_URL,
    repository: process.env.GITHUB_REPOSITORY,
  });
  const issue = await client.getIssue(context.issueNumber);
  if (issue.state !== 'open' || issue.pull_request) {
    console.log('The issue closed during the audit; nothing was published.');
    setOutputs({ verdict: '' });
    return;
  }

  const body = renderComment(verdict, { round: context.round, config });
  await client.createComment(context.issueNumber, body);
  const changes = labelChanges(verdict, config);
  for (const name of changes.remove) {
    if (hasLabel(issue, name)) await client.removeLabel(context.issueNumber, name);
  }
  await client.addLabels(context.issueNumber, changes.add);
  console.log(`Published round ${context.round}: ${verdict.verdict}`);
  setOutputs({ verdict: verdict.verdict });
});
