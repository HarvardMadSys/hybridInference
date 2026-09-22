// @vitest-environment node
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

/**
 * The import checker, against the cases a plausible implementation gets wrong.
 *
 * Every one of these fails *quietly* when it is wrong, which is why they are
 * pinned here rather than left to review:
 *
 * - a scanner that treats "inside any string" as documentation recognises no
 *   real import at all, and the build stops rejecting anything;
 * - a scanner that treats "inside no string" turns the quickstart's own code
 *   samples into dependencies on packages nobody installed;
 * - an offset computed with `lastIndexOf` lands on a repeated substring — the
 *   `'@site-ui/'` inside `'@site-ui/host'` — which is outside the opening quote
 *   and so looks exactly like a real import;
 * - a doc comment containing a backtick, which is how this very file used to
 *   describe the interface, opens a template span that never closes and hides
 *   every import after it.
 */
const require_ = createRequire(import.meta.url);
const { verifyModuleImports, importSpecifiers } = require_('./verify-imports.js') as {
  verifyModuleImports: (dir: string) => string[];
  importSpecifiers: (source: string) => string[];
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
});

describe('import verification', () => {
  // [source, why, recognised as an import?, allowed by the interface?]
  //
  // Two separate questions on purpose: "recognised" catches a scanner that
  // misses real imports or invents them from a sample, "allowed" catches the
  // rules. Collapsing them would let a scanner that recognises everything and a
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
});
