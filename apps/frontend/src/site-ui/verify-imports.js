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
 * Run from `prepareSiteUi`, so a forbidden import fails the build before
 * anything is compiled, with every problem listed at once rather than one per
 * build. This is the early half of the check. The bundler guard in
 * `containment.js` holds the same rule at the point where each request becomes
 * a file, and it is what makes the rule hold: a reader of source cannot see a
 * request the bundler assembles for itself. So this file may miss things the
 * guard catches, but it must never reject a module the guard — and the
 * interface — would accept.
 */

// eslint-disable-next-line @typescript-eslint/no-require-imports
const fs = require('node:fs');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const path = require('node:path');

/**
 * The framework specifiers the interface names: `react`, `react/*`,
 * `react-dom`, `next` and `next/*`.
 *
 * A subpath must stay a subpath. `next/../../src/site-ui/routes` starts with
 * `next/` and resolves into the application, so a `.` or `..` segment — or an
 * empty one, or a Windows separator — is not a subpath of anything.
 */
function isFrameworkSpecifier(specifier) {
  if (specifier === 'react-dom') return true;
  const [pathPart] = specifier.split(/[?#]/);
  const segments = pathPart.split('/');
  if (segments[0] !== 'react' && segments[0] !== 'next') return false;
  return !segments.some(
    (segment, index) =>
      (index > 0 && (segment === '' || segment === '.' || segment === '..')) ||
      segment.includes('\\'),
  );
}

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
    test: (specifier) => isFrameworkSpecifier(specifier),
    why: 'A module renders React components inside a Next.js application.',
  },
  {
    name: 'its own files',
    // Resolve relative imports before accepting them: ../ paths can otherwise
    // escape the staged module and bypass the host facade.
    test: (specifier, context) => {
      if (!isRelative(specifier)) return false;
      const from = context && context.dir;
      if (!from || !context.root) return true; // No context: the caller allowed it.
      return relativeImportIsInside(context.root, path.resolve(from, stripQuery(specifier)));
    },
    why: 'Relative imports stay inside the module.',
  },
];

function isRelative(specifier) {
  return specifier === '.' || specifier.startsWith('./') || specifier.startsWith('../');
}

