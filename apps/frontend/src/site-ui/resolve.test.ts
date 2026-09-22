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
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const require_ = createRequire(import.meta.url);

/**
 * A lock around the shared generated state.
 *
 * This file resolves modules, and resolving *writes*: `src/site-ui/active/` and
 * `tsconfig.generated.json` are regenerated on every call. Those are shared with
 * every other test file in the run, and Vitest runs files in parallel — so a
 * file that imports `@/site-ui/module` while this one is mid-write can read
 * either the old module or the new one. That is the mechanism behind a test here
 * failing intermittently and passing on re-run, and it is a property of the
 * global the resolver legitimately owns, not a bad assertion.
 *
 * The lock is a file in the OS temp directory, so it serialises across workers
 * and across processes. It is deliberately coarse: the alternative is giving the
 * resolver a per-run output directory, which is a change to production code to
 * serve a test.
 */
const LOCK = join(tmpdir(), 'site-ui-resolve.lock');

function withSharedState<T>(body: () => T): T {
  const deadline = Date.now() + 30_000;
  let held = false;
  while (!held) {
    try {
      writeFileSync(LOCK, String(process.pid), { flag: 'wx' });
      held = true;
    } catch {
      if (Date.now() > deadline) {
        throw new Error(`timed out waiting for ${LOCK}; remove it if a run was killed`);
      }
      // Busy-wait: the critical sections here are milliseconds of file writes.
      execFileSync('sleep', ['0.05']);
    }
  }
  try {
    return body();
  } finally {
    rmSync(LOCK, { force: true });
  }
}
const frontendDir = join(__dirname, '..', '..');

type Resolution = {
  kind: string;
  id: string;
  api: number;
  moduleDir: string;
  client: string;
  server: string;
  styles: string;
  stub: string;
  manifestPath: string;
  tsconfigPath: string;
};

const resolve = require_(join(frontendDir, 'src', 'site-ui', 'resolve.js')) as {
  prepareSiteUi: (dir: string, env?: Record<string, string>) => Resolution;
  generateSiteUi: (dir: string, env?: Record<string, string>) => Resolution;
  resolveSiteUi: (dir: string, env?: Record<string, string>) => Resolution;
  webpackAlias: (dir: string, resolution: Resolution) => Record<string, string>;
  SiteUiConfigError: new (...args: never[]) => Error;
  assertSiteUiEntries: (resolution: {
    root: string;
    client: string;
    server: string;
    styles: string;
    id?: string;
  }) => void;
};

const BRIDGE = join(frontendDir, 'src', 'site-ui', 'active');
const GENERATED = join(frontendDir, 'src', 'generated', 'distribution-ui');
const TSCONFIG = join(frontendDir, 'tsconfig.generated.json');
const FIXTURE = join(frontendDir, 'tests', 'fixtures', 'site-ui-demo');

/** Read the generated files as a set, so a stale leftover is visible. */
function generatedState() {
  const read = (file: string) => (existsSync(file) ? readFileSync(file, 'utf8') : null);
  return {
    client: read(join(BRIDGE, 'client.ts')),
    server: read(join(BRIDGE, 'server.ts')),
    styles: read(join(BRIDGE, 'styles.css')),
    manifest: read(join(GENERATED, 'manifest.json')),
    tsconfig: read(TSCONFIG),
  };
}

