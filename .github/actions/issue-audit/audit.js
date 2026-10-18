'use strict';

const fs = require('node:fs');
const path = require('node:path');

const { parseCredentials } = require('./lib/config');
const {
  agentEnv,
  binaryName,
  claudeInvocation,
  codexInvocation,
  codexLoginInvocation,
  parseClaudeOutput,
  parseCodexOutput,
  runProcess,
} = require('./lib/harness');
const { ensureDir, loadConfig, main, readJson, workdir, writePrivate } = require('./lib/runtime');
const { assertNoSecrets, validateVerdict } = require('./lib/verdict');

function failure(label, result) {
  if (result.timedOut) return new Error(`${label} exceeded its time limit`);
  if (result.overflowed) return new Error(`${label} exceeded its output limit`);
  return new Error(`${label} exited with ${result.signal ?? result.code}`);
}

main(async () => {
  const config = loadConfig();
  const credential = parseCredentials(config.harness, process.env);
  const dir = workdir();
  const checkout = process.env.ISSUE_AUDIT_CHECKOUT;
  if (!checkout || !path.isAbsolute(checkout)) throw new Error('checkout path must be absolute');

  const prompt = fs.readFileSync(path.join(dir, 'prompt.md'), 'utf8');
  const schema = readJson(path.join(dir, 'schema.json'));
  const binary = path.join(dir, 'harness', 'bin', binaryName(config));
  const home = ensureDir(path.join(dir, 'home'));
  const tmp = ensureDir(path.join(dir, 'tmp'));
  const deadline = Date.now() + config.timeoutMinutes * 60 * 1000;
  const remaining = () => Math.max(1000, deadline - Date.now());
  const base = { path: process.env.PATH, home, tmp };

  let parsed;
  if (config.harness === 'claude-code') {
    const invocation = claudeInvocation({ config, schema });
    const result = await runProcess(binary, invocation.args, {
      cwd: checkout,
      env: agentEnv({ ...base, extra: { ...invocation.env, [credential.name]: credential.value } }),
      input: prompt,
      timeoutMs: remaining(),
    });
    if (result.timedOut || result.overflowed) throw failure('Claude Code', result);
    parsed = parseClaudeOutput(result.stdout);
  } else {
    const codexHome = ensureDir(path.join(dir, 'codex-home'));
    const env = agentEnv({ ...base, extra: { CODEX_HOME: codexHome } });
    const login = await runProcess(binary, codexLoginInvocation().args, {
      cwd: checkout,
      env,
      input: credential.value,
      timeoutMs: Math.min(remaining(), 60000),
    });
    if (login.code !== 0) throw failure('codex login', login);
    const schemaPath = path.join(dir, 'codex-schema.json');
    const outputPath = path.join(dir, 'codex-last-message.txt');
    writePrivate(schemaPath, JSON.stringify(schema));
    const invocation = codexInvocation({ config, schemaPath, outputPath });
    const result = await runProcess(binary, invocation.args, {
      cwd: checkout,
      env,
      input: prompt,
      timeoutMs: remaining(),
    });
    if (result.code !== 0) throw failure('Codex', result);
    parsed = parseCodexOutput(fs.existsSync(outputPath) ? fs.readFileSync(outputPath, 'utf8') : '');
  }

  assertNoSecrets(parsed.text, [credential.value]);
  const verdict = validateVerdict(parsed.value, config);
  assertNoSecrets(JSON.stringify(verdict), [credential.value]);
  writePrivate(path.join(dir, 'verdict.json'), JSON.stringify(verdict));
  const cost = parsed.costUsd === null ? '' : ` (reported cost $${parsed.costUsd.toFixed(4)})`;
  console.log(`Verdict: ${verdict.verdict} with ${verdict.questions.length} questions${cost}`);
});
