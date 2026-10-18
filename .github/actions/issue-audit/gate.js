'use strict';

const { COMMENT_MARKER, hasPermission, readConfig } = require('./config');
const { client, readEvent, repoFromEnv, writeOutputs } = require('./github');

const skip = (reason) => ({ run: false, reason });

function isAuditComment(comment) {
  return comment?.user?.type === 'Bot' && String(comment.body || '').includes(COMMENT_MARKER);
}

function countRounds(comments) {
  return comments.filter(isAuditComment).length;
}

function labelNames(issue) {
  return (issue.labels || []).map((item) => (typeof item === 'string' ? item : item.name));
}

// Decides from the event and looked-up permissions alone; it performs no I/O.
function decide({ eventName, event, config, roles, priorRounds }) {
  const issue = event.issue;
  if (!issue) return skip('event has no issue');
  if (issue.pull_request) return skip('pull request');
  if (issue.state !== 'open') return skip('issue is not open');
  if (event.sender?.type === 'Bot') return skip('event sent by a bot');

  const labels = labelNames(issue);
  const useTrusted = config.trigger !== 'label';
  const useLabel = config.trigger !== 'trusted';
  const authorTrusted = useTrusted && hasPermission(roles.author, config.minPermission);
  const accepted = useLabel && labels.includes(config.auditLabel);
  const round = priorRounds + 1;

  if (eventName === 'issues' && ['opened', 'reopened'].includes(event.action)) {
    if (authorTrusted) return { run: true, reason: 'trusted author', round };
    return skip('author is not trusted; waiting for a maintainer');
  }

  if (eventName === 'issues' && event.action === 'labeled') {
    if (useLabel && event.label?.name === config.auditLabel) {
      return { run: true, reason: 'maintainer applied the audit label', round };
    }
    return skip('label is not the audit label');
  }

  const fromAuthor = event.sender?.login === issue.user?.login;
  const isComment = eventName === 'issue_comment' && event.action === 'created';
  if (isComment) {
    const body = String(event.comment?.body || '').trim();
    if (body.split(/\s/, 1)[0] === config.command) {
      if (hasPermission(roles.sender, config.minPermission)) {
        return { run: true, reason: 'maintainer command', round };
      }
      return skip('command sender lacks permission');
    }
  }

  const isFollowUp = isComment || (eventName === 'issues' && event.action === 'edited');
  if (!isFollowUp) return skip(`unsupported event ${eventName}.${event.action}`);
  if (!fromAuthor) return skip('follow-up is not from the issue author');
  if (!labels.includes(config.needsInfoLabel)) return skip('issue is not awaiting answers');
  if (!authorTrusted && !accepted) return skip('issue is neither trusted nor accepted');
  if (priorRounds >= config.maxRounds) return skip('automatic round limit reached');
  return { run: true, reason: 'author answered questions', round };
}

async function roleOf(github, { owner, repo }, login) {
  if (!login) return 'none';
  const path = `/repos/${owner}/${repo}/collaborators/${encodeURIComponent(login)}/permission`;
  const result = await github.request('GET', path);
  if (result.status === 404) return 'none';
  if (result.status >= 300) throw new Error(`permission lookup failed with HTTP ${result.status}`);
  return result.data.role_name || result.data.permission || 'none';
}

async function main(env = process.env) {
  const config = readConfig(env);
  const event = readEvent(env);
  const repository = repoFromEnv(env);
  const github = client({ token: env.GITHUB_TOKEN, apiUrl: env.GITHUB_API_URL });
  const issue = event.issue || {};
  const needsSender = env.GITHUB_EVENT_NAME === 'issue_comment';
  const roles = {
    author:
      config.trigger === 'label' ? 'none' : await roleOf(github, repository, issue.user?.login),
    sender: needsSender ? await roleOf(github, repository, event.sender?.login) : 'none',
  };
  const comments = issue.number
    ? await github.paginate(
        `/repos/${repository.owner}/${repository.repo}/issues/${issue.number}/comments`
      )
    : [];
  const decision = decide({
    eventName: env.GITHUB_EVENT_NAME,
    event,
    config,
    roles,
    priorRounds: countRounds(comments),
  });
  process.stdout.write(`issue-audit: ${decision.run ? 'run' : 'skip'} (${decision.reason})\n`);
  writeOutputs({ run: decision.run, round: decision.round || 0 }, env);
}

module.exports = { countRounds, decide, isAuditComment, labelNames };

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`issue-audit gate failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
