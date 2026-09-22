// The container build's module handling, tested without a container.
//
// `prepare-module.mjs` is what the official Dockerfile runs, so its rules are
// the build's rules: which module gets compiled in, where its assets land, and
// what makes a bundle complete. Each of those has a failure mode that produces a
// *successful* build of the wrong thing — a context silently ignored, an asset
// that never ships, a manifest left behind by a previous build — so they are
// pinned here rather than observed in a Docker log.

import { execFileSync } from 'node:child_process';
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '..', '..');
const script = path.join(frontend, 'scripts', 'site-ui', 'prepare-module.mjs');
const exampleModule = path.resolve(
  frontend,
  '..',
  '..',
  'distributions',
  'example',
  'frontend',
  'site-ui',
);

/** Run the tool, returning its status and output rather than throwing. */
function run(...args: string[]): { status: number; output: string } {
  try {
    const output = execFileSync('node', [script, ...args], {
      encoding: 'utf8',
      stdio: 'pipe',
      env: { ...process.env, SITE_UI_API: '' },
    });
    return { status: 0, output };
  } catch (error) {
    const failure = error as { status?: number; stdout?: string; stderr?: string };
    return {
      status: failure.status ?? 1,
      output: `${failure.stdout ?? ''}${failure.stderr ?? ''}`,
    };
  }
}

