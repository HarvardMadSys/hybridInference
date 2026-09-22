#!/usr/bin/env node
/**
 * Build-time Site UI staging and packaging, shared by local and image builds.
 * Resolution, contract checks and import verification use the same resolver as
 * the app, type checker and tests.
 *
 * Commands (paths are resolved from the current directory unless noted):
 *   prepare --app <dir> --context <dir> --subdir <rel> --into <dir> --api <n>
 *   merge --repo-root <dir> --app <relative-dir>
 *   manifest --repo-root <dir> --app <relative-dir>
 *   check --repo-root <dir> --app <relative-dir>
 *
 * `prepare` validates and stages the module. A context containing `.built-in-ui`
 * selects the neutral UI and needs no API argument. `merge` packages public
 * assets, `manifest` adds the module record and static output, and `check`
 * validates the standalone bundle.
 */

import { createRequire } from 'node:module';
import {
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  writeFileSync,
  rmSync,
  statSync,
} from 'node:fs';
import path from 'node:path';
import process from 'node:process';

/*
 * `console` is the interface here. This file is a build step: its output *is*
 * the log a person reads when a module is rejected, and the alternative — an
 * exit code with no message — is the failure mode the resolver's own error
 * messages exist to avoid. The application's `no-console` rule is about code
 * that ships to a browser.
 */
/* eslint-disable no-console */

const require_ = createRequire(import.meta.url);

/** The marker a context carries to mean "use the application's own UI". */
export const BUILT_IN_MARKER = '.built-in-ui';

/** Where a prepared module is staged inside the application. */
export const STAGED_MODULE = path.join('src', 'site-ui', 'external');

/**
 * The file the staging step writes to say what it staged.
 *
 * The container build is one shell command per line, and the module's location
 * has to reach `next build` as environment — but only when there *is* one, and
 * "only when" is a decision the build tooling makes, not something a Dockerfile
 * can infer. Writing the environment to a file the next step sources keeps that
 * decision in one place: the neutral build writes `SITE_UI_DIR=` and is exactly
 * the build it was before this existed.
 */
export const STAGING_ENV = path.join('.site-ui-build-env');

/**
 * What never travels with a module.
 *
 * `node_modules` is the one that matters, and it matters twice over. The
 * interface says a module's dependencies come from the application's lock and
 * that this build does not merge a distribution's `package.json` or run its
 * install scripts — so staging an installed tree would compile against
 * dependencies nothing reviewed. It also breaks the build rather than merely
 * surprising it: a module with its own `@types/react` gives TypeScript two
 * copies of React's types, and a component returning `ReactNode` from one is not
 * assignable to the other. That is a confusing error for a correct component,
 * and it appeared the first time a real distribution's tree was built this way.
 *
 * The build outputs are excluded for the plainer reason: they are not inputs.
 */
const NEVER_STAGED = new Set(['node_modules', '.next', 'dist', 'build']);

export class SiteUiBuildError extends Error {}

function fail(message) {
  throw new SiteUiBuildError(message);
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const options = {};
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    if (!flag.startsWith('--')) fail(`unexpected argument ${JSON.stringify(flag)}`);
    options[flag.slice(2)] = rest[index + 1];
  }
  return { command, options };
}

function required(options, name) {
  const value = options[name];
  if (!value) fail(`--${name} is required`);
  return value;
}

/**
 * Resolve a caller-supplied directory, refusing anything that escapes it.
 *
 * `realpath` first so a symlink cannot be used to leave the context: the
 * resolver reads files relative to the module root, and a link pointing at
 * `/etc` would make "the module's own source" mean something else. This is
 * boundary maintenance, not a security sandbox — a module is trusted code — but
 * a build that silently compiled a file from outside its declared input would
 * make the recorded tree a lie.
 */
function containedDirectory(root, candidate, label) {
  if (!existsSync(root)) fail(`${label} ${root} does not exist`);
  const realRoot = realpathSync(root);
  const realCandidate = existsSync(candidate) ? realpathSync(candidate) : null;
  if (realCandidate === null) fail(`${label} ${candidate} does not exist`);
  const relative = path.relative(realRoot, realCandidate);
  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    fail(`${label} ${candidate} is outside ${root}`);
  }
  if (!statSync(realCandidate).isDirectory()) fail(`${label} ${candidate} is not a directory`);
  return realCandidate;
}

