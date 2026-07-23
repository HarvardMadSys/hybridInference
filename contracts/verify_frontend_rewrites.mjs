#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const inventoryPath = path.join(repoRoot, 'contracts/frontend-backend-routes.v1.json');
const nextConfigPath = path.join(repoRoot, 'apps/frontend/next.config.js');
const backendSentinel = 'http://phase2-backend.invalid';

process.env.BACKEND_INTERNAL_URL = backendSentinel;
const require = createRequire(import.meta.url);
delete require.cache[require.resolve(nextConfigPath)];
const config = require(nextConfigPath);
const configured = await config.rewrites();
const rewrites = Array.isArray(configured)
  ? configured
  : [
      ...(configured.beforeFiles || []),
      ...(configured.afterFiles || []),
      ...(configured.fallback || []),
    ];

const actual = rewrites
  .filter((rewrite) => rewrite.destination.startsWith(backendSentinel))
  .map((rewrite) => [
    rewrite.source,
    rewrite.destination.slice(backendSentinel.length),
  ])
  .sort(([leftSource, leftDestination], [rightSource, rightDestination]) =>
    `${leftSource}\0${leftDestination}`.localeCompare(`${rightSource}\0${rightDestination}`),
  );
const inventory = JSON.parse(fs.readFileSync(inventoryPath, 'utf8'));
const declared = inventory.routes
  .map((rewrite) => [rewrite.source, rewrite.destination])
  .sort(([leftSource, leftDestination], [rightSource, rightDestination]) =>
    `${leftSource}\0${leftDestination}`.localeCompare(`${rightSource}\0${rightDestination}`),
  );

if (new Set(actual.map((item) => JSON.stringify(item))).size !== actual.length) {
  throw new Error('next.config.js contains duplicate backend rewrites');
}
if (JSON.stringify(actual) !== JSON.stringify(declared)) {
  throw new Error(
    `frontend/backend rewrite inventory is stale\nactual=${JSON.stringify(actual)}\ndeclared=${JSON.stringify(declared)}`,
  );
}

process.stdout.write(`Verified ${actual.length} classified frontend/backend rewrites.\n`);