describe('the container build\u2019s module handling', () => {
  let scratch: string;

  beforeEach(() => {
    scratch = mkdtempSync(path.join(tmpdir(), 'site-ui-build-'));
  });

  afterEach(() => {
    rmSync(scratch, { recursive: true, force: true });
  });

  it('treats a context with the built-in marker as "no external module"', () => {
    const context = path.join(scratch, 'context');
    mkdirSync(context);
    writeFileSync(path.join(context, '.built-in-ui'), '');
    const into = path.join(scratch, 'staged');

    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      context,
      '--subdir',
      '.',
      '--into',
      into,
    );

    expect(result.status).toBe(0);
    expect(result.output).toContain('no external module');
    // Nothing is staged, and the build environment says so: an unset
    // `SITE_UI_DIR` is the neutral build, which is what this Dockerfile produced
    // before modules existed.
    expect(existsSync(into)).toBe(false);
    expect(existsSync(path.join(scratch, '.site-ui-build-env'))).toBe(false);
  });

  it('stages an external module and records which one', () => {
    const into = path.join(scratch, 'src', 'site-ui', 'external');

    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      exampleModule,
      '--subdir',
      '.',
      '--into',
      into,
      '--api',
      '1',
    );

    expect(result.status).toBe(0);
    expect(result.output).toContain("staged the 'example' module");
    expect(existsSync(path.join(into, 'client.tsx'))).toBe(true);
    // The environment the Dockerfile sources, which is how the decision reaches
    // `next build`. Without it the build compiles the neutral UI while the log
    // says a module was staged.
    expect(readFileSync(path.join(scratch, '.site-ui-build-env'), 'utf8')).toContain(
      `SITE_UI_DIR='${into}'`,
    );
  });

  it('requires an explicit API for external modules before staging', () => {
    const into = path.join(scratch, 'staged');
    const result = run('prepare', '--app', scratch, '--context', exampleModule, '--into', into);
    expect(result.status).toBe(1);
    expect(result.output).toContain('required for an external module');
    expect(existsSync(into)).toBe(false);
  });

  it('selects the staged module only with the documented environment', () => {
    const into = path.join(scratch, 'src', 'site-ui', 'external');
    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      exampleModule,
      '--into',
      into,
      '--api',
      '1',
    );
    expect(result.status).toBe(0);
    const resolver = path.join(frontend, 'src/site-ui/resolve.js');
    const command = `const r = require(${JSON.stringify(resolver)}); process.stdout.write(r.resolveSiteUi(process.cwd()).id)`;
    const invoke = (selected: boolean) =>
      execFileSync('node', ['-e', command], {
        cwd: scratch,
        encoding: 'utf8',
        env: {
          ...process.env,
          SITE_UI_DIR: selected ? into : '',
          SITE_UI_API: selected ? '1' : '',
        },
      });
    expect(invoke(true)).toBe('example');
    expect(invoke(false)).toBe('neutral');
  });

  it('refuses a context that claims to carry a module and does not', () => {
    // The failure this prevents is the quiet one: an empty or wrong directory
    // falls back to the neutral UI and publishes a site whose home page
    // reverted, with nothing in the build log to say so.
    const context = path.join(scratch, 'empty');
    mkdirSync(context);

    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      context,
      '--subdir',
      '.',
      '--into',
      path.join(scratch, 'staged'),
      '--api',
      '1',
    );

    expect(result.status).toBe(1);
    expect(result.output).toContain('is not usable');
  });

  it('refuses a module for another interface revision', () => {
    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      exampleModule,
      '--subdir',
      '.',
      '--into',
      path.join(scratch, 'staged'),
      '--api',
      '2',
    );

    expect(result.status).toBe(1);
    expect(result.output).toMatch(/implements Site UI API v1/);
  });

  it('refuses to read a module through a path that leaves the context', () => {
    const context = path.join(scratch, 'context');
    mkdirSync(context);
    const outside = path.join(scratch, 'outside');
    mkdirSync(outside);
    cpSync(exampleModule, outside, { recursive: true });

    const result = run(
      'prepare',
      '--app',
      scratch,
      '--context',
      context,
      '--subdir',
      '../outside',
      '--into',
      path.join(scratch, 'staged'),
      '--api',
      '1',
    );

    expect(result.status).toBe(1);
    expect(result.output).toContain('outside');
  });

  describe('assets', () => {
    /** An application whose build produced a bundle, and a module beside it. */
    function scaffold(assets: Record<string, string>, owned: Record<string, string> = {}) {
      const app = path.join(scratch, 'app');
      const moduleDir = path.join(app, 'src', 'site-ui', 'external');
      const generated = path.join(app, 'src', 'generated', 'distribution-ui');
      mkdirSync(moduleDir, { recursive: true });
      mkdirSync(generated, { recursive: true });
      mkdirSync(path.join(app, '.next', 'standalone', 'public'), { recursive: true });
      writeFileSync(
        path.join(generated, 'manifest.json'),
        JSON.stringify({ id: 'example', kind: 'distribution' }),
      );

      for (const [relative, body] of Object.entries(assets)) {
        const full = path.join(moduleDir, 'public', relative);
        mkdirSync(path.dirname(full), { recursive: true });
        writeFileSync(full, body);
      }
      for (const [relative, body] of Object.entries(owned)) {
        const full = path.join(app, 'public', relative);
        mkdirSync(path.dirname(full), { recursive: true });
        writeFileSync(full, body);
      }
      return app;
    }

    it('merges the module\u2019s own assets under its own id', () => {
      const app = scaffold({ 'site-assets/example/mark.svg': '<svg/>' });

      const result = run('merge', '--repo-root', scratch, '--app', path.relative(scratch, app));

      expect(result.status).toBe(0);
      expect(result.output).toContain("1 'example' asset(s)");
      expect(
        existsSync(path.join(app, '.next/standalone/public/site-assets/example/mark.svg')),
      ).toBe(true);
    });

    it('refuses an asset outside the module\u2019s own id', () => {
      // The rule that makes "one module cannot shadow another, or the
      // application" true, and the only place that knows both trees.
      const app = scaffold({ 'site-assets/somewhere-else/mark.svg': '<svg/>' });

      const result = run('merge', '--repo-root', scratch, '--app', path.relative(scratch, app));

      expect(result.status).toBe(1);
      expect(result.output).toContain('outside site-assets/example/');
    });

    it('refuses an asset the application already ships', () => {
      const app = scaffold(
        { 'site-assets/example/mark.svg': '<svg>module</svg>' },
        { 'site-assets/example/mark.svg': '<svg>application</svg>' },
      );

      const result = run('merge', '--repo-root', scratch, '--app', path.relative(scratch, app));

      expect(result.status).toBe(1);
      expect(result.output).toContain('collide');
    });

    it('packages host public assets even without a module or a traced server import', () => {
      const app = scaffold({}, { 'untraced.svg': '<svg>host asset</svg>' });
      const result = run('merge', '--repo-root', scratch, '--app', path.relative(scratch, app));
      expect(result.status).toBe(0);
      expect(readFileSync(path.join(app, '.next/standalone/public/untraced.svg'), 'utf8')).toBe(
        '<svg>host asset</svg>',
      );
    });

    it('says so when the module ships no assets', () => {
      const app = scaffold({});

      const result = run('merge', '--repo-root', scratch, '--app', path.relative(scratch, app));

      expect(result.status).toBe(0);
      expect(result.output).toContain('no public assets');
    });
  });

  it('refuses a bundle that is missing its compiled chunks', () => {
    const app = path.join(scratch, 'app');
    mkdirSync(path.join(app, '.next', 'standalone'), { recursive: true });
    mkdirSync(path.join(app, 'src', 'generated', 'distribution-ui'), { recursive: true });
    writeFileSync(
      path.join(app, 'src/generated/distribution-ui/manifest.json'),
      JSON.stringify({ id: 'example', kind: 'distribution' }),
    );

    const result = run('check', '--repo-root', scratch, '--app', path.relative(scratch, app));

    expect(result.status).toBe(1);
    expect(result.output).toContain('.next/static');
  });
});
