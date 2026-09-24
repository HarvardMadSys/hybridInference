// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { afterAll, describe, expect, it, vi } from 'vitest';

/**
 * The bundler guard, run through the bundler.
 *
 * `containment.js` is only as good as the requests it sees, and what it sees
 * depends on webpack: which resolver a request goes through, whether it carries
 * an issuer, whether it is resolved at all. So every case here is a real
 * compilation with Next's own bundled webpack and css-loader, over a throwaway
 * application laid out the way an image build lays out the real one — a `src/`
 * with the facade and an internal the module must not reach, the application's
 * real `react`, `react-dom` and `next` linked in, and the module staged at
 * `src/site-ui/external`.
 *
 * A full `next build` takes most of a minute, which is too slow per case for a
 * unit suite; each compilation here takes a fraction of a second and runs the
 * same plugins `next.config.js` installs, through the same entry point.
 */

const require_ = createRequire(import.meta.url);
const frontendDir = join(__dirname, '..', '..');

type Config = Record<string, unknown> & { plugins?: unknown[] };
type Stats = { compilation: { errors: { message: string }[] } };
type Compiler = {
  run(callback: (error: Error | null, stats?: Stats) => void): void;
  close(callback: () => void): void;
};

const { webpack } = require_('next/dist/compiled/webpack/bundle5')() as {
  webpack: (config: Config) => Compiler;
};
const { installSiteUiContainment, isFrameworkExternal } = require_('./containment.js') as {
  installSiteUiContainment: (config: Config, frontendDir: string, resolution: object) => Config;
  isFrameworkExternal: (specifier: string) => boolean;
};
const { webpackAlias } = require_('./resolve.js') as {
  webpackAlias: (frontendDir: string, resolution: object) => Record<string, string>;
};
const { cssFileResolve } = require_(
  'next/dist/build/webpack/config/blocks/css/loaders/file-resolve',
) as { cssFileResolve: (url: string, resourcePath: string, urlImports: boolean) => boolean };

const nextDir = dirname(require_.resolve('next/package.json'));
const cssLoader = join(nextDir, 'dist/build/webpack/loaders/css-loader/src/index.js');
const swcHelpers = join(
  dirname(require_.resolve('@swc/helpers/package.json', { paths: [nextDir] })),
  '_',
);

const MODULE = 'src/site-ui/external';

/** The application around the module: the facade, internals, framework. */
const APPLICATION: Record<string, string> = {
  'src/site-ui/host.js': 'export const useT = () => (key, fallback) => fallback;\n',
  'src/site-ui/active/client.js': "export * from '../external/client.js';\n",
  'src/components/secret.js': 'export const secret = 1;\n',
  'src/components/loader.js': 'module.exports = () => "export default 1";\n',
  'src/components/logo.svg': '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
  'src/styles/globals.css': 'body { color: red; }\n',
  'node_modules/outside/package.json': '{ "name": "outside", "main": "index.js" }\n',
  'node_modules/outside/index.js': 'export const outside = 1;\n',
};

const trees: string[] = [];
afterAll(() => {
  for (const tree of trees) rmSync(tree, { recursive: true, force: true });
});

/** A throwaway application with one module in it; `files` are module-relative. */
function application(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), 'site-ui-containment-'));
  trees.push(root);
  mkdirSync(join(root, 'node_modules'));
  for (const name of ['react', 'react-dom', 'next']) {
    symlinkSync(join(frontendDir, 'node_modules', name), join(root, 'node_modules', name));
  }
  const all = { ...APPLICATION };
  for (const [name, text] of Object.entries(files)) all[`${MODULE}/${name}`] = text;
  for (const [name, text] of Object.entries(all)) {
    mkdirSync(dirname(join(root, name)), { recursive: true });
    writeFileSync(join(root, name), text);
  }
  return root;
}

/** Next's css-loader reports into a build trace Next's own plugin provides. */
const traceSpan = {
  traceChild: () => traceSpan,
  traceAsyncFn: (body: (span: unknown) => unknown) => body(traceSpan),
  traceFn: (body: (span: unknown) => unknown) => body(traceSpan),
  setAttribute() {},
  stop() {},
};

