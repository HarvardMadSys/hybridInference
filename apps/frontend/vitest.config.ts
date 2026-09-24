import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';

// The generated stub is written by the same resolver the Next config calls, so
// tests exercise the *resolved* module rather than a second, hand-maintained
// alias. Without this, a distribution build's `npm run test` would assert
// against the neutral UI while the build compiled in the distribution's — the
// exact disagreement the resolver exists to prevent.
const require_ = createRequire(import.meta.url);
const { prepareSiteUi, vitestAlias } = require_('./src/site-ui/resolve.js') as {
  prepareSiteUi: (dir: string) => unknown;
  vitestAlias: (dir: string, resolution: unknown) => Record<string, string>;
};

const frontendDir = __dirname;
const siteUi = prepareSiteUi(frontendDir);

export default defineConfig({
  test: {
    setupFiles: ['./vitest.setup.ts'],
  },
  resolve: {
    alias: {
      '@': resolve(frontendDir, 'src'),
      ...vitestAlias(frontendDir, siteUi),
    },
  },
  oxc: {
    jsx: { runtime: 'automatic' },
  },
});
