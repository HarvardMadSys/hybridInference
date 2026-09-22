'use strict';

/**
 * Static check that a pinned distribution module only imports what the
 * interface promises.
 *
 * The facade in `src/site-ui/host.ts` is a promise about what will keep
 * working. A promise nobody checks is a comment: a module that reaches into
 * `@/components/providers/AuthProvider` compiles today and breaks the next time
 * that file moves, and the breakage lands on the distribution's release rather
 * than on the change that caused it.
 *
 * Run from `prepareSiteUi`, so a forbidden import fails the build rather than
 * being discovered in review — or not.
 */

// eslint-disable-next-line @typescript-eslint/no-require-imports
const fs = require('node:fs');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const path = require('node:path');

/**
 * Module specifiers a distribution's UI may use.
 *
 * Three narrow rules rather than a list of allowed packages, because the point
 * is that a module may rely on the host, on the framework the host already
 * ships, and on itself — nothing else.
 */
const ALLOWED_SPECIFIER_RULES = [
  {
    name: 'the host facade',
    test: (specifier) => specifier === '@site-ui/host',
    why: 'Import site configuration, translation, auth state and the contract types from @site-ui/host.',
  },
  {
    name: 'the framework and its own dependencies',
    test: (specifier) =>
      specifier === 'react' ||
      specifier.startsWith('react/') ||
      specifier === 'react-dom' ||
      specifier === 'next' ||
      specifier.startsWith('next/'),
    why: 'A module renders React components inside a Next.js application.',
  },
  {
    name: 'its own files',
    // Resolve relative imports before accepting them: ../ paths can otherwise
    // escape the staged module and bypass the host facade.
    test: (specifier, context) => {
      if (!specifier.startsWith('./') && !specifier.startsWith('../')) return false;
      const from = context && context.dir;
      if (!from || !context.root) return true; // No context: the caller allowed it.
      return relativeImportIsInside(context.root, path.resolve(from, specifier));
    },
    why: 'Relative imports stay inside the module.',
  },
];

/** Check the resolved file, including TypeScript extensions and directory entries. */
function isInside(root, candidate) {
  const rel = path.relative(root, candidate);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

function relativeImportIsInside(root, candidate) {
  if (!isInside(root, candidate)) return false;
  const extensions = ['', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.json', '.css'];
  const candidates = extensions.map((extension) => candidate + extension);
  candidates.push(
    ...extensions.slice(1).map((extension) => path.join(candidate, `index${extension}`)),
  );
  for (const file of candidates) {
    try {
      if (fs.statSync(file).isFile()) return isInside(fs.realpathSync(root), fs.realpathSync(file));
    } catch {
      /* A missing candidate may resolve with another extension. */
    }
  }
  // The compiler diagnoses missing files; existing ancestor links still must
  // stay inside the module (including unresolved files below linked folders).
  let parent = candidate;
  while (!fs.existsSync(parent) && parent !== path.dirname(parent)) parent = path.dirname(parent);
  return isInside(fs.realpathSync(root), fs.realpathSync(parent));
}

/** Extensions to walk. A module is TypeScript and CSS in practice. */
const SOURCE_EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs']);

/** Directories never worth walking. */
const SKIP_DIRECTORIES = new Set(['node_modules', '.next', 'dist', 'build', 'coverage']);

/**
 * Paths that are not compiled into the image, and are therefore not bound by
 * the interface's import rules.
 *
 * A module's own tests and its test-runner configuration are built by the
 * distribution's tooling, not by this application's bundler: they legitimately
 * import a test framework, a DOM environment and Node built-ins. Checking them
 * here would reject every module that has tests at all, and the alternative —
 * telling module authors not to write tests — is worse.
 */
const TEST_PATH =
  /(^|\/)(tests?|__tests__|__mocks__)\/|(^|\/)(vitest|jest|playwright)\.config\.[cm]?[jt]s$/;

/**
 * One lexical scan over a source file, classifying it into code, strings and
 * comments.
 *
 * Hand-written rather than a regex, and this is the reason: a doc comment
 * containing a backtick — ``Note the import: `@site-ui/host` `` — would open a
 * template-literal span that never closes, and every real import after it would
 * then look like it was inside a string. The check would report nothing and
 * look like it had passed. Anything that silently passes is worse than anything
 * that fails, so the scan has to understand comments.
 *
 * Returns the code with comments blanked out, plus the ranges the strings
 * occupied. Templates are treated as strings: an interpolated expression inside
 * one is rare enough here, and a `require()` inside it that this misses is
 * caught by the missing-dependency at build time.
 */
function scan(source) {
  const spans = [];
  let code = '';
  let index = 0;

  const blank = (text) => '\u0000'.repeat(text.length);

  while (index < source.length) {
    const character = source[index];
    const next = source[index + 1];

    if (character === '/' && next === '/') {
      const end = source.indexOf('\n', index);
      const stop = end === -1 ? source.length : end;
      code += blank(source.slice(index, stop));
      index = stop;
      continue;
    }

    if (character === '/' && next === '*') {
      const end = source.indexOf('*/', index + 2);
      const stop = end === -1 ? source.length : end + 2;
      code += blank(source.slice(index, stop));
      index = stop;
      continue;
    }

    if (character === "'" || character === '"' || character === '`') {
      const start = index;
      index += 1;
      while (index < source.length) {
        if (source[index] === '\\') {
          index += 2;
          continue;
        }
        if (source[index] === character) {
          index += 1;
          break;
        }
        index += 1;
      }
      spans.push([start, index]);
      code += source.slice(start, index);
      continue;
    }

    code += character;
    index += 1;
  }

  return { code, spans };
}

/**
 * Whether an offset falls inside a string that is *not* the specifier's own.
 *
 * The innermost enclosing span is what decides, and that detail is load-bearing
 * in both directions:
 *
 * - `import { x } from '@/a';` — the specifier sits inside exactly one span,
 *   the quotes `from` is followed by, so it is code;
 * - `const s = 'import x from "@/a";';` — it sits inside two, and the innermost
 *   is the inner double-quoted one, so it is a code sample.
 *
 * "Inside any span" would call the first case documentation and the check would
 * silently pass on every real import; "inside no span" would call the second
 * case a dependency. Neither is a mistake that shows up as a failure, which is
 * why this is spelled out rather than left to the obvious reading.
 */
function isInsideString(spans, offset) {
  let innermost = null;
  for (const span of spans) {
    if (offset <= span[0] || offset >= span[1]) continue;
    if (innermost === null || span[0] > innermost[0]) innermost = span;
  }
  if (innermost === null) return false;

  // The specifier's own quotes open immediately before it, so a span starting
  // one character earlier *is* those quotes.
  return innermost[0] !== offset - 1;
}

/**
 * Every import/export specifier in one source file.
 *
 * Regex, not a parser: this runs in plain Node from a Next config, where no
 * TypeScript parser is guaranteed to be loadable, and the shapes being matched
 * are stable. A specifier written in a form this misses is a specifier the
 * bundler sees and this check does not, so the patterns are deliberately broad
 * and allow newlines between the keywords and the string.
 */
function importSpecifiers(source) {
  const { code, spans } = scan(source);
  const found = [];
  const patterns = [
    // `import ... from 'x'`, `export ... from 'x'`
    /\b(?:import|export)\s[^;'"]*?\bfrom\s*['"]([^'"]+)['"]/g,
    // bare `import 'x'` (a stylesheet, usually)
    /\bimport\s*['"]([^'"]+)['"]/g,
    // `require('x')` and dynamic `import('x')`
    /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
    /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  ];

  for (const pattern of patterns) {
    for (const match of code.matchAll(pattern)) {
      const specifier = match[1];
      // Where the specifier starts, not where the whole match does: the match
      // may begin at the `from`, and for a *nested* sample the text before the
      // specifier is outside the string while the specifier itself is inside.
      //
      // `indexOf`, not `lastIndexOf`: a specifier can repeat inside its own
      // match — `'@site-ui/host'` contains `'@site-ui/'` — and `lastIndexOf`
      // lands on the repetition, which is *outside* the opening quote and so
      // looks exactly like a real import.
      const start = match.index + match[0].indexOf(specifier);
      // A specifier inside a longer string is documentation, not code. The
      // quickstart pages ship lines like `import OpenAI from "openai";` to show
      // a reader what to type, and treating one as a real dependency would
      // force a module to obfuscate its own examples.
      if (isInsideString(spans, start)) continue;
      found.push(specifier);
    }
  }
  return found;
}

function* walk(directory, seen = new Set()) {
  const real = fs.realpathSync(directory);
  if (seen.has(real)) return;
  seen.add(real);
  let entries;
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRECTORIES.has(entry.name)) continue;
      yield* walk(full, seen);
    } else if (entry.isFile() && SOURCE_EXTENSIONS.has(path.extname(entry.name))) {
      yield full;
    }
  }
}