function configFor(root: string, options: { target?: string; guard?: boolean; cache?: object }) {
  const resolution = {
    kind: options.guard === false ? 'neutral' : 'distribution',
    moduleDir: join(root, MODULE),
    stub: join(root, 'src/site-ui/active/client.ts'),
  };
  const config: Config = {
    mode: 'development',
    devtool: false,
    context: root,
    target: options.target ?? 'web',
    entry: join(root, 'src/site-ui/active/client.js'),
    output: { path: join(root, '.out') },
    cache: options.cache ?? false,
    infrastructureLogging: { level: 'error' },
    resolve: {
      // The aliases next.config.js registers, and the one Next adds for the
      // helpers its compiler injects.
      alias: {
        ...webpackAlias(root, resolution),
        '@': join(root, 'src'),
        '@swc/helpers/_': swcHelpers,
      },
      extensions: ['.js', '.json'],
    },
    module: {
      rules: [
        { test: /\.svg$/, type: 'asset/resource' },
        {
          test: /\.css$/,
          use: [
            {
              loader: cssLoader,
              options: {
                importLoaders: 0,
                modules: false,
                // What Next's global CSS rule passes: root-relative and URI
                // references are left for the browser.
                url: (url: string, resourcePath: string) =>
                  cssFileResolve(url, resourcePath, false),
                import: (url: string, _media: unknown, resourcePath: string) =>
                  cssFileResolve(url, resourcePath, false),
                postcss: async () => ({ postcss: require_('postcss') }),
              },
            },
          ],
        },
      ],
    },
    plugins: [
      {
        apply(compiler: {
          hooks: { compilation: { tap(name: string, body: (c: unknown) => void): void } };
          webpack: {
            NormalModule: {
              getCompilationHooks(compilation: unknown): {
                loader: { tap(name: string, body: (context: object) => void): void };
              };
            };
          };
        }) {
          compiler.hooks.compilation.tap('trace', (compilation) => {
            compiler.webpack.NormalModule.getCompilationHooks(compilation).loader.tap(
              'trace',
              (context) => Object.assign(context, { currentTraceSpan: traceSpan }),
            );
          });
        },
      },
    ],
  };
  return installSiteUiContainment(config, root, resolution);
}

/** Compile the application; resolve with every error message. */
function compile(
  root: string,
  options: { target?: string; guard?: boolean; cache?: object } = {},
): Promise<string[]> {
  const compiler = webpack(configFor(root, options));
  return new Promise((resolve, reject) => {
    compiler.run((error, stats) => {
      compiler.close(() => {
        if (error || !stats) reject(error ?? new Error('no stats'));
        else resolve(stats.compilation.errors.map(({ message }) => message));
      });
    });
  });
}

/** A module whose client entry runs `clientSource`, with any other files it needs. */
function moduleWith(clientSource: string, files: Record<string, string> = {}) {
  return { 'client.js': `${clientSource}\nexport const descriptor = {};\n`, ...files };
}

// Every case runs one or two real webpack compilations. Alone they take a
// second or two; with the whole suite competing for the CPU they can take
// longer than the default five seconds, which is a timing failure, not a finding.
vi.setConfig({ testTimeout: 60_000 });

