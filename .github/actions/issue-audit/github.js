'use strict';

const fs = require('node:fs');

const API_VERSION = '2022-11-28';
const MAX_PAGES = 10;

function client({ token, apiUrl = 'https://api.github.com', fetchImpl = fetch }) {
  if (!token) throw new Error('github-token is required');

  async function request(method, path, body) {
    const response = await fetchImpl(`${apiUrl}${path}`, {
      method,
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${token}`,
        'x-github-api-version': API_VERSION,
        ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    return { status: response.status, data };
  }

  async function ok(method, path, body) {
    const result = await request(method, path, body);
    if (result.status >= 300) {
      throw new Error(`GitHub ${method} ${path} failed with HTTP ${result.status}`);
    }
    return result.data;
  }

  async function paginate(path) {
    const items = [];
    for (let page = 1; page <= MAX_PAGES; page += 1) {
      const separator = path.includes('?') ? '&' : '?';
      const batch = await ok('GET', `${path}${separator}per_page=100&page=${page}`);
      items.push(...batch);
      if (batch.length < 100) break;
    }
    return items;
  }

  return { request, ok, paginate };
}

function repoFromEnv(env = process.env) {
  const [owner, repo] = String(env.GITHUB_REPOSITORY || '').split('/');
  if (!owner || !repo) throw new Error('GITHUB_REPOSITORY must be owner/name');
  return { owner, repo };
}

function readEvent(env = process.env) {
  if (!env.GITHUB_EVENT_PATH) throw new Error('GITHUB_EVENT_PATH is not set');
  return JSON.parse(fs.readFileSync(env.GITHUB_EVENT_PATH, 'utf8'));
}

function writeOutputs(outputs, env = process.env) {
  const lines = Object.entries(outputs).map(([key, value]) => `${key}=${value}`);
  if (env.GITHUB_OUTPUT) fs.appendFileSync(env.GITHUB_OUTPUT, `${lines.join('\n')}\n`);
  else process.stdout.write(`${lines.join('\n')}\n`);
}

module.exports = { client, readEvent, repoFromEnv, writeOutputs };
