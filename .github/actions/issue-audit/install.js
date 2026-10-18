'use strict';

const { spawnSync } = require('node:child_process');
const path = require('node:path');

const { packageSpec } = require('./lib/harness');
const { ensureDir, loadConfig, main, workdir } = require('./lib/runtime');

main(async () => {
  const config = loadConfig();
  const prefix = ensureDir(path.join(workdir(), 'harness'));
  const spec = packageSpec(config);
  console.log(`Installing ${spec}`);
  const result = spawnSync(
    'npm',
    ['install', '--global', '--prefix', prefix, '--no-audit', '--no-fund', spec],
    { stdio: 'inherit', timeout: 10 * 60 * 1000 }
  );
  if (result.status !== 0) throw new Error(`installing ${spec} failed`);
});