/** Prepare: validate an external module and stage it inside the application. */
function prepare(options) {
  const context = path.resolve(required(options, 'context'));
  const subdir = options.subdir ?? '';
  const into = path.resolve(required(options, 'into'));

  // The application root, which is where the staging environment is written and
  // where `next build` will read it from. Passed rather than derived from
  // `--into`, whose depth is a layout detail this script should not know.
  const app = path.resolve(required(options, 'app'));

  if (existsSync(path.join(context, BUILT_IN_MARKER))) {
    rmSync(path.join(app, STAGING_ENV), { force: true });
    console.log("[site-ui] no external module: compiling the application's own UI");
    return;
  }

  const moduleDir = containedDirectory(
    context,
    path.resolve(context, subdir || '.'),
    'module directory',
  );

  // The resolver is the single decision about what a usable module is: the
  // entries exist, the descriptor parses, the API revision matches, the imports
  // stay inside the interface. Calling it here means the container rejects the
  // same modules a local build rejects, with the same messages.
  const { resolveSiteUi, assertModulePresent, assertSiteUiEntries, assertModuleImports } = require_(
    '../../src/site-ui/resolve.js',
  );
  const api = options.api ?? process.env.SITE_UI_API;
  if (!api) fail('--api or SITE_UI_API is required for an external module');
  try {
    // Resolved for its errors, not its value: this is where a module missing an
    // entry, naming another API revision or importing outside the interface is
    // refused. The copy below is unconditional once it has passed.
    const resolution = resolveSiteUi(into, { SITE_UI_DIR: moduleDir, SITE_UI_API: String(api) });
    assertModulePresent(resolution);
    assertSiteUiEntries(resolution);
    assertModuleImports(resolution);
  } catch (error) {
    fail(`the module at ${moduleDir} is not usable:\n${error.message}`);
  }

  rmSync(into, { recursive: true, force: true });
  mkdirSync(into, { recursive: true });
  // `dereference: false` keeps a symlink a symlink, so a module cannot smuggle
  // a file from outside the context into the staged copy after the containment
  // check above has passed.
  cpSync(moduleDir, into, {
    recursive: true,
    dereference: false,
    filter: (source) => !NEVER_STAGED.has(path.basename(source)),
  });

  // Reported, not silently ignored. A distribution whose module has a
  // `package.json` of its own has dependencies this build will not install, and
  // the resulting error — "cannot find module" from deep inside its own tree —
  // says nothing about why.
  if (existsSync(path.join(moduleDir, 'node_modules'))) {
    console.log(
      '[site-ui] the module ships its own node_modules; it is not staged. A module ' +
        "resolves dependencies from the application's lock (see docs/developer/distribution-customization.md).",
    );
  }

  // Re-resolve against the staged copy: the module's own relative imports now
  // resolve inside the application, and this is the resolution the build will
  // actually use.
  const staged = resolveSiteUi(into, { SITE_UI_DIR: into, SITE_UI_API: String(api) });
  writeFileSync(
    path.join(app, STAGING_ENV),
    `SITE_UI_DIR='${into.replaceAll("'", "'\\''")}'\nSITE_UI_API=${staged.api}\n`,
  );

  console.log(`[site-ui] staged the '${staged.id}' module (API v${staged.api}) at ${into}`);
  console.log(`[site-ui] from ${moduleDir}${subdir ? ` (subdir ${subdir})` : ''}`);
}

/** Every file below `dir`, as paths relative to it. */
function walk(dir, base = dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) found.push(...walk(full, base));
    else found.push(path.relative(base, full));
  }
  return found;
}

/**
 * Where `next build` puts the runnable application.
 *
 * The tooling reads and writes here rather than at the application root, because
 * this is what the image copies. Next emits the server here; `merge` and
 * `manifest` add the public assets and static chunks before `check` runs.
 */
const BUNDLE = path.join('.next', 'standalone');

/**
 * Merge the module's public tree into the built bundle.
 *
 * `--repo-root` is the checkout; `--app` is the application inside it. Both are
 * needed because the two trees being compared live in different places: the
 * application's *own* public files are a source (`apps/frontend/public`), and
 * the bundle's are build output (`apps/frontend/.next/standalone/public`).
 * Collision checks compare module assets against the application's source files.
 */
