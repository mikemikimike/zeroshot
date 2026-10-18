'use strict';

const fs = require('node:fs');
const path = require('node:path');

const { DEFAULTS, envName, parseConfig } = require('./config');

function workdir(env = process.env) {
  const dir = env.ISSUE_AUDIT_WORKDIR;
  if (!dir || !path.isAbsolute(dir)) throw new Error('ISSUE_AUDIT_WORKDIR must be absolute');
  return dir;
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

function writePrivate(file, contents) {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, contents, { mode: 0o600 });
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

// The decide step validates the inputs once and records them; later steps reuse that record so
// every step sees the same configuration and only the agent step sees model credentials.
function recordConfig(env = process.env) {
  const inputs = {};
  for (const input of Object.keys(DEFAULTS)) {
    const name = envName(input);
    if (env[name] !== undefined) inputs[name] = env[name];
  }
  const config = parseConfig(inputs);
  writePrivate(path.join(workdir(env), 'inputs.json'), JSON.stringify(inputs));
  return config;
}

function loadConfig(env = process.env) {
  return parseConfig(readJson(path.join(workdir(env), 'inputs.json')));
}

function setOutputs(outputs, env = process.env) {
  const lines = Object.entries(outputs).map(([key, value]) => {
    const text = String(value).replace(/[\r\n]+/g, ' ');
    return `${key}=${text}`;
  });
  if (env.GITHUB_OUTPUT) fs.appendFileSync(env.GITHUB_OUTPUT, `${lines.join('\n')}\n`);
}

function main(fn) {
  fn().catch((error) => {
    console.error(`::error::${String(error.message).replace(/[\r\n]+/g, ' ')}`);
    process.exitCode = 1;
  });
}

module.exports = {
  ensureDir,
  loadConfig,
  main,
  readJson,
  recordConfig,
  setOutputs,
  workdir,
  writePrivate,
};