describe('a request from the module is judged by the file it resolves to', () => {
  // [case, module files, what the error must name, compile target]
  it.each<[string, Record<string, string>, string[], string?]>([
    [
      'a dynamic import behind a magic comment',
      moduleWith(
        "export const load = () => import(/* webpackChunkName: 'x' */ '@/components/secret');",
      ),
      ["client.js imports '@/components/secret'", 'src/components/secret.js'],
    ],
    [
      'a test helper that production code imports',
      moduleWith("export { secret } from './tests/leak.js';", {
        'tests/leak.js': "export { secret } from '@/components/secret';\n",
      }),
      ["tests/leak.js imports '@/components/secret'"],
    ],
    [
      "a stylesheet's @import of the application's CSS",
      moduleWith("import './styles.css';", {
        'styles.css': "@import '../../styles/globals.css';\n",
      }),
      ["'../../styles/globals.css'", 'src/styles/globals.css'],
    ],
    [
      "a stylesheet's url() of an application file",
      moduleWith("import './styles.css';", {
        'styles.css': ".a { background: url('../../components/logo.svg'); }\n",
      }),
      ["'../../components/logo.svg'", 'src/components/logo.svg'],
    ],
    [
      'a framework prefix walked back out with ..',
      moduleWith("export const deep = () => import('next/../../src/components/secret');"),
      ["'next/../../src/components/secret'", 'src/components/secret.js'],
    ],
    [
      'a template-literal import over an application directory',
      moduleWith('export const load = (name) => import(`@/components/${name}`);'),
      ["computes an import from '@/components'", 'src/components'],
    ],
    [
      'a string-named import binding',
      moduleWith("import { 'secret' as s } from '@/components/secret';\nexport { s };"),
      ["client.js imports '@/components/secret'"],
    ],
    [
      'require.context over an application directory',
      moduleWith("export const all = require.context('@/components', true);"),
      ["computes an import from '@/components'"],
    ],
    [
      'import.meta.webpackContext over an application directory',
      moduleWith("export const all = import.meta.webpackContext('@/components');"),
      ["computes an import from '@/components'"],
    ],
    [
      'new URL of an application file',
      moduleWith("export const u = new URL('../../components/logo.svg', import.meta.url);"),
      ["client.js imports '../../components/logo.svg'"],
    ],
    [
      'a CommonJS require',
      moduleWith("export const s = require('@/components/secret');"),
      ["client.js imports '@/components/secret'"],
    ],
    [
      'an absolute path',
      moduleWith('export const load = () => import(__SECRET__);'),
      ['src/components/secret.js'],
    ],
    [
      'a file: URI',
      moduleWith('export const load = () => import(__SECRET_URI__);'),
      ['src/components/secret.js'],
    ],
    [
      'a data: URI, which webpack reads without resolving',
      moduleWith(
        'export const load = () => import(\'data:text/javascript,export * from "@/components/secret.js"\');',
      ),
      ['a URI the bundler reads without resolving it'],
    ],
    [
      'a Node.js built-in, which the server compilation leaves external',
      moduleWith("import { readFileSync } from 'node:fs';\nexport { readFileSync };"),
      ["client.js imports 'node:fs'", 'runtime require'],
      'node',
    ],
    [
      'an inline loader from the application',
      moduleWith("import raw from '!!../../components/loader.js!./data.json';\nexport { raw };", {
        'data.json': '{}\n',
      }),
      ["names the loader '../../components/loader.js'", "Next's own loaders"],
    ],
    [
      'a package the module carries in its own node_modules',
      moduleWith("export { outside } from 'bundled';", {
        'node_modules/bundled/package.json': '{ "name": "bundled", "main": "index.js" }\n',
        'node_modules/bundled/index.js': 'export const outside = 1;\n',
      }),
      ["client.js imports 'bundled'"],
    ],
    [
      "a package from the application's node_modules",
      moduleWith("export { outside } from 'outside';"),
      ["client.js imports 'outside'", 'node_modules/outside/index.js'],
    ],
    [
      'the bridge back into the application',
      moduleWith("export * from '@site-ui/client';"),
      ["client.js imports '@site-ui/client'"],
    ],
  ])('%s fails the build', async (_case, files, expected, target) => {
    const root = application(files);
    // Absolute requests can only be written once the tree exists.
    const client = join(root, MODULE, 'client.js');
    const source = require_('node:fs').readFileSync(client, 'utf8') as string;
    const secret = join(root, 'src/components/secret.js');
    writeFileSync(
      client,
      source
        .replace('__SECRET__', JSON.stringify(secret))
        .replace('__SECRET_URI__', JSON.stringify(`file://${secret}`)),
    );

    // Without the guard the same module compiles cleanly: the case is a real
    // way past the source check, not a module that was broken anyway.
    expect(await compile(root, { target, guard: false })).toEqual([]);
    const errors = (await compile(root, { target })).join('\n');
    expect(errors).toContain('[site-ui]');
    for (const fragment of expected) expect(errors).toContain(fragment);
  });

  it('a symlink out of the module fails the build', async () => {
    const root = application(moduleWith("export { secret } from './linked.js';"));
    symlinkSync(join(root, 'src/components/secret.js'), join(root, MODULE, 'linked.js'));
    expect(await compile(root, { guard: false })).toEqual([]);
    const errors = (await compile(root)).join('\n');
    expect(errors).toContain("client.js imports './linked.js'");
    expect(errors).toContain('src/components/secret.js');
  });

  it('a cached resolution is judged as well', async () => {
    // The persistent cache outlives a change of module, so a resolution one
    // build recorded unguarded must not be served to a guarded one. A computed
    // import is the case to prove it with: the graph audit re-judges resolved
    // files, but only the resolver ever sees the directory a context ranges over.
    const root = application(
      moduleWith('export const load = (name) => import(`@/components/${name}`);'),
    );
    const cache = {
      type: 'filesystem',
      cacheDirectory: join(root, '.cache'),
      buildDependencies: { config: [] },
    };
    expect(await compile(root, { guard: false, cache })).toEqual([]);
    const errors = (await compile(root, { cache })).join('\n');
    expect(errors).toContain("computes an import from '@/components'");
  });
});

