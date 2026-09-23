import { execFileSync } from 'node:child_process';
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, relative } from 'node:path';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const require_ = createRequire(import.meta.url);

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

const FIXTURE = join(frontendDir, 'tests', 'fixtures', 'site-ui-demo');
const EXAMPLE = join(frontendDir, '..', '..', 'distributions', 'example', 'frontend', 'site-ui');

/**
 * A private copy of what the resolver reads from `apps/frontend`.
 *
 * Resolving *writes*: `src/site-ui/active/`, the build manifest and
 * `tsconfig.generated.json` are regenerated on every call. In the real tree
 * those files are shared with every other test file in the run —
 * `SiteUiBoundary.test.tsx` loads the compiled-in module through the bridge —
 * and Vitest runs files in parallel, so a test here that selected the fixture
 * could change which module another file imported. That was why tests in both
 * files failed intermittently. Each test here resolves against its own copy and
 * never writes the shared state.
 *
 * The copy holds only what resolution reads, at the paths it has in the
 * application: the base TypeScript project, the neutral module and the fixture.
 */
type Sandbox = {
  /** The stand-in for `apps/frontend`. */
  app: string;
  /** The fixture module, copied to `tests/fixtures/site-ui-demo` inside it. */
  fixture: string;
  bridge: string;
  manifest: string;
  tsconfig: string;
};

function sandbox(): Sandbox {
  const app = mkdtempSync(join(tmpdir(), 'site-ui-frontend-'));
  cpSync(join(frontendDir, 'tsconfig.json'), join(app, 'tsconfig.json'));
  cpSync(join(frontendDir, 'src', 'site-ui', 'neutral'), join(app, 'src', 'site-ui', 'neutral'), {
    recursive: true,
  });
  const fixture = join(app, 'tests', 'fixtures', 'site-ui-demo');
  cpSync(FIXTURE, fixture, { recursive: true });
  return {
    app,
    fixture,
    bridge: join(app, 'src', 'site-ui', 'active'),
    manifest: join(app, 'src', 'generated', 'distribution-ui', 'manifest.json'),
    tsconfig: join(app, 'tsconfig.generated.json'),
  };
}

/** Read the generated files as a set, so a stale leftover is visible. */
function generatedState(box: Sandbox) {
  const read = (file: string) => (existsSync(file) ? readFileSync(file, 'utf8') : null);
  return {
    client: read(join(box.bridge, 'client.ts')),
    server: read(join(box.bridge, 'server.ts')),
    styles: read(join(box.bridge, 'styles.css')),
    manifest: read(box.manifest),
    tsconfig: read(box.tsconfig),
  };
}

/**
 * A minimal valid module in `dir`, with any file replaced, added or (`null`)
 * left out.
 */