function merge(options) {
  const root = path.resolve(required(options, 'repo-root'));
  const app = path.resolve(root, required(options, 'app'));
  const bundle = path.join(app, BUNDLE);
  if (!existsSync(bundle)) {
    fail(`${bundle} is missing; the application must build with output: 'standalone'`);
  }
  const moduleDir = path.join(app, STAGED_MODULE);
  const source = path.join(moduleDir, 'public');
  // Two trees, and the difference matters. `owned` is what the application
  // itself ships -- the only tree a module can genuinely collide with. `target`
  // is the bundle, where `next build` has already copied `public/`, so the
  // module's own files are expected to be present there; comparing against it
  // reported every module's own asset as a collision with itself.
  const owned = path.join(app, 'public');
  const target = path.join(bundle, 'public');

  // Next traces files read by server code, but arbitrary public assets are not
  // guaranteed to be traced. Package all host assets before adding the module.
  if (existsSync(owned)) cpSync(owned, target, { recursive: true });

  if (!existsSync(source)) {
    console.log('[site-ui] the module ships no public assets');
    return;
  }

  // The id comes from the manifest the resolver wrote, not from a command-line
  // value: the assets have to land under the id the *build* recorded, or a
  // module's own pages would reference a path its files are not at.
  const manifestPath = path.join(app, 'src', 'generated', 'distribution-ui', 'manifest.json');
  if (!existsSync(manifestPath)) fail(`${manifestPath} is missing; run the build first`);
  const moduleId = JSON.parse(readFileSync(manifestPath, 'utf8')).id;
  if (typeof moduleId !== 'string' || moduleId === '') {
    fail(`${manifestPath} carries no module id`);
  }

  // A module's assets live under `site-assets/<id>/`. The rule is not tidiness:
  // it is what makes "two modules cannot write the same path, and neither can
  // shadow a file the application ships" true, and it is checked here because
  // this is the only place that knows both trees.
  const files = walk(source);
  const expected = path.join('site-assets', moduleId);
  const collisions = [];
  for (const relative of files) {
    if (!relative.startsWith(expected + path.sep) && relative !== expected) {
      fail(
        `the module ships ${relative}, which is outside ${expected}/. A module owns the ` +
          'assets its own pages reference, under its own id, so that two modules cannot ' +
          "write the same path and neither can shadow the application's.",
      );
    }
    if (existsSync(path.join(owned, relative))) collisions.push(relative);
  }
  if (collisions.length > 0) {
    fail(
      `the module's assets collide with the application's: ${collisions.slice(0, 5).join(', ')}${
        collisions.length > 5 ? ` (and ${collisions.length - 5} more)` : ''
      }`,
    );
  }

  cpSync(source, target, { recursive: true });
  console.log(`[site-ui] merged ${files.length} '${moduleId}' asset(s) into public/`);
}

/**
 * Publish the module manifest into the standalone output.
 *
 * `next build` writes `src/generated/distribution-ui/manifest.json` inside the
 * application; the standalone tree it emits does not include `src/`, so the
 * record of *which* module was compiled in would not travel with the artifact.
 * A running container should be able to be asked what it contains rather than
 * trusted to have been labelled correctly, which is what makes this a build step
 * and not a convenience.
 */
function manifest(options) {
  const app = path.resolve(required(options, 'repo-root'), required(options, 'app'));
  const source = path.join(app, 'src', 'generated', 'distribution-ui', 'manifest.json');
  if (!existsSync(source)) fail(`${source} is missing; run the build first`);
  const standalone = path.join(app, BUNDLE);
  if (!existsSync(standalone)) {
    fail(`${standalone} is missing; the application must build with output: 'standalone'`);
  }

  // `output: 'standalone'` omits compiled chunks. Package them here so `check`
  // validates the complete bundle before the runtime image copies it.
  const staticSource = path.join(app, '.next', 'static');
  if (!existsSync(staticSource)) fail(`${staticSource} is missing; the build produced no chunks`);
  cpSync(staticSource, path.join(standalone, '.next', 'static'), { recursive: true });

  const record = JSON.parse(readFileSync(source, 'utf8'));
  cpSync(source, path.join(standalone, 'site-ui-manifest.json'));
  console.log(
    `[site-ui] bundle carries module '${record.id}' (${record.kind}), its chunks and its public files`,
  );
}

/** Check: the bundle is complete and names no build-host path. */
function check(options) {
  const app = path.resolve(required(options, 'repo-root'), required(options, 'app'));
  const bundle = path.join(app, BUNDLE);
  const required_ = ['server.js', path.join('.next', 'static'), 'site-ui-manifest.json', 'public'];
  const missing = required_.filter((relative) => !existsSync(path.join(bundle, relative)));
  if (missing.length > 0) {
    fail(`the bundle is incomplete: ${missing.join(', ')}`);
  }

  // The container is not the build host. A path from the machine that built the
  // image is not a secret, but it describes something a reader cannot use and
  // it can indicate a stale or incorrectly generated package manifest.
  const manifest = JSON.parse(readFileSync(path.join(bundle, 'site-ui-manifest.json'), 'utf8'));
  for (const key of ['id', 'kind']) {
    const value = manifest[key];
    if (typeof value === 'string' && (value.includes('/') || value.includes('\\'))) {
      fail(`the module manifest's ${key} is ${JSON.stringify(value)}, which is a path`);
    }
  }

  console.log(
    `[site-ui] bundle ok: module '${manifest.id}' (${manifest.kind}), API v${manifest.site_ui_api}`,
  );
}

function main(argv) {
  const { command, options } = parseArgs(argv);
  switch (command) {
    case 'prepare':
      prepare(options);
      return 0;
    case 'manifest':
      manifest(options);
      return 0;
    case 'merge':
      merge(options);
      return 0;
    case 'check':
      check(options);
      return 0;
    default:
      console.error(
        'usage: prepare-module.mjs <prepare|merge|manifest|check> [options]\n' +
          '  prepare --app <dir> --context <dir> --subdir <rel> --into <dir> --api <n>\n' +
          '  merge|manifest|check --repo-root <dir> --app <relative-dir>',
      );
      return 2;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  try {
    process.exit(main(process.argv.slice(2)));
  } catch (error) {
    if (error instanceof SiteUiBuildError) {
      console.error(`site-ui: ${error.message}`);
      process.exit(1);
    }
    throw error;
  }
}
