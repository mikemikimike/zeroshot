'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { describe, it } = require('node:test');

const actionDir = path.resolve(__dirname, '..', '..', '.github', 'actions', 'issue-audit');
const { readConfig } = require(path.join(actionDir, 'config'));
const { buildPrompt } = require(path.join(actionDir, 'context'));
const { countRounds, decide } = require(path.join(actionDir, 'gate'));
const { clean, harnessResult, planLabels, renderComment, validateVerdict } = require(
  path.join(actionDir, 'publish')
);

const config = readConfig({});
const marker = '<!-- zeroshot-issue-audit:v1 round=1 verdict=needs_info -->';

function issue(overrides = {}) {
  return {
    number: 7,
    title: 'Add a flag',
    body: 'Please add a flag.',
    state: 'open',
    user: { login: 'reporter', type: 'User' },
    labels: [],
    ...overrides,
  };
}

function gate({ eventName = 'issues', action = 'opened', roles = {}, priorRounds = 0, ...rest }) {
  const event = {
    action,
    issue: issue(rest.issue),
    sender: rest.sender || { login: 'reporter', type: 'User' },
    label: rest.label,
    comment: rest.comment,
  };
  return decide({
    eventName,
    event,
    config: rest.config || config,
    roles: { author: 'none', sender: 'none', ...roles },
    priorRounds,
  });
}

describe('issue audit gate', () => {
  it('audits issues opened by trusted authors and waits on everyone else', () => {
    assert.deepEqual(gate({ roles: { author: 'write' } }), {
      run: true,
      reason: 'trusted author',
      round: 1,
    });
    assert.equal(gate({ roles: { author: 'triage' } }).run, false);
    assert.equal(gate({ roles: { author: 'none' } }).run, false);
  });

  it('audits when a maintainer applies the audit label, unless the trigger is trusted-only', () => {
    const labeled = {
      action: 'labeled',
      label: { name: 'audit' },
      sender: { login: 'maintainer' },
    };
    assert.equal(gate(labeled).run, true);
    assert.equal(gate({ ...labeled, label: { name: 'bug' } }).run, false);
    assert.equal(gate({ ...labeled, config: { ...config, trigger: 'trusted' } }).run, false);
  });

  it('runs follow-up rounds only for the author of an awaiting, eligible issue', () => {
    const awaiting = { labels: [{ name: 'needs-info' }] };
    const reply = {
      eventName: 'issue_comment',
      action: 'created',
      comment: { body: 'Use the default.' },
      roles: { author: 'write' },
      priorRounds: 1,
      issue: awaiting,
    };
    assert.deepEqual(gate(reply), { run: true, reason: 'author answered questions', round: 2 });
    assert.equal(gate({ ...reply, sender: { login: 'someone-else' } }).run, false);
    assert.equal(gate({ ...reply, issue: {} }).run, false);
    assert.equal(gate({ ...reply, priorRounds: 3 }).run, false);
    assert.equal(gate({ ...reply, roles: { author: 'none' } }).run, false);
    const accepted = { labels: [{ name: 'needs-info' }, { name: 'audit' }] };
    assert.equal(gate({ ...reply, roles: { author: 'none' }, issue: accepted }).run, true);
    assert.equal(gate({ ...reply, eventName: 'issues', action: 'edited' }).run, true);
  });

  it('lets maintainers restart with the command past the round limit', () => {
    const command = {
      eventName: 'issue_comment',
      action: 'created',
      comment: { body: '/audit please' },
      sender: { login: 'maintainer', type: 'User' },
      priorRounds: 9,
    };
    assert.equal(gate({ ...command, roles: { sender: 'maintain' } }).run, true);
    assert.equal(gate({ ...command, roles: { sender: 'read' } }).run, false);
    assert.equal(gate({ ...command, comment: { body: 'see /audit' } }).run, false);
  });

  it('ignores pull requests, closed issues and bot events', () => {
    const trusted = { roles: { author: 'admin' } };
    assert.equal(gate({ ...trusted, issue: { pull_request: {} } }).run, false);
    assert.equal(gate({ ...trusted, issue: { state: 'closed' } }).run, false);
    assert.equal(gate({ ...trusted, sender: { login: 'app', type: 'Bot' } }).run, false);
  });

  it('counts only marked comments written by bots as rounds', () => {
    const comments = [
      { user: { type: 'Bot' }, body: marker },
      { user: { type: 'User' }, body: marker },
      { user: { type: 'Bot' }, body: 'unrelated' },
    ];
    assert.equal(countRounds(comments), 1);
  });

  it('rejects invalid configuration', () => {
    assert.throws(() => readConfig({ AUDIT_TRIGGER: 'everyone' }), /trigger/);
    assert.throws(() => readConfig({ AUDIT_MIN_PERMISSION: 'none' }), /min-permission/);
    assert.throws(() => readConfig({ AUDIT_MAX_ROUNDS: '0' }), /max-rounds/);
    assert.throws(() => readConfig({ AUDIT_COMMAND: 'audit' }), /command/);
  });
});

