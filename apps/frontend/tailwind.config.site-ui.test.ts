// @vitest-environment node
//
// Tailwind emits a utility only if it finds the class name in a `content` file,
// so a module compiled from outside `src/` rendered markup whose classes the
// stylesheet did not contain. Runs Tailwind the way the build does — its own
// loader reading this directory's `tailwind.config.ts`, with the module selected
// in the environment — in a child process, because a loaded config is cached
// per process and each case needs its own selection.

import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const frontendDir = __dirname;

/** A class name nothing in `src/` uses. */
const MODULE_ONLY = 'bg-[#0a1b2c]';

function utilities(env: Record<string, string>): string {
  const script =
    "require('postcss')([require('tailwindcss')({ config: './tailwind.config.ts' })])" +
    ".process('@tailwind utilities;', { from: undefined })" +
    '.then((result) => process.stdout.write(result.css));';
  return execFileSync('node', ['-e', script], {
    cwd: frontendDir,
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

describe('Tailwind scans the selected Site UI module', () => {
  let outside: string;

  beforeEach(() => {
    outside = mkdtempSync(join(tmpdir(), 'site-ui-tailwind-'));
    mkdirSync(join(outside, 'parts'));
    writeFileSync(
      join(outside, 'manifest.json'),
      JSON.stringify({ id: 'outside', site_ui_api: 1, locale: 'en' }),
    );
    writeFileSync(
      join(outside, 'client.tsx'),
      "export { Landing } from './parts/landing';\n" +
        "export const descriptor = { siteUiApi: 1, id: 'outside', locale: 'en' } as const;\n",
    );
    writeFileSync(
      join(outside, 'parts', 'landing.tsx'),
      `export function Landing() {\n  return <main className="${MODULE_ONLY}" />;\n}\n`,
    );
    writeFileSync(join(outside, 'server.ts'), 'export {};\n');
    writeFileSync(join(outside, 'styles.css'), '');
  });

  afterEach(() => {
    rmSync(outside, { recursive: true, force: true });
  });

  it('emits a utility used only by a module outside src/', () => {
    const selected = utilities({ SITE_UI_DIR: outside, SITE_UI_API: '1' });
    const neutral = utilities({ SITE_UI_DIR: '', SITE_UI_API: '' });

    expect(selected).toContain('0a1b2c');
    // The control: without the module selected, nothing else supplies it.
    expect(neutral).not.toContain('0a1b2c');
  }, 60_000);
});