describe('what a module may use keeps building', () => {
  it('compiles a module that uses everything the interface allows', async () => {
    const root = application({
      'client.js': [
        "import React from 'react';",
        "import { createPortal } from 'react-dom';",
        "import empty from 'next/dist/compiled/server-only/empty';",
        "import { useT } from '@site-ui/host';",
        "import { Header } from './parts/Header.js';",
        "import { Header as Linked } from './linked.js';",
        "import './styles.css';",
        // What SWC injects for a private class field; the module never names it.
        "import { _ as privateGet } from '@swc/helpers/_/_class_private_field_get';",
        // Next's own loader, named inline the way its CSS extraction does.
        "import raw from '!!next/dist/build/webpack/loaders/empty-loader.js!./data.json';",
        'export const locale = (lang) => import(`./locales/${lang}.json`);',
        "export const parts = require.context('./parts', false);",
        "export const mark = new URL('./mark.svg', import.meta.url);",
        'export const used = [React, createPortal, empty, useT, Header, Linked, privateGet, raw];',
        'export const descriptor = {};',
      ].join('\n'),
      'parts/Header.js': 'export const Header = () => null;\n',
      'locales/en.json': '{ "hello": "hello" }\n',
      'data.json': '{}\n',
      'mark.svg': '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
      'styles.css': [
        "@import './parts/extra.css';",
        ".a { background: url('./mark.svg'); }",
        // Left for the browser, never resolved by the bundler.
        '.b { background: url(/site-assets/example/mark.svg); }',
        ".c { background: url('data:image/svg+xml;utf8,<svg/>'); }",
      ].join('\n'),
      'parts/extra.css': '.d { color: blue; }\n',
    });
    symlinkSync('./parts/Header.js', join(root, MODULE, 'linked.js'));
    expect(await compile(root)).toEqual([]);
  });

  it('leaves a neutral build alone', () => {
    const config: Config = { plugins: [], resolve: {} };
    installSiteUiContainment(config, frontendDir, { kind: 'neutral' });
    expect(config).toEqual({ plugins: [], resolve: {} });
  });
});

describe('an external is judged by the package it names', () => {
  it.each([
    ['next/dist/compiled/react', true],
    ['react', true],
    ['react-dom/server', true],
    ['next/dist/shared/../../../../src/site-ui/routes', false],
    ['next/./dist', false],
    ['node:fs', false],
    ['fs', false],
    ['styled-jsx', false],
  ])('%s is the framework: %s', (specifier, expected) => {
    expect(isFrameworkExternal(specifier)).toBe(expected);
  });
});