/** A request's path, without the query or fragment a loader would receive. */
function stripQuery(specifier) {
  return specifier.split(/[?#]/)[0];
}

/** Check the resolved file, including TypeScript extensions and directory entries. */
function isInside(root, candidate) {
  const rel = path.relative(root, candidate);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}

/**
 * Inside the module, and not inside a dependency tree the module carries.
 *
 * A module's dependencies come from the application's lock; a `node_modules`
 * below it is never staged, and importing from one is importing a package.
 */
function isOwn(root, candidate) {
  return (
    isInside(root, candidate) &&
    !path.relative(root, candidate).split(path.sep).includes('node_modules')
  );
}

/** Extensions a request may omit, in any order the bundler or `tsc` tries them. */
const RESOLVED_EXTENSIONS = ['.tsx', '.ts', '.jsx', '.js', '.mjs', '.cjs', '.json', '.css'];

/**
 * Every existing file a relative request could resolve to.
 *
 * All of them, not the first: the bundler and this check try extensions in
 * different orders, and `./x` naming both an inside `x.ts` and an outside
 * `x.js` symlink must be judged by the one that escapes.
 */
function candidateFiles(candidate) {
  return [
    candidate,
    ...RESOLVED_EXTENSIONS.map((extension) => candidate + extension),
    ...RESOLVED_EXTENSIONS.map((extension) => path.join(candidate, `index${extension}`)),
  ].filter(isFile);
}

function isFile(candidate) {
  try {
    return fs.statSync(candidate).isFile();
  } catch {
    return false; // A missing candidate may resolve with another extension.
  }
}

function relativeImportIsInside(root, candidate) {
  const realRoot = fs.realpathSync(root);
  if (!isOwn(root, candidate)) return false;
  const files = candidateFiles(candidate);
  if (files.length > 0) return files.every((file) => isOwn(realRoot, fs.realpathSync(file)));
  // The compiler diagnoses missing files; existing ancestor links still must
  // stay inside the module (including unresolved files below linked folders).
  let parent = candidate;
  while (!fs.existsSync(parent) && parent !== path.dirname(parent)) parent = path.dirname(parent);
  return isOwn(realRoot, fs.realpathSync(parent));
}

/** Extensions to read. A module is TypeScript and CSS in practice. */
const SOURCE_EXTENSIONS = new Set(['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs']);
const STYLE_EXTENSIONS = new Set(['.css']);

/** Directories never worth reading for source: dependencies and build output. */
const SKIP_DIRECTORIES = new Set(['node_modules', '.next', 'dist', 'build', 'coverage']);

/**
 * Names the staging step never copies into the application, at any depth.
 *
 * `scripts/site-ui/prepare-module.mjs` imports this set rather than keeping
 * its own, because the two have to agree: a symlink the staging step *does*
 * copy is a file the image can serve, so it is checked wherever it sits.
 * `coverage` is deliberately absent — staging copies it, so a
 * `coverage -> /elsewhere` link would ship.
 */
const NEVER_STAGED = new Set(['node_modules', '.next', 'dist', 'build']);

/**
 * Paths that are not compiled into the image unless production code imports
 * them, and are therefore not bound by the interface's import rules.
 *
 * A module's own tests and its test-runner configuration are built by the
 * distribution's tooling, not by this application's bundler: they legitimately
 * import a test framework, a DOM environment and Node built-ins. Checking them
 * here would reject every module that has tests at all, and the alternative —
 * telling module authors not to write tests — is worse.
 *
 * The exemption is for files the entries do not reach. A test helper that
 * `client.tsx` imports is compiled like any other file, and is checked like one.
 */
const TEST_PATH =
  /(^|\/)(tests?|__tests__|__mocks__)\/|(^|\/)(vitest|jest|playwright)\.config\.[cm]?[jt]s$/;

/**
 * The files the bundler may start from: every extension either the resolver or
 * webpack could pick for an entry, since the two try them in different orders.
 */
const ENTRY_FILES = [
  ...['client', 'server'].flatMap((name) =>
    ['.tsx', '.ts', '.jsx', '.js', '.mjs'].map((extension) => name + extension),
  ),
  'styles.css',
  'styles.css.css',
];

function scriptKind(ts, fileName) {
  switch (path.extname(fileName)) {
    case '.ts':
    case '.mts':
    case '.cts':
      return ts.ScriptKind.TS;
    case '.tsx':
      return ts.ScriptKind.TSX;
    default:
      // Next compiles JSX in `.js` files as well, so JavaScript is read as JSX.
      return ts.ScriptKind.JSX;
  }
}

/**
 * The static text a request expression starts with, and whether it continues.
 *
 * `'./x'` is a whole request. `` `./locales/${lang}` `` and `'./locales/' + lang`
 * are the bundler's computed imports: it compiles every file under the static
 * prefix's directory, so that directory is what gets checked.
 */
function requestText(ts, node) {
  if (!node) return null;
  node = unwrap(ts, node);
  if (ts.isStringLiteralLike(node)) return { text: node.text, computed: false };
  if (ts.isTemplateExpression(node)) return { text: node.head.text, computed: true };
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = requestText(ts, node.left);
    return left && { text: left.text, computed: true };
  }
  return null;
}

/** The directory a computed request ranges over: its prefix up to the last `/`. */
function computedDirectory(prefix) {
  const slash = prefix.lastIndexOf('/');
  if (slash === -1) return prefix.startsWith('.') ? '.' : null;
  return prefix.slice(0, slash + 1);
}

/**
 * The expression under any TypeScript-only wrapping.
 *
 * The compiler strips `as`, `satisfies`, `!` and parentheses before the bundler
 * reads the file, so `(import.meta as Meta).webpackContext(…)` is, to the
 * bundler, `import.meta.webpackContext(…)`.
 */
function unwrap(ts, node) {
  while (
    ts.isParenthesizedExpression(node) ||
    ts.isAsExpression(node) ||
    ts.isSatisfiesExpression(node) ||
    ts.isNonNullExpression(node) ||
    ts.isTypeAssertionExpression(node)
  ) {
    node = node.expression;
  }
  return node;
}

function isImportMeta(ts, node) {
  node = unwrap(ts, node);
  return ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword;
}

function isRequire(ts, node) {
  node = unwrap(ts, node);
  return ts.isIdentifier(node) && node.text === 'require';
}

/** `require.context`, `import.meta.webpackContext`: a directory, not a file. */
function contextCall(ts, callee) {
  callee = unwrap(ts, callee);
  if (!ts.isPropertyAccessExpression(callee)) return false;
  const name = callee.name.text;
  if (name === 'context') return isRequire(ts, callee.expression);
  return name === 'webpackContext' && isImportMeta(ts, callee.expression);
}

/** `import()`, `require()`, `require.resolve()`, `require.resolveWeak()`. */
function fileCall(ts, callee) {
  callee = unwrap(ts, callee);
  if (callee.kind === ts.SyntaxKind.ImportKeyword) return true;
  if (isRequire(ts, callee)) return true;
  return (
    ts.isPropertyAccessExpression(callee) &&
    isRequire(ts, callee.expression) &&
    (callee.name.text === 'resolve' || callee.name.text === 'resolveWeak')
  );
}

/** `new URL(request, import.meta.url)`: an asset (or worker) the bundler emits. */
function isUrlOfThisFile(ts, node) {
  const callee = unwrap(ts, node.expression);
  if (!ts.isIdentifier(callee) || callee.text !== 'URL') return false;
  const base = node.arguments && node.arguments[1] && unwrap(ts, node.arguments[1]);
  return (
    base !== undefined &&
    ts.isPropertyAccessExpression(base) &&
    base.name.text === 'url' &&
    isImportMeta(ts, base.expression)
  );
}

/**
 * Every request one script makes, found by parsing it.
 *
 * A parser rather than patterns, because each pattern this file used to hold
 * had a way past it — a comment between `import(` and the string, a template
 * literal, a string-named import, an apostrophe in JSX text opening a "string"
 * that ran over the next import, `require.context`, `new URL`. TypeScript is
 * already a locked build dependency (`resolve.js` reads the descriptor with it),
 * it reads JSX and type syntax, and it never throws: a file it cannot make full
 * sense of still yields the requests it can see, and the bundler judges the
 * rest.
 *
 * Returns `{ specifier, directory, url }`: `directory` for a computed import or
 * a context, `url` for `new URL(…, import.meta.url)`, which webpack resolves
 * relative to the file even without a `./`.
 */
function scriptRequests(source, fileName = 'module.tsx') {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const ts = require('typescript');
  const file = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    false,
    scriptKind(ts, fileName),
  );
  const found = [];
  const push = (specifier, flags = {}) => {
    if (specifier) found.push({ specifier, directory: false, url: false, ...flags });
  };
  const pushRequest = (node, flags) => {
    const request = requestText(ts, node);
    if (!request) return;
    if (!request.computed) return push(request.text, flags);
    push(computedDirectory(request.text), { ...flags, directory: true });
  };

  const visit = (node) => {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      ts.isStringLiteral(node.moduleSpecifier)
    ) {
      push(node.moduleSpecifier.text);
    } else if (
      ts.isImportEqualsDeclaration(node) &&
      ts.isExternalModuleReference(node.moduleReference)
    ) {
      pushRequest(node.moduleReference.expression);
    } else if (
      ts.isImportTypeNode(node) &&
      ts.isLiteralTypeNode(node.argument) &&
      ts.isStringLiteral(node.argument.literal)
    ) {
      push(node.argument.literal.text);
    } else if (ts.isCallExpression(node)) {
      if (contextCall(ts, node.expression)) {
        const request = requestText(ts, node.arguments[0]);
        if (request) push(request.text, { directory: true });
      } else if (fileCall(ts, node.expression)) {
        pushRequest(node.arguments[0]);
      }
    } else if (ts.isNewExpression(node) && isUrlOfThisFile(ts, node)) {
      pushRequest(node.arguments[0], { url: true });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

/**
 * Every request one stylesheet makes: `@import`, `url()`, and the CSS Modules
 * `composes … from` and `@value … from`.
 *
 * A URL the build leaves for the browser — root-relative, `data:`, `https:`,
 * a bare `#fragment` — is not a request for the bundler and is skipped, the
 * same way Next's css-loader skips it.
 */
function styleRequests(source) {
  const code = source.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, ' '));
  const quoted = String.raw`(?:"([^"]*)"|'([^']*)')`;
  const bare = String.raw`([^)'"\s][^)\s]*)`;
  const patterns = [
    new RegExp(String.raw`@import\s+(?:url\(\s*(?:${quoted}|${bare})\s*\)|${quoted})`, 'gi'),
    new RegExp(String.raw`\burl\(\s*(?:${quoted}|${bare})\s*\)`, 'gi'),
    new RegExp(String.raw`\bcomposes\s*:[^;{}]*?\bfrom\s+${quoted}`, 'gi'),
    new RegExp(String.raw`@value\s[^;{}]*?\bfrom\s+${quoted}`, 'gi'),
  ];
  const found = new Set();
  for (const pattern of patterns) {
    for (const match of code.matchAll(pattern)) {
      const target = match.slice(1).find((group) => group !== undefined);
      if (!target || /^(?:[a-z][a-z0-9+.-]*:|\/|#)/i.test(target)) continue;
      found.add(target);
    }
  }
  return [...found].map((specifier) => ({ specifier, directory: false, url: false, style: true }));
}

/**
 * Every import/export specifier in one source file.
 *
 * Kept for callers that want the list rather than a verdict; `fileName` picks
 * the dialect.
 */
function importSpecifiers(source, fileName = 'module.tsx') {
  const requests = STYLE_EXTENSIONS.has(path.extname(fileName))
    ? styleRequests(source)
    : scriptRequests(source, fileName);
  return requests.map((request) => request.specifier);
}

/**
 * How a request should be read.
 *
 * A stylesheet request is relative unless it cannot be: css-loader tries
 * `./x` before `x`, and a leading `~` is its old spelling for a package. A
 * `new URL` request is relative to the file with or without `./`.
 */
function normalize(request, dir) {
  const { specifier } = request;
  if (request.style) {
    if (specifier.startsWith('~')) return specifier.slice(1);
    if (isRelative(specifier)) return specifier;
    const relative = `./${specifier}`;
    return candidateFiles(path.resolve(dir, stripQuery(relative))).length > 0
      ? relative
      : specifier;
  }
  if (request.url && !isRelative(specifier) && !/^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(specifier)) {
    return `./${specifier}`;
  }
  return specifier;
}

/** Every file below a directory, skipping dependency trees and build output. */
function* walk(directory, extensions, seen = new Set()) {
  let real;
  try {
    real = fs.realpathSync(directory);
  } catch {
    return;
  }
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
      yield* walk(full, extensions, seen);
    } else if (entry.isFile() && extensions.has(path.extname(entry.name))) {
      yield full;
    }
  }
}