describe('a module declares its identity once', () => {
  // `manifest.json` and the client's `descriptor` are two statements of the same
  // facts, and nothing compared them: the resolver read the descriptor and never
  // opened the manifest, so a module could declare one id in each and build
  // cleanly — the image recording one name while the manifest, which is what a
  // reader and the release tooling see, said another.
  const write = (dir: string, manifest: unknown) => {
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, 'client.tsx'),
      "export const descriptor = { id: 'example', siteUiApi: 1, locale: 'en' };\n" +
        'export function Landing() { return null; }\n',
    );
    writeFileSync(join(dir, 'server.ts'), 'export const locale = "en";\n');
    writeFileSync(join(dir, 'styles.css'), '');
    writeFileSync(join(dir, 'manifest.json'), JSON.stringify(manifest));
  };

  const entries = (dir: string) => ({
    kind: 'distribution',
    root: dir,
    client: join(dir, 'client.tsx'),
    server: join(dir, 'server.ts'),
    styles: join(dir, 'styles.css'),
    id: 'example',
  });

  it.each([
    ['agreeing', { id: 'example', site_ui_api: 1, locale: 'en' }, true],
    ['a conflicting id', { id: 'different-module', site_ui_api: 1, locale: 'en' }, false],
    ['a conflicting locale', { id: 'example', site_ui_api: 1, locale: 'fr' }, false],
    ['an unsupported api', { id: 'example', site_ui_api: 999, locale: 'en' }, false],
  ])('%s manifest', (label, manifest, accepted) => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-manifest-'));
    try {
      write(dir, manifest);
      const run = () => resolve.assertSiteUiEntries(entries(dir));
      if (accepted) expect(run).not.toThrow();
      else expect(run).toThrow(/manifest\.json declares/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('refuses a missing manifest for an external module', () => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-manifest-'));
    try {
      write(dir, { id: 'example', site_ui_api: 1, locale: 'en' });
      rmSync(join(dir, 'manifest.json'));
      expect(() => resolve.assertSiteUiEntries(entries(dir))).toThrow(/manifest.json/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('checks typed descriptors and rejects a client-only API mismatch', () => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-manifest-'));
    try {
      write(dir, { id: 'example', site_ui_api: 1, locale: 'en' });
      const client = join(dir, 'client.tsx');
      writeFileSync(
        client,
        "export const descriptor: SiteUiModuleDescriptor = { id: 'example', siteUiApi: 1, locale: 'en' } as const;",
      );
      expect(() => resolve.assertSiteUiEntries(entries(dir))).not.toThrow();
      writeFileSync(client, readFileSync(client, 'utf8').replace('siteUiApi: 1', 'siteUiApi: 2'));
      expect(() => resolve.assertSiteUiEntries(entries(dir))).toThrow(/descriptor declares '2'/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('says which of the two files disagrees', () => {
    // "The module is not usable" would leave the author guessing which
    // declaration to change.
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-manifest-'));
    try {
      write(dir, { id: 'different-module', site_ui_api: 1, locale: 'en' });
      expect(() => resolve.assertSiteUiEntries(entries(dir))).toThrow(
        /manifest\.json declares id 'different-module' but the client's descriptor declares 'example'/,
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('Site UI resolution', () => {
  let scratch: string;

  beforeAll(() => {
    scratch = mkdtempSync(join(tmpdir(), 'site-ui-'));
  });

  afterAll(() => {
    rmSync(scratch, { recursive: true, force: true });
    // Leave the tree in its default configuration: the rest of the suite, and
    // the next `next build`, must see the neutral module.
    withSharedState(() => resolve.generateSiteUi(frontendDir, {}));
  });

  it('compiles the neutral UI when nothing is configured', () => {
    const resolution = resolve.prepareSiteUi(frontendDir, {});

    expect(resolution.kind).toBe('neutral');
    expect(resolution.id).toBe('neutral');
    expect(resolution.api).toBe(1);
    expect(resolution.moduleDir).toBe(join(frontendDir, 'src', 'site-ui', 'neutral'));
  });

  it('treats an empty or whitespace SITE_UI_DIR as no extension at all', () => {
    // Shells hand out empty strings for unset variables. Treating `''` as a
    // request would make every unconfigured build fail, which is the opposite
    // of the rule that an *absent* extension means the neutral UI.
    for (const value of ['', '   ', '\t']) {
      const resolution = resolve.prepareSiteUi(frontendDir, { SITE_UI_DIR: value });
      expect(resolution.kind).toBe('neutral');
    }
  });

  it('compiles an external module when one is configured', () => {
    const resolution = resolve.prepareSiteUi(frontendDir, {
      SITE_UI_DIR: FIXTURE,
      SITE_UI_API: '1',
    });

    expect(resolution.kind).toBe('distribution');
    expect(resolution.id).toBe('site-ui-demo');
    expect(resolution.client).toBe(join(FIXTURE, 'client.tsx'));
    expect(resolution.server).toBe(join(FIXTURE, 'server.ts'));
    expect(resolution.styles).toBe(join(FIXTURE, 'styles.css'));
  });

  it('resolves a relative SITE_UI_DIR against the repository, not the caller', () => {
    // The relative path is the subject: it must resolve against `frontendDir`
    // rather than against `process.cwd()`. The lock is around the call because
    // resolving also *writes* the shared generated state.
    const resolution = withSharedState(() =>
      resolve.prepareSiteUi(frontendDir, {
        SITE_UI_DIR: 'tests/fixtures/site-ui-demo',
        SITE_UI_API: '1',
      }),
    );
    expect(resolution.moduleDir).toBe(FIXTURE);
  });

  describe('a configured extension that cannot be used fails the build', () => {
    // This is the rule that matters most: silently falling back to the neutral
    // UI would publish a site whose home page and sign-in frame are the wrong
    // design, and nothing in the build log would say so.
    const expectFailure = (env: Record<string, string>, pattern: RegExp) => {
      // Resolving writes the generated state even when it then throws, so these
      // go through the lock too.
      expect(() => withSharedState(() => resolve.prepareSiteUi(frontendDir, env))).toThrow(pattern);
    };

    it('rejects a missing API revision', () => {
      expectFailure({ SITE_UI_DIR: FIXTURE }, /SITE_UI_API/);
    });

    it('rejects a non-integer API revision', () => {
      expectFailure({ SITE_UI_DIR: FIXTURE, SITE_UI_API: 'one' }, /must be an integer/);
    });

    it('rejects a module written for another interface revision', () => {
      expectFailure(
        { SITE_UI_DIR: FIXTURE, SITE_UI_API: '2' },
        /implements Site UI API v1.*declares v2/s,
      );
    });

    it('rejects a path that does not exist', () => {
      expectFailure(
        { SITE_UI_DIR: join(scratch, 'nowhere'), SITE_UI_API: '1' },
        /has no client\/server\/styles\.css set/,
      );
    });

    it('rejects a directory that is missing the server entry', () => {
      const incomplete = join(scratch, 'incomplete');
      rmSync(incomplete, { recursive: true, force: true });
      cpSync(FIXTURE, incomplete, { recursive: true });
      rmSync(join(incomplete, 'server.ts'));

      expectFailure({ SITE_UI_DIR: incomplete, SITE_UI_API: '1' }, /server entry is missing/);
    });

    it('rejects a module that is missing a required export', () => {
      const partial = join(scratch, 'partial');
      rmSync(partial, { recursive: true, force: true });
      cpSync(FIXTURE, partial, { recursive: true });
      const client = join(partial, 'client.tsx');
      writeFileSync(
        client,
        readFileSync(client, 'utf8').replace('export const descriptor', 'const descriptor'),
      );

      expectFailure({ SITE_UI_DIR: partial, SITE_UI_API: '1' }, /does not export `descriptor`/);
    });

    it('accepts a module with no account frame, because absence is an answer', () => {
      // The three page-owning frames are optional by design: no `Landing` means
      // the console's landing page is correct, and no `AuthFrame` means the
      // account pages sit inside the console container. Requiring them forced
      // every module to answer, and the neutral module's stub answer — a card —
      // claimed five whole pages and left them without chrome.
      const partial = join(scratch, 'no-auth-frame');
      rmSync(partial, { recursive: true, force: true });
      cpSync(FIXTURE, partial, { recursive: true });
      const client = join(partial, 'client.tsx');
      writeFileSync(
        client,
        readFileSync(client, 'utf8').replace('export function AuthFrame', 'function AuthFrame'),
      );

      const resolution = resolve.prepareSiteUi(frontendDir, {
        SITE_UI_DIR: partial,
        SITE_UI_API: '1',
      });
      expect(resolution.kind).toBe('distribution');
      expect(resolution.id).toBe('site-ui-demo');

      // Leave the shared tree on the neutral bridge: this test generates one,
      // and every later test reads the generated state.
      withSharedState(() => resolve.generateSiteUi(frontendDir, {}));
    });

    it('rejects a module that imports an internal of the shared application', () => {
      const deep = join(scratch, 'deep-import');
      rmSync(deep, { recursive: true, force: true });
      cpSync(FIXTURE, deep, { recursive: true });
      const client = join(deep, 'client.tsx');
      writeFileSync(
        client,
        readFileSync(client, 'utf8').replace(
          "} from '@site-ui/host';",
          "} from '@site-ui/host';\nimport { useSiteConfig as internal } from '@/components/providers/SiteConfigProvider';",
        ),
      );

      expectFailure({ SITE_UI_DIR: deep, SITE_UI_API: '1' }, /imports '@\/components/);
    });

    it('rejects a module that depends on a package the host does not provide', () => {
      const dep = join(scratch, 'third-party');
      rmSync(dep, { recursive: true, force: true });
      cpSync(FIXTURE, dep, { recursive: true });
      const client = join(dep, 'client.tsx');
      writeFileSync(client, `import { clsx } from 'clsx';\n${readFileSync(client, 'utf8')}`);

      expectFailure({ SITE_UI_DIR: dep, SITE_UI_API: '1' }, /imports 'clsx'/);
    });
  });

  it('points the bundler, TypeScript and Vitest at the same module', () => {
    const resolution = resolve.prepareSiteUi(frontendDir, {
      SITE_UI_DIR: FIXTURE,
      SITE_UI_API: '1',
    });
    const alias = resolve.webpackAlias(frontendDir, resolution);
    const tsconfig = JSON.parse(readFileSync(TSCONFIG, 'utf8'));

    // The bundler resolves the bare specifiers to the bridge, the bridge
    // forwards to the module…
    expect(alias['@site-ui/client']).toBe(join(BRIDGE, 'client'));
    expect(readFileSync(join(BRIDGE, 'client.ts'), 'utf8')).toContain(
      'fixtures/site-ui-demo/client',
    );

    // …and `tsc` is pointed straight at the module's own entries, because it
    // does not resolve a directory specifier to an index file.
    expect(tsconfig.compilerOptions.paths['@site-ui/client']).toEqual([
      'tests/fixtures/site-ui-demo/client.tsx',
    ]);
    expect(tsconfig.compilerOptions.paths['@site-ui/server']).toEqual([
      'tests/fixtures/site-ui-demo/server.ts',
    ]);

    // The application's own mappings survive: `paths` replaces rather than
    // merges, so a generated project that dropped `@/*` would break every
    // import in the app.
    expect(tsconfig.compilerOptions.paths['@/*']).toEqual(['./src/*']);
  });

  it('records what the build compiled in, without any secret', () => {
    const resolution = resolve.prepareSiteUi(frontendDir, {
      SITE_UI_DIR: FIXTURE,
      SITE_UI_API: '1',
    });
    const manifest = JSON.parse(readFileSync(resolution.manifestPath, 'utf8'));

    expect(manifest).toMatchObject({
      site_ui_api: 1,
      kind: 'distribution',
      id: 'site-ui-demo',
    });
    // No filesystem path in a file that is copied into the image: see
    // `manifestSource`. What identifies a build is the module's name and the
    // interface revision, both of which hold wherever the build ran.
    expect(Object.keys(manifest).sort()).toEqual(['id', 'kind', 'module', 'site_ui_api'].sort());
    expect(manifest.module).toBe('site-ui-demo');
  });

  it('leaves no trace of a distribution build when the next build is neutral', () => {
    // The failure this pins is a build that inherits the *previous* build's
    // module: a cached bridge, a stale tsconfig or a stylesheet that was
    // imported once and never removed. Any of those publishes one
    // distribution's design from another distribution's build.
    resolve.prepareSiteUi(frontendDir, { SITE_UI_DIR: FIXTURE, SITE_UI_API: '1' });
    const withModule = generatedState();
    expect(withModule.client).toContain('site-ui-demo');

    resolve.prepareSiteUi(frontendDir, {});
    const neutral = generatedState();

    for (const [name, contents] of Object.entries(neutral)) {
      expect(contents, `${name} should have been rewritten`).not.toBeNull();
      expect(contents, `${name} still mentions the fixture`).not.toContain('site-ui-demo');
      expect(contents, `${name} still mentions the fixture directory`).not.toContain(
        'tests/fixtures',
      );
    }
    expect(neutral.client).not.toBe(withModule.client);
    expect(JSON.parse(neutral.manifest!).kind).toBe('neutral');
  });

  it('is idempotent, so a second build is not a spurious change', () => {
    withSharedState(() => {
      resolve.generateSiteUi(frontendDir, { SITE_UI_DIR: FIXTURE, SITE_UI_API: '1' });
      const first = generatedState();
      resolve.generateSiteUi(frontendDir, { SITE_UI_DIR: FIXTURE, SITE_UI_API: '1' });
      expect(generatedState()).toEqual(first);
    });
  });

  it('generates files that are ignored by git', () => {
    // A generated file that git tracks is a file someone will eventually edit.
    const tracked = execFileSync(
      'git',
      ['ls-files', 'src/site-ui/active', 'src/generated', 'tsconfig.generated.json'],
      { cwd: frontendDir, encoding: 'utf8' },
    ).trim();
    expect(tracked).toBe('');
  });
});
