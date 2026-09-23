// @vitest-environment node
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

/**
 * The import checker, against the cases a plausible implementation gets wrong.
 *
 * Every one of these fails *quietly* when it is wrong, which is why they are
 * pinned here rather than left to review:
 *
 * - a reader that treats "inside any string" as documentation recognises no
 *   real import at all, and the build stops rejecting anything;
 * - a reader that treats "inside no string" turns the quickstart's own code
 *   samples into dependencies on packages nobody installed;
 * - a comment, a template literal, a string-named binding or an apostrophe in
 *   JSX text must not hide the request next to it — each of those once did;
 * - `next/` followed by `..` is not a subpath of Next.js;
 * - a test helper that production code imports is production code;
 * - a stylesheet imports too.
 *
 * The bundler guard in `containment.js` is what makes the rule hold; this
 * checker is the early report, and it must never reject a module that guard
 * would build.
 */
const require_ = createRequire(import.meta.url);
const { verifyModuleImports, importSpecifiers, isFrameworkSpecifier } = require_(
  './verify-imports.js',
) as {
  verifyModuleImports: (dir: string) => string[];
  importSpecifiers: (source: string, fileName?: string) => string[];
  isFrameworkSpecifier: (specifier: string) => boolean;
};

/** Run one source file through the checker in its own directory. */
function check(source: string): { recognised: string[]; problems: string[] } {
  const dir = mkdtempSync(join(tmpdir(), 'site-ui-imports-'));
  try {
    writeFileSync(join(dir, 'client.tsx'), `${source}\nexport const descriptor = 1;\n`);
    return {
      recognised: importSpecifiers(readFileSync(join(dir, 'client.tsx'), 'utf8')),
      problems: verifyModuleImports(dir),
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * The same checker, against a module that has neighbours.
 *
 * `check` above writes one file into an empty directory, so every relative path
 * it can build stays inside the module and the containment rule is never
 * exercised. These cases give the module a parent with files in it, which is the
 * shape a real module has: `src/site-ui/external/` sits inside the shared
 * application, and `../../components/...` reaches its internals.
 */
function checkInTree(source: string): string[] {
  const outer = mkdtempSync(join(tmpdir(), 'site-ui-tree-'));
  try {
    const root = join(outer, 'module');
    mkdirSync(join(root, 'auth'), { recursive: true });
    writeFileSync(join(outer, 'sibling.ts'), 'export const s = 1;\n');
    writeFileSync(join(root, 'auth', 'own.ts'), 'export const o = 1;\n');
    writeFileSync(join(root, 'auth', 'client.tsx'), source);
    return verifyModuleImports(root);
  } finally {
    rmSync(outer, { recursive: true, force: true });
  }
}

/**
 * A module with the given files, beside an application with files of its own.
 * Returns the checker's problems; `links` are `[link, target]`, module-relative
 * links pointing at absolute or outer-relative targets.
 */
function checkModule(files: Record<string, string>, links: [string, string][] = []): string[] {
  const outer = mkdtempSync(join(tmpdir(), 'site-ui-module-'));
  try {
    const root = join(outer, 'module');
    mkdirSync(root);
    writeFileSync(join(outer, 'sibling.ts'), 'export const s = 1;\n');
    writeFileSync(join(outer, 'sibling.css'), 'body { color: red; }\n');
    for (const [name, text] of Object.entries(files)) {
      mkdirSync(dirname(join(root, name)), { recursive: true });
      writeFileSync(join(root, name), text);
    }
    for (const [link, target] of links) {
      mkdirSync(dirname(join(root, link)), { recursive: true });
      symlinkSync(target.replace('<outer>', outer), join(root, link));
    }
    return verifyModuleImports(root);
  } finally {
    rmSync(outer, { recursive: true, force: true });
  }
}

describe('the relative-import boundary', () => {
  // The rule's name is "its own files", and recognising `./` or `../` is not the
  // same as staying inside the module. These four are the difference.
  it.each([
    ["import { o } from './own';", true],
    ["import { o } from '../auth/own';", true],
    ["import { s } from '../../sibling';", false],
    ["import { useAuth } from '../../components/providers/AuthProvider';", false],
  ])('%s stays inside the module: %s', (source, allowed) => {
    const problems = checkInTree(source);
    expect(problems.length === 0, problems.join('; ')).toBe(allowed);
  });

  it.each(['linked.ts', 'linked/index.ts'])('refuses a symlink escape resolved as %s', (target) => {
    const outer = mkdtempSync(join(tmpdir(), 'site-ui-symlink-'));
    try {
      const root = join(outer, 'module');
      mkdirSync(root);
      writeFileSync(join(outer, 'outside.ts'), 'export const outside = 1;');
      const link = join(root, target);
      mkdirSync(join(link, '..'), { recursive: true });
      symlinkSync(join(outer, 'outside.ts'), link);
      writeFileSync(join(root, 'client.tsx'), "export { outside } from './linked';");
      expect(verifyModuleImports(root).join('\n')).toMatch(/outside this module/);
    } finally {
      rmSync(outer, { recursive: true, force: true });
    }
  });

  it('accepts a local directory entry and a symlink to a local file', () => {
    const root = mkdtempSync(join(tmpdir(), 'site-ui-local-link-'));
    try {
      mkdirSync(join(root, 'parts'));
      writeFileSync(join(root, 'parts', 'index.ts'), 'export const part = 1;');
      symlinkSync('./parts/index.ts', join(root, 'linked.ts'));
      writeFileSync(
        join(root, 'client.tsx'),
        "export { part } from './parts'; export { part as linked } from './linked';",
      );
      expect(verifyModuleImports(root)).toEqual([]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('says why an escaping relative path is refused', () => {
    // The generic message lists the allowed specifier kinds, which reads as
    // nonsense for a path the author believed was local.
    const [problem] = checkInTree("import { s } from '../../sibling';");
    expect(problem).toMatch(/resolves outside this module/);
  });

  it('judges a request by every file it could mean', () => {
    // Webpack tries `.js` before `.ts`; this checker tries them the other way
    // round. `./x` naming an inside `x.ts` and an escaping `x.js` escapes.
    const problems = checkModule(
      { 'client.tsx': "export { s } from './x';", 'x.ts': 'export const s = 2;\n' },
      [['x.js', '<outer>/sibling.ts']],
    );
    expect(problems.join('\n')).toContain("client.tsx: imports './x'");
  });

  it("refuses a dependency tree inside the module as the module's own files", () => {
    const problems = checkModule({
      'client.tsx': "export { x } from './node_modules/pkg/index.js';",
      'node_modules/pkg/index.js': 'export const x = 1;\n',
    });
    expect(problems.join('\n')).toContain("imports './node_modules/pkg/index.js'");
  });
});

describe('import verification', () => {
  // [source, why, recognised as an import?, allowed by the interface?]
  //
  // Two separate questions on purpose: "recognised" catches a reader that
  // misses real imports or invents them from a sample, "allowed" catches the
  // rules. Collapsing them would let a reader that recognises everything and a
  // checker that rejects nothing look like a pass.
  it.each([
    ["import { x } from '@/components/nope';", 'a deep import into the app', true, false],
    ["import openai from 'openai';", 'a package the host does not provide', true, false],
    ["import 'server-only';", 'a bare side-effect import', true, false],
    ['const { a } = require("lodash");', 'a require of a package', true, false],
    [
      "/** Note the import: `@site-ui/host` then */\nimport { x } from '@/deep';",
      'a deep import after a doc comment containing a backtick',
      true,
      false,
    ],
    [
      "import { useSiteConfig, type AuthFrameProps } from '@site-ui/host';",
      'the facade',
      true,
      true,
    ],
    ["import Link from 'next/link';", 'next', true, true],
    ["import { a } from './local';", 'a relative path', true, true],
    [
      'const sample = \'import OpenAI from "openai";\';',
      'a code sample shown to the reader',
      false,
      true,
    ],
    ['const s = "require(\'bad-pkg\')"', 'a require inside a string', false, true],

    // The ways past the old lexical scanner, each of which built in an image.
    [
      'export const m = () => import(/* webpackChunkName: "x" */ \'@/deep\');',
      'a dynamic import behind a magic comment',
      true,
      false,
    ],
    ["import { x } from /* why */ '@/deep';", 'a comment before the specifier', true, false],
    ['export const m = () => import(`@/deep`);', 'a template-literal specifier', true, false],
    ['export const m = require(`@/deep`);', 'a template-literal require', true, false],
    [
      'export const m = (name: string) => import(`@/components/${name}`);',
      'a computed import over an application directory',
      true,
      false,
    ],
    [
      "export const m = (name: string) => require('@/components/' + name);",
      'a concatenated require',
      true,
      false,
    ],
    [
      "export const s = async () => `${await import('@/deep')}`;",
      'an import inside a template interpolation',
      true,
      false,
    ],
    ["import { 'default' as x } from '@/deep';", 'a string-named import binding', true, false],
    ["import x from 'next/../../src/site-ui/routes';", 'next/ walked back out', true, false],
    ["import x from 'react/../../src/deep';", 'react/ walked back out', true, false],
    [
      "export const c = require.context('@/components', true);",
      'require.context over the application',
      true,
      false,
    ],
    [
      "export const c = (import.meta as unknown as { webpackContext(d: string): unknown }).webpackContext('@/components');",
      'import.meta.webpackContext behind a cast',
      true,
      false,
    ],
    [
      "export const u = new URL('../../escape.ts', import.meta.url);",
      'new URL of a file outside the module',
      true,
      false,
    ],
    [
      "const quote = /'/;\nimport { x } from '@/deep';\nexport { quote, x };",
      'a quote inside a regular expression',
      true,
      false,
    ],
    [
      'export function A() {\n  return <p>We\'re live</p>;\n}\nexport { x } from "@/deep";',
      'an apostrophe in JSX text',
      true,
      false,
    ],
    ["import x = require('@/deep');", 'import-equals', true, false],
    ["type T = typeof import('@/deep');", 'a type-only import', true, false],
    ["export const p = require.resolve('@/deep');", 'require.resolve', true, false],
    ["import x from 'data:text/javascript,export default 1';", 'a data: URI', true, false],
    ["import x from 'file:///etc/hostname';", 'a file: URI', true, false],
    ["import { readFileSync } from 'node:fs';", 'a Node.js built-in', true, false],
    ["import raw from '!!raw-loader!./local.txt';", 'an inline loader', true, false],
    ["import x from '/app/src/site-ui/routes';", 'an absolute path', true, false],

    // What a module may do, however it is spelled.
    ["import { Inter } from 'next/font/google';", 'a next/* subpath', true, true],
    ["import { flushSync } from 'react-dom';", 'react-dom', true, true],
    [
      'export const m = (lang: string) => import(`./locales/${lang}.json`);',
      'a computed import over its own directory',
      true,
      true,
    ],
    [
      "export const c = require.context('./parts', false);",
      'require.context over its own directory',
      true,
      true,
    ],
    [
      "export const u = new URL('./mark.svg', import.meta.url);",
      'new URL of its own asset',
      true,
      true,
    ],
    [
      "export const u = new URL('mark.svg', import.meta.url);",
      'new URL relative without ./',
      true,
      true,
    ],
    ["// import { x } from '@/deep';", 'an import in a comment', false, true],
  ])('%s — %s', (source, _why, recognised, allowed) => {
    const { recognised: found, problems } = check(source);
    expect(found.length > 0, `recognised: ${JSON.stringify(found)}`).toBe(recognised);
    expect(problems.length > 0, `problems: ${JSON.stringify(problems)}`).toBe(!allowed);
  });

  it("exempts a module's own tests and runner config, and only those", () => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-imports-'));
    try {
      writeFileSync(join(dir, 'client.tsx'), 'export const descriptor = 1;\n');
      mkdirSync(join(dir, 'tests'), { recursive: true });
      writeFileSync(
        join(dir, 'tests', 'a.test.tsx'),
        "import { describe } from 'vitest';\nimport { render } from '@testing-library/react';\n",
      );
      writeFileSync(
        join(dir, 'vitest.config.ts'),
        "import { defineConfig } from 'vitest/config';\n",
      );

      // Nothing a *test* imports is compiled into the image, so holding it to
      // the interface's rules would reject every module that has tests at all.
      expect(verifyModuleImports(dir)).toEqual([]);

      // The exemption must not become a hiding place: a component at the module
      // root is still checked, whatever the tests next to it import.
      writeFileSync(join(dir, 'frame.tsx'), "import { x } from '@/internal';\n");
      expect(verifyModuleImports(dir).join('\n')).toContain('@/internal');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it.each([
    ["export { leak } from './tests/leak';", 'a static re-export'],
    ["export const load = () => import('./tests/leak');", 'a dynamic import'],
    ['export const load = (name: string) => import(`./tests/${name}`);', 'a computed import'],
    ["export { leak } from './parts';", 'a file that imports it in turn'],
  ])('checks a test file production code reaches through %s', (source) => {
    const problems = checkModule({
      'client.tsx': source,
      'parts/index.ts': "export { leak } from '../tests/leak';\n",
      'tests/leak.ts': "export { useAuth as leak } from '@/components/providers/AuthProvider';\n",
      '__tests__/unreached.test.ts': "import { it } from 'vitest';\nit('runs', () => {});\n",
    });
    // The helper production code reaches is compiled into the image; the test
    // nothing reaches is not, and keeps its exemption. (A computed import over
    // `./tests/` does reach every file in it, as it does for the bundler.)
    expect(problems.join('\n')).toContain(
      "tests/leak.ts: imports '@/components/providers/AuthProvider'",
    );
    expect(problems.join('\n')).not.toContain('vitest');
  });
});

describe('stylesheets', () => {
  // [stylesheet, allowed?]
  it.each([
    ["@import '../sibling.css';", false],
    ['@import url("../sibling.css") screen;', false],
    [".a { background: url('../sibling.css'); }", false],
    ['.a { background: url(../sibling.css); }', false],
    [".a { composes: b from '../sibling.css'; }", false],
    ["@import 'tailwindcss/base';", false],
    ["@import './own.css';", true],
    ['@import url(own.css);', true],
    ['.a { background: url(own.css); }', true],
    ['.a { background: url(/site-assets/example/mark.svg); }', true],
    [".a { background: url('data:image/svg+xml;utf8,<svg/>'); }", true],
    ['.a { background: url(https://example.com/x.png); }', true],
    ['.a { filter: url(#blur); }', true],
    ["/* @import '../sibling.css'; */", true],
  ])('%s is allowed: %s', (css, allowed) => {
    const problems = checkModule({
      'client.tsx': "import './styles.css';\nexport const descriptor = 1;\n",
      'styles.css': css,
      'own.css': '.own { color: blue; }\n',
    });
    expect(problems.length === 0, problems.join('; ')).toBe(allowed);
  });

  it('reads a stylesheet as stylesheet syntax', () => {
    expect(importSpecifiers("@import './a.css';\n.b { background: url(c.png) }", 'x.css')).toEqual([
      './a.css',
      'c.png',
    ]);
  });
});

describe('symlinks the staging step would copy', () => {
  it.each([
    ['public/site-assets/example/coverage', false],
    ['coverage', false],
    ['node_modules/linked', true],
    ['dist', true],
    ['public/site-assets/example/build', true],
  ])('%s pointing outside the module is allowed: %s', (link, allowed) => {
    // `coverage` is copied into the image with everything else; the names the
    // staging step never copies are the only ones a link may hide behind.
    const problems = checkModule({ 'client.tsx': 'export const descriptor = 1;\n' }, [
      [link, '<outer>'],
    ]);
    expect(problems.length === 0, problems.join('; ')).toBe(allowed);
  });
});

describe('the framework rule', () => {
  it.each([
    ['react', true],
    ['react/jsx-runtime', true],
    ['react-dom', true],
    ['next', true],
    ['next/link', true],
    ['next/font/google', true],
    ['react-dom/client', false],
    ['next/../../src/site-ui/routes', false],
    ['next/./link', false],
    ['next//link', false],
    ['next/dist\\..\\..\\src', false],
    ['nextjs', false],
    ['@next/font', false],
  ])('%s: %s', (specifier, allowed) => {
    expect(isFrameworkSpecifier(specifier)).toBe(allowed);
  });
});

describe('the modules this repository ships', () => {
  const frontend = join(__dirname, '..', '..');
  it.each([
    [
      'the public example',
      join(frontend, '..', '..', 'distributions', 'example', 'frontend', 'site-ui'),
    ],
    ['the demo fixture', join(frontend, 'tests', 'fixtures', 'site-ui-demo')],
  ])('%s passes', (_name, dir) => {
    expect(verifyModuleImports(dir)).toEqual([]);
  });
});