function writeModule(dir: string, files: Record<string, string | null> = {}): string {
  const base: Record<string, string | null> = {
    'manifest.json': JSON.stringify({ id: 'minimal', site_ui_api: 1, locale: 'en' }),
    'client.tsx':
      "export const descriptor = { siteUiApi: 1, id: 'minimal', locale: 'en' } as const;\n" +
      'export function Landing() {\n  return null;\n}\n',
    'server.ts': 'export {};\n',
    'styles.css': '',
  };
  for (const [name, text] of Object.entries({ ...base, ...files })) {
    if (text === null) continue;
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), text);
  }
  return dir;
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
  let box: Sandbox;

  beforeAll(() => {
    scratch = mkdtempSync(join(tmpdir(), 'site-ui-'));
  });

  afterAll(() => {
    rmSync(scratch, { recursive: true, force: true });
  });

  beforeEach(() => {
    box = sandbox();
  });

  afterEach(() => {
    rmSync(box.app, { recursive: true, force: true });
  });

  it('compiles the neutral UI when nothing is configured', () => {
    const resolution = resolve.prepareSiteUi(box.app, {});

    expect(resolution.kind).toBe('neutral');
    expect(resolution.id).toBe('neutral');
    expect(resolution.api).toBe(1);
    expect(resolution.moduleDir).toBe(join(box.app, 'src', 'site-ui', 'neutral'));
  });

  it('treats an empty or whitespace SITE_UI_DIR as no extension at all', () => {
    // Shells hand out empty strings for unset variables. Treating `''` as a
    // request would make every unconfigured build fail, which is the opposite
    // of the rule that an *absent* extension means the neutral UI.
    for (const value of ['', '   ', '\t']) {
      const resolution = resolve.prepareSiteUi(box.app, { SITE_UI_DIR: value });
      expect(resolution.kind).toBe('neutral');
    }
  });

  it('compiles an external module when one is configured', () => {
    const resolution = resolve.prepareSiteUi(box.app, {
      SITE_UI_DIR: box.fixture,
      SITE_UI_API: '1',
    });

    expect(resolution.kind).toBe('distribution');
    expect(resolution.id).toBe('site-ui-demo');
    expect(resolution.client).toBe(join(box.fixture, 'client.tsx'));
    expect(resolution.server).toBe(join(box.fixture, 'server.ts'));
    expect(resolution.styles).toBe(join(box.fixture, 'styles.css'));
  });

  it('accepts the public homepage-only example and defaults its empty server entry', () => {
    const resolution = resolve.prepareSiteUi(box.app, { SITE_UI_DIR: EXAMPLE, SITE_UI_API: '1' });
    expect(resolution.id).toBe('example');
    const source = readFileSync(join(box.bridge, 'server.ts'), 'utf8');
    const { outputText } = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS },
    });
    const exports = {};
    runInNewContext(outputText, { exports, require: () => ({}) });
    expect(exports).toEqual({ locale: '' });
  });

  it('exposes only the consumed server locale, not arbitrary module exports', () => {
    resolve.prepareSiteUi(box.app, { SITE_UI_DIR: box.fixture, SITE_UI_API: '1' });
    const source = readFileSync(join(box.bridge, 'server.ts'), 'utf8');
    const { outputText } = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS },
    });
    const exports = {};
    runInNewContext(outputText, {
      exports,
      require: () => ({ locale: 'en-GB', metadata: { title: 'Unused' }, internal: true }),
    });
    expect(exports).toEqual({ locale: 'en-GB' });
  });

  it('resolves a relative SITE_UI_DIR against the application, not the caller', () => {
    // The relative path is the subject: it must resolve against the frontend
    // directory the resolver is given rather than against `process.cwd()`. The
    // two differ here — this file runs from the real `apps/frontend`, which has
    // a fixture at the same relative path — so resolving against the working
    // directory would select the wrong copy rather than fail.
    const resolution = resolve.prepareSiteUi(box.app, {
      SITE_UI_DIR: 'tests/fixtures/site-ui-demo',
      SITE_UI_API: '1',
    });
    expect(process.cwd()).not.toBe(box.app);
    expect(resolution.moduleDir).toBe(box.fixture);
  });

  describe('a configured extension that cannot be used fails the build', () => {
    // This is the rule that matters most: silently falling back to the neutral
    // UI would publish a site whose home page and sign-in frame are the wrong
    // design, and nothing in the build log would say so.
    const expectFailure = (env: Record<string, string>, pattern: RegExp) => {
      expect(() => resolve.prepareSiteUi(box.app, env)).toThrow(pattern);
    };

    /** A copy of the fixture, for a test that breaks it. */
    const copyFixture = (name: string) => {
      const copy = join(scratch, name);
      rmSync(copy, { recursive: true, force: true });
      cpSync(FIXTURE, copy, { recursive: true });
      return copy;
    };

    it('rejects a missing API revision', () => {
      expectFailure({ SITE_UI_DIR: box.fixture }, /SITE_UI_API/);
    });

    it('rejects a non-integer API revision', () => {
      expectFailure({ SITE_UI_DIR: box.fixture, SITE_UI_API: 'one' }, /must be an integer/);
    });

    it('rejects a module written for another interface revision', () => {
      expectFailure(
        { SITE_UI_DIR: box.fixture, SITE_UI_API: '2' },
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
      const incomplete = copyFixture('incomplete');
      rmSync(join(incomplete, 'server.ts'));

      expectFailure({ SITE_UI_DIR: incomplete, SITE_UI_API: '1' }, /server entry is missing/);
    });

    it('rejects a module that is missing a required export', () => {
      const partial = copyFixture('partial');
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
      // account pages sit inside the console container.
      const partial = copyFixture('no-auth-frame');
      const client = join(partial, 'client.tsx');
      writeFileSync(
        client,
        readFileSync(client, 'utf8').replace('export function AuthFrame', 'function AuthFrame'),
      );

      const resolution = resolve.prepareSiteUi(box.app, {
        SITE_UI_DIR: partial,
        SITE_UI_API: '1',
      });
      expect(resolution.kind).toBe('distribution');
      expect(resolution.id).toBe('site-ui-demo');
    });

    it('rejects a module that imports an internal of the shared application', () => {
      const deep = copyFixture('deep-import');
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
      const dep = copyFixture('third-party');
      const client = join(dep, 'client.tsx');
      writeFileSync(client, `import { clsx } from 'clsx';\n${readFileSync(client, 'utf8')}`);

      expectFailure({ SITE_UI_DIR: dep, SITE_UI_API: '1' }, /imports 'clsx'/);
    });
  });

  it('points the bundler, TypeScript and Vitest at the same module', () => {
    const resolution = resolve.prepareSiteUi(box.app, {
      SITE_UI_DIR: box.fixture,
      SITE_UI_API: '1',
    });
    const alias = resolve.webpackAlias(box.app, resolution);
    const tsconfig = JSON.parse(readFileSync(box.tsconfig, 'utf8'));

    // The bundler resolves the bare specifiers to the bridge, the bridge
    // forwards to the module…
    expect(alias['@site-ui/client']).toBe(join(box.bridge, 'client'));
    expect(readFileSync(join(box.bridge, 'client.ts'), 'utf8')).toContain(
      'fixtures/site-ui-demo/client',
    );

    // TypeScript reads the client directly and the normalized server facade,
    // so a server entry without locale uses the same default as Webpack.
    expect(tsconfig.compilerOptions.paths['@site-ui/client']).toEqual([
      'tests/fixtures/site-ui-demo/client.tsx',
    ]);
    expect(tsconfig.compilerOptions.paths['@site-ui/server']).toEqual([
      'src/site-ui/active/server.ts',
    ]);

    // The application's own mappings survive: `paths` replaces rather than
    // merges, so a generated project that dropped `@/*` would break every
    // import in the app.
    expect(tsconfig.compilerOptions.paths['@/*']).toEqual(['./src/*']);
  });

  it('records what the build compiled in, without any secret', () => {
    const resolution = resolve.prepareSiteUi(box.app, {
      SITE_UI_DIR: box.fixture,
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
    resolve.prepareSiteUi(box.app, { SITE_UI_DIR: box.fixture, SITE_UI_API: '1' });
    const withModule = generatedState(box);
    expect(withModule.client).toContain('site-ui-demo');

    resolve.prepareSiteUi(box.app, {});
    const neutral = generatedState(box);

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

  it('is idempotent, and a repeat resolution rewrites nothing', () => {
    // Byte-identical output is what keeps a second build from being a change.
    // Not rewriting it at all is what keeps a concurrent reader — another test
    // file, or `next dev` watching the bridge — from ever seeing it truncated.
    resolve.generateSiteUi(box.app, { SITE_UI_DIR: box.fixture, SITE_UI_API: '1' });
    const first = generatedState(box);
    const files = [
      join(box.bridge, 'client.ts'),
      join(box.bridge, 'server.ts'),
      join(box.bridge, 'styles.css'),
      box.manifest,
      box.tsconfig,
    ];
    // Backdated, so a rewrite within the same clock tick cannot look like none.
    const past = new Date('2020-01-01T00:00:00Z');
    for (const file of files) utimesSync(file, past, past);

    resolve.generateSiteUi(box.app, { SITE_UI_DIR: box.fixture, SITE_UI_API: '1' });

    expect(generatedState(box)).toEqual(first);
    for (const file of files) expect(statSync(file).mtimeMs, file).toBe(past.getTime());
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

describe('an entry names exactly one file', () => {
  // The bridge forwards to an extensionless `<module>/client`, and webpack,
  // Vite and TypeScript each complete that name in a different order. With
  // `client.tsx` and `client.js` side by side, the checks and the type checker
  // read the first while webpack bundled the second: the image shipped a file
  // nothing had looked at.
  let box: Sandbox;

  beforeEach(() => {
    box = sandbox();
  });

  afterEach(() => {
    rmSync(box.app, { recursive: true, force: true });
  });

  const select = (dir: string) => ({ SITE_UI_DIR: dir, SITE_UI_API: '1' });

  it.each([
    ['client', 'client.js'],
    ['client', 'client.ts'],
    ['client', 'client.mjs'],
    ['server', 'server.js'],
    ['server', 'server.d.ts'],
  ])('refuses a %s entry that also exists as %s', (entry, extra) => {
    const dir = writeModule(join(box.app, 'modules', 'ambiguous'), {
      [extra]: 'export const descriptor = {};\n',
    });

    expect(() => resolve.prepareSiteUi(box.app, select(dir))).toThrow(
      new RegExp(`more than one ${entry} entry: .*${extra.replace('.', '\\.')}`),
    );
    // Refused before anything is generated for it.
    expect(existsSync(box.tsconfig)).toBe(false);
  });

  it('reads, checks, bridges and type-checks the same file, whatever its extension', () => {
    const dir = writeModule(join(box.app, 'modules', 'javascript'), {
      'manifest.json': JSON.stringify({ id: 'javascript', site_ui_api: 1, locale: 'en' }),
      'client.tsx': null,
      'client.jsx':
        "export const descriptor = { siteUiApi: 1, id: 'javascript', locale: 'en' };\n" +
        'export function Landing() {\n  return <main />;\n}\n',
      'server.ts': null,
      'server.js': "export const locale = 'en';\n",
    });

    const resolution = resolve.prepareSiteUi(box.app, select(dir));
    const client = join(dir, 'client.jsx');

    // The checks and the descriptor reader: the id came from this file.
    expect(resolution.client).toBe(client);
    expect(resolution.server).toBe(join(dir, 'server.js'));
    expect(resolution.id).toBe('javascript');
    // TypeScript.
    const tsconfig = JSON.parse(readFileSync(box.tsconfig, 'utf8'));
    expect(tsconfig.compilerOptions.paths['@site-ui/client']).toEqual([relative(box.app, client)]);
    // Webpack and Vitest: the alias names the bridge, and the one name the
    // bridge forwards to is completed by exactly one file.
    expect(resolve.webpackAlias(box.app, resolution)['@site-ui/client']).toBe(
      join(box.bridge, 'client'),
    );
    const bridge = readFileSync(join(box.bridge, 'client.ts'), 'utf8');
    const target = join(box.bridge, JSON.parse(/export \* from ("[^"]+")/.exec(bridge)![1]));
    expect(
      readdirSync(dirname(target)).filter((file) => file.startsWith(`${basename(target)}.`)),
    ).toEqual(['client.jsx']);
  });

  it.each([
    ['a default function', 'export default function Landing() {\n  return null;\n}\n'],
    ['a default object', 'const Landing = () => null;\nexport default { Landing };\n'],
    [
      'a renamed default',
      'function Landing() {\n  return null;\n}\nexport { Landing as default };\n',
    ],
    ['a forwarded default', "export { default } from './parts/landing';\n"],
  ])('refuses a client entry with %s', (_label, body) => {
    const dir = writeModule(join(box.app, 'modules', 'default-export'), {
      'client.tsx': `export const descriptor = { siteUiApi: 1, id: 'minimal', locale: 'en' } as const;\n${body}`,
      'parts/landing.tsx': 'export default function Landing() {\n  return null;\n}\n',
    });

    expect(() => resolve.prepareSiteUi(box.app, select(dir))).toThrow(/has a default export/);
  });

  it('reads the syntax, so a default export in a comment or a string is not one', () => {
    const dir = writeModule(join(box.app, 'modules', 'mentions-default'), {
      'client.tsx':
        "export const descriptor = { siteUiApi: 1, id: 'minimal', locale: 'en' } as const;\n" +
        '/*\nexport default Landing\n*/\n' +
        "export const sample = 'export default Landing;';\n",
    });

    expect(resolve.prepareSiteUi(box.app, select(dir)).id).toBe('minimal');
  });

  it('leaves the server entry free to have a default export', () => {
    // The server bridge reads `locale` off the namespace and ignores the rest,
    // so a default there is harmless — the fixture has one.
    const dir = writeModule(join(box.app, 'modules', 'server-default'), {
      'server.ts': "export const locale = 'en';\nexport default { locale };\n",
    });

    expect(resolve.prepareSiteUi(box.app, select(dir)).kind).toBe('distribution');
  });
});
