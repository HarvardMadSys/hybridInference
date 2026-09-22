#!/usr/bin/env node
'use strict';

/**
 * The one entry point that resolves which public-site UI a command runs against.
 *
 * Every tool that reads the application's sources needs this to have happened
 * first, and each of them needs it for a different reason:
 *
 * - `next dev` / `next build` call it from `next.config.js` as a side effect of
 *   loading the config;
 * - `vitest` calls it from `vitest.config.ts`, the same way;
 * - `tsc` cannot call anything, because it only reads a project file.
 *
 * `npm run type-check` calls this script before reading the generated project,
 * so clean checkouts and changed module selections resolve independently of
 * earlier commands.
 *
 * Run it explicitly, or let one of the configs above run it for you. It is
 * idempotent, so running it twice costs a few milliseconds.
 *
 * Usage: node src/site-ui/prepare.js [--quiet]
 */

// eslint-disable-next-line @typescript-eslint/no-require-imports
const path = require('node:path');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { prepareSiteUi } = require('./resolve.js');

const frontendDir = path.resolve(__dirname, '..', '..');

try {
  const resolution = prepareSiteUi(frontendDir);
  if (!process.argv.includes('--quiet')) {
    process.stdout.write(
      `[site-ui] prepared the ${resolution.kind} Site UI${
        resolution.kind === 'distribution' ? ` ('${resolution.id}')` : ''
      }, API v${resolution.api}\n`,
    );
  }
} catch (error) {
  process.stderr.write(`[site-ui] ${error.message}\n`);
  process.exit(1);
}
