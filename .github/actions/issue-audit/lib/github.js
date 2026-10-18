'use strict';

const { ROLES } = require('./config');

const MAX_COMMENT_PAGES = 30;
const REQUEST_TIMEOUT_MS = 30000;

class GitHubError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/** Minimal REST client used only by the deterministic decide and publish steps. */
function createClient({ token, apiUrl, repository, fetchImpl = fetch }) {
  if (!token) throw new Error('github-token is required');
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository ?? '')) {
    throw new Error('GITHUB_REPOSITORY must be owner/name');
  }
  const base = `${(apiUrl || 'https://api.github.com').replace(/\/+$/, '')}/repos/${repository}`;

  async function request(method, path, body) {
    const response = await fetchImpl(`${base}${path}`, {
      method,
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${token}`,
        'x-github-api-version': '2022-11-28',
        ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) {
      throw new GitHubError(
        `GitHub ${method} ${path} failed with ${response.status}`,
        response.status
      );
    }
    return response.status === 204 ? null : response.json();
  }

  return {
    getIssue: (number) => request('GET', `/issues/${number}`),

    async listComments(number) {
      const comments = [];
      for (let page = 1; page <= MAX_COMMENT_PAGES; page += 1) {
        const batch = await request('GET', `/issues/${number}/comments?per_page=100&page=${page}`);
        comments.push(...batch);
        if (batch.length < 100) return comments;
      }
      throw new GitHubError(`issue has more than ${MAX_COMMENT_PAGES * 100} comments`);
    },

    async getRole(login) {
      try {
        const result = await request(
          'GET',
          `/collaborators/${encodeURIComponent(login)}/permission`
        );
        if (ROLES.includes(result.role_name)) return result.role_name;
        return ROLES.includes(result.permission) ? result.permission : 'none';
      } catch (error) {
        if (error.status === 404) return 'none';
        throw error;
      }
    },

    createComment: (number, body) => request('POST', `/issues/${number}/comments`, { body }),

    addLabels: (number, labels) => request('POST', `/issues/${number}/labels`, { labels }),

    async removeLabel(number, name) {
      try {
        await request('DELETE', `/issues/${number}/labels/${encodeURIComponent(name)}`);
      } catch (error) {
        if (error.status !== 404) throw error;
      }
    },
  };
}

module.exports = { GitHubError, createClient };