describe('issue audit verdicts', () => {
  it('validates, bounds and sanitizes agent output', () => {
    const verdict = validateVerdict(
      {
        verdict: 'needs_info',
        summary: 'Clear goal. <!-- zeroshot-issue-audit:v1 --> cc @octocat ![x](https://e.x/p.png)',
        questions: Array.from({ length: 5 }, (_, index) => ({
          question: `Question ${index}?`,
          recommended: 'Default',
        })),
      },
      config
    );
    assert.equal(verdict.questions.length, 3);
    assert.doesNotMatch(verdict.summary, /zeroshot-issue-audit|!\[|@octocat/);
    assert.match(verdict.summary, /@​octocat/);
    assert.deepEqual(
      validateVerdict({ verdict: 'ready', summary: 'ok', questions: [{ question: 'x' }] }, config)
        .questions,
      []
    );
    assert.throws(() => validateVerdict({ verdict: 'needs_info', summary: 'x' }, config));
    assert.throws(() => validateVerdict({ verdict: 'approve', summary: 'x' }, config));
    assert.throws(() => validateVerdict({ verdict: 'ready', summary: ' ' }, config));
    assert.equal(clean('a'.repeat(2000)).length, 1201);
  });

  it('reads structured output from each harness and fails closed', () => {
    const value = { verdict: 'ready', summary: 'ok', questions: [] };
    const claude = {
      type: 'result',
      subtype: 'success',
      is_error: false,
      structured_output: value,
    };
    assert.deepEqual(harnessResult(JSON.stringify(claude), 'claude'), value);
    assert.deepEqual(harnessResult(JSON.stringify(value), 'codex'), value);
    assert.throws(() =>
      harnessResult(JSON.stringify({ ...claude, subtype: 'error_max_budget' }), 'claude')
    );
    assert.throws(() =>
      harnessResult(JSON.stringify({ ...claude, structured_output: null }), 'claude')
    );
    assert.throws(() => harnessResult('not json', 'codex'));
  });

  it('renders a marked comment and swaps labels', () => {
    const verdict = {
      verdict: 'needs_info',
      summary: 'Goal is clear.',
      questions: [{ question: 'Which flag name?', recommended: '--clarify' }],
    };
    const first = renderComment(verdict, { round: 1, config });
    assert.match(first, /^<!-- zeroshot-issue-audit:v1 round=1 verdict=needs_info -->/);
    assert.match(first, /1\. Which flag name\?\n {3}\*Suggested default:\* --clarify/);
    assert.match(renderComment(verdict, { round: 3, config }), /comment `\/audit`/);
    assert.match(
      renderComment({ verdict: 'ready', summary: 'Done.', questions: [] }, { round: 2, config }),
      /\*\*Ready for implementation\.\*\*/
    );
    assert.deepEqual(planLabels({ verdict: 'ready' }, ['needs-info', 'bug'], config), {
      add: ['ready'],
      remove: ['needs-info'],
    });
    assert.deepEqual(planLabels({ verdict: 'needs_info' }, ['needs-info'], config), {
      add: [],
      remove: [],
    });
  });
});

describe('issue audit prompt and packaging', () => {
  it('fences the untrusted thread and bounds long entries', () => {
    const boundary = '<<<ISSUE-test>>>';
    const prompt = buildPrompt({
      rubric: '# Rubric',
      issue: issue({ body: 'x'.repeat(25000) }),
      comments: [
        { user: { login: 'zeroshot-bot', type: 'Bot' }, body: marker, created_at: 't1' },
        { user: { login: 'reporter', type: 'User' }, body: 'Answer', created_at: 't2' },
      ],
      config,
      round: 2,
      boundary,
    });
    assert.equal(prompt.split(boundary).length, 4);
    assert.match(prompt, /\[truncated 5000 characters\]/);
    assert.match(prompt, /@zeroshot-bot \(previous audit\)/);
    assert.match(prompt, /@reporter \(issue author\) commented/);
    assert.match(prompt, /round 2 of at most 3/);
  });

  it('keeps every schema property required, as Codex structured output demands', () => {
    const schema = JSON.parse(fs.readFileSync(path.join(actionDir, 'verdict.schema.json'), 'utf8'));
    const objects = [schema, schema.properties.questions.items];
    for (const object of objects) {
      assert.equal(object.additionalProperties, false);
      assert.deepEqual([...object.required].sort(), Object.keys(object.properties).sort());
    }
  });

  it('defaults to the harness versions pinned in the target image', () => {
    const action = fs.readFileSync(path.join(actionDir, 'action.yml'), 'utf8');
    const dockerfile = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'docker', 'zeroshot-target', 'Dockerfile'),
      'utf8'
    );
    for (const [input, arg] of [
      ['claude-code-version', 'CLAUDE_CODE_VERSION'],
      ['codex-version', 'CODEX_VERSION'],
    ]) {
      const pinned = dockerfile.match(new RegExp(`ARG ${arg}=(\\S+)`))[1];
      const block = action.slice(action.indexOf(`  ${input}:`));
      assert.equal(block.match(/default: (\S+)/)[1], pinned, input);
    }
  });
});