/**
 * Check one module directory.
 *
 * Returns a list of problems rather than throwing, so a caller can report all
 * of them at once — fixing forbidden imports one build at a time is a bad way
 * to spend an afternoon.
 */
function verifyModuleImports(moduleDir) {
  const problems = [];
  const root = fs.realpathSync(moduleDir);

  function checkLinks(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (SKIP_DIRECTORIES.has(entry.name)) continue;
      const full = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        try {
          if (!isInside(root, fs.realpathSync(full)))
            problems.push(`${path.relative(root, full)}: symlink resolves outside this module`);
        } catch {
          problems.push(`${path.relative(root, full)}: broken symlink`);
        }
      } else if (entry.isDirectory()) checkLinks(full);
    }
  }
  checkLinks(root);

  for (const file of walk(root)) {
    const relative = path.relative(root, file).split(path.sep).join('/');
    if (TEST_PATH.test(relative)) continue;

    const source = fs.readFileSync(file, 'utf8');
    for (const specifier of importSpecifiers(source)) {
      const context = { root, dir: path.dirname(file) };
      if (ALLOWED_SPECIFIER_RULES.some((rule) => rule.test(specifier, context))) continue;

      // `@/...` gets its own message: it is the mistake people actually make,
      // and "use @site-ui/host instead" is the fix.
      const escapes = specifier.startsWith('./') || specifier.startsWith('../');
      const hint = specifier.startsWith('@/')
        ? ' `@/...` paths are internals of the shared application and may move at any time; ' +
          're-export what you need through @site-ui/host.'
        : escapes
          ? ' The path resolves outside this module. A relative import has to stay inside ' +
            'the module it is written in; anything from the shared application comes ' +
            'through @site-ui/host.'
          : ` Allowed: ${ALLOWED_SPECIFIER_RULES.map((rule) => rule.name).join(', ')}.`;

      problems.push(`${relative}: imports '${specifier}'.${hint}`);
    }
  }

  return problems;
}

module.exports = { verifyModuleImports, importSpecifiers, ALLOWED_SPECIFIER_RULES, TEST_PATH };