const READABLE_EXTENSIONS = new Set([...SOURCE_EXTENSIONS, ...STYLE_EXTENSIONS]);

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
      if (NEVER_STAGED.has(entry.name)) continue;
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

  const relativeName = (file) => path.relative(root, file).split(path.sep).join('/');
  const requestsOf = new Map();
  const requestsIn = (file) => {
    if (!requestsOf.has(file)) {
      let source;
      try {
        source = fs.readFileSync(file, 'utf8');
      } catch {
        source = '';
      }
      requestsOf.set(
        file,
        STYLE_EXTENSIONS.has(path.extname(file))
          ? styleRequests(source)
          : scriptRequests(source, file),
      );
    }
    return requestsOf.get(file);
  };

  // What the entries reach, followed through the module's own files: a file
  // production code imports is compiled into the image whatever its path says.
  const reached = new Set();
  const pending = ENTRY_FILES.map((name) => path.join(root, name)).filter(isFile);
  const reach = (file) => {
    const real = fs.realpathSync(file);
    if (!isOwn(root, real) || !READABLE_EXTENSIONS.has(path.extname(real))) return;
    if (!reached.has(real)) pending.push(real);
  };
  while (pending.length > 0) {
    const file = pending.pop();
    if (reached.has(file)) continue;
    reached.add(file);
    for (const request of requestsIn(file)) {
      const specifier = normalize(request, path.dirname(file));
      if (!isRelative(specifier)) continue;
      const target = path.resolve(path.dirname(file), stripQuery(specifier));
      if (request.directory) {
        for (const each of walk(target, READABLE_EXTENSIONS)) reach(each);
      } else {
        for (const each of candidateFiles(target)) reach(each);
      }
    }
  }

  const checked = new Set(reached);
  for (const file of walk(root, READABLE_EXTENSIONS)) {
    const real = fs.realpathSync(file);
    if (!TEST_PATH.test(relativeName(real))) checked.add(real);
  }

  for (const file of [...checked].sort()) {
    const relative = relativeName(file);
    const context = { root, dir: path.dirname(file) };
    for (const request of requestsIn(file)) {
      const specifier = normalize(request, context.dir);
      if (ALLOWED_SPECIFIER_RULES.some((rule) => rule.test(specifier, context))) continue;

      // `@/...` gets its own message: it is the mistake people actually make,
      // and "use @site-ui/host instead" is the fix.
      const hint = specifier.startsWith('@/')
        ? ' `@/...` paths are internals of the shared application and may move at any time; ' +
          're-export what you need through @site-ui/host.'
        : isRelative(specifier)
          ? ' The path resolves outside this module. A relative import has to stay inside ' +
            'the module it is written in; anything from the shared application comes ' +
            'through @site-ui/host.'
          : specifier.includes('!')
            ? ' Inline loaders are not part of the interface; the application decides how ' +
              'a file is compiled.'
            : ` Allowed: ${ALLOWED_SPECIFIER_RULES.map((rule) => rule.name).join(', ')}.`;
      const verb = request.directory ? 'computes an import from' : 'imports';
      const problem = `${relative}: ${verb} '${request.specifier}'.${hint}`;
      if (!problems.includes(problem)) problems.push(problem);
    }
  }

  return problems;
}

module.exports = {
  verifyModuleImports,
  importSpecifiers,
  isFrameworkSpecifier,
  ALLOWED_SPECIFIER_RULES,
  NEVER_STAGED,
  TEST_PATH,
};
