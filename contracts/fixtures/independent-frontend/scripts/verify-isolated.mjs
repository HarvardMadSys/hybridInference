import { spawnSync } from 'node:child_process';
import { cp, mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const sourceRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const scratchRoot = await mkdtemp(join(tmpdir(), 'hybridinference-contract-fixture-'));
const isolatedRoot = join(scratchRoot, 'fixture');
const npmCli = process.env.npm_execpath;

function runNpm(...args) {
  if (!npmCli) throw new Error('npm_execpath is required for isolated verification');
  const result = spawnSync(process.execPath, [npmCli, ...args], {
    cwd: isolatedRoot,
    env: process.env,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`npm ${args.join(' ')} failed with status ${result.status}`);
  }
}

try {
  await mkdir(isolatedRoot);
  for (const entry of [
    'README.md',
    'package.json',
    'package-lock.json',
    'scripts',
    'src',
    'tests',
  ]) {
    await cp(join(sourceRoot, entry), join(isolatedRoot, entry), {
      recursive: true,
    });
  }

  runNpm('ci', '--ignore-scripts');
  runNpm('run', 'lint');
  runNpm('test');
  runNpm('run', 'build');
  runNpm('pack', '--dry-run');
} finally {
  await rm(scratchRoot, { recursive: true, force: true });
}
