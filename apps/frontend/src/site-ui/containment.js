'use strict';

/**
 * Build-time containment for a distribution's Site UI module.
 *
 * The interface promises that a module imports `@site-ui/host`, React, Next.js
 * and its own files — nothing else. `verify-imports.js` reads the module's
 * source and reports the mistakes it can see, early; this file is what makes the
 * promise hold. It sits where the bundler turns each request into a file, and
 * judges the request by the file it *resolves to* rather than by how it is
 * spelled. Spelling has no end — a magic comment, a template literal,
 * `next/../../`, a re-export through a test helper, a CSS `@import`, a symlink —
 * and the resolved file is one path.
 *
 * ## The rule
 *
 * A request made from the module — its issuer is a module file, or, for the
 * resolutions a loader makes on a file's behalf, it starts in a module
 * directory — must resolve, after `realpath`, to one of:
 *
 * - a file of the module itself, outside any `node_modules` below it (a
 *   module's dependencies come from the application's lock, not its own tree);
 * - the facade file `@site-ui/host` is aliased to;
 * - a file inside the application's own `react`, `react-dom` or `next`;
 * - the runtime Next's compiler injects into a module's code without the module
 *   naming it: `@swc/helpers` (private class fields, for instance) and
 *   `styled-jsx` (`<style jsx>`), resolved from Next's own dependencies.
 *
 * A computed import — `require.context`, `import.meta.webpackContext`, or an
 * `import()` of a template literal — must range over a directory of the module.
 * An inline loader (`'!!loader!./x'`) must be one of Next's; loaders are build
 * tooling, which the application owns.
 *
 * ## Where it hooks in
 *
 * The bundler reaches a module's dependencies three ways, so there are three
 * hook points, all installed by `installSiteUiContainment`:
 *
 * - `resolve.plugins`: every resolution webpack performs, including the ones a
 *   loader makes itself — css-loader's `@import` and `url()`, the `src` of
 *   `next/font/local` — and the directory of a computed import;
 * - `resolveLoader.plugins`: the loaders a request names inline. Loaders the
 *   application's rules apply resolve from the project root, not the module;
 * - the finished module graph: the two kinds of dependency webpack records
 *   without resolving them — externals (a Node.js built-in in the server
 *   compilation becomes a runtime `require`) and URI requests (`data:`,
 *   `file:`), which webpack reads itself.
 *
 * ## Where the module directory and the facade come from
 *
 * From the resolution `next.config.js` already holds. `prepareSiteUi` decided
 * which module this build compiles in — `resolution.moduleDir`, the staged copy
 * in an image build — and `webpackAlias` decides which file `@site-ui/host`
 * means. The guard is handed both rather than working either out again, so it
 * cannot disagree with the build about what "the module" is. The neutral UI is
 * part of the application and is not contained.
 *
 * This is boundary maintenance, not a sandbox: a module is trusted code, and
 * code that runs can reach outside itself at runtime. What the guard guarantees
 * is that nothing the bundler compiles into a module comes from outside the
 * promised surface.
 */

// eslint-disable-next-line @typescript-eslint/no-require-imports
const fs = require('node:fs');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const path = require('node:path');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { webpackAlias } = require('./resolve');

const PLUGIN = 'SiteUiContainment';

/** Marks a resolution this plugin started itself, so it is judged once. */
const JUDGED = Symbol('site-ui-containment');

/** The packages a module may resolve into, as the application installed them. */
const FRAMEWORK_PACKAGES = ['react', 'react-dom', 'next'];

/** What Next's compiler adds to a module's own code, resolved from Next. */
const COMPILER_RUNTIME = ['@swc/helpers', 'styled-jsx'];

const ALLOWED =
  'A Site UI module may import only @site-ui/host, react, react-dom, next and its own files.';

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative === '' ||
    (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
  );
}

/** A `realpath` per path per build: resolutions revisit the same directories. */
function cachedRealpath() {
  const cache = new Map();
  return (candidate) => {
    let real = cache.get(candidate);
    if (real === undefined) {
      try {
        real = fs.realpathSync(candidate);
      } catch {
        real = null;
      }
      cache.set(candidate, real);
    }
    return real;
  };
}

/** A resource named by a URI (`data:`, `https:`) rather than a path. */
function isUri(resource) {
  return /^[a-z][a-z0-9+.-]+:/i.test(resource);
}

class SiteUiContainment {
  /**
   * @param {string} frontendDir `apps/frontend`, as `next.config.js` knows it.
   * @param {object} resolution what `prepareSiteUi` returned for this build.
   */
  constructor(frontendDir, resolution) {
    this.realpath = cachedRealpath();
    this.frontendDir = this.realpath(frontendDir) ?? path.resolve(frontendDir);
    this.moduleDir = this.realpath(resolution.moduleDir);
    if (this.moduleDir === null) {
      throw new Error(`[site-ui] the Site UI module directory ${resolution.moduleDir} is missing`);
    }

    // An extensionless alias target: the bundler completes it with its own
    // extension list, so any file it could complete to is the facade.
    const facade = webpackAlias(frontendDir, resolution)['@site-ui/host'];
    this.facade = new Set(
      ['.ts', '.tsx', '.js', '.jsx', '.mjs', '']
        .map((extension) => this.realpath(facade + extension))
        .filter((file) => file !== null && fs.statSync(file).isFile()),
    );

    // Resolved from the application, so a copy a module brought in a
    // `node_modules` of its own is never mistaken for the application's.
    const packageRoot = (name, from) =>
      this.realpath(path.dirname(require.resolve(`${name}/package.json`, { paths: [from] })));
    this.frameworkRoots = FRAMEWORK_PACKAGES.map((name) => packageRoot(name, this.frontendDir));
    this.nextRoot = this.frameworkRoots[FRAMEWORK_PACKAGES.indexOf('next')];
    this.runtimeRoots = COMPILER_RUNTIME.map((name) => packageRoot(name, this.nextRoot));
  }

  /** Whether a real path is the module's own, and not a dependency tree in it. */
  owns(real) {
    if (real === null || !isInside(this.moduleDir, real)) return false;
    return !path.relative(this.moduleDir, real).split(path.sep).includes('node_modules');
  }

  /** Whether a resolution starting in `directory` starts in the module. */
  startsInside(directory) {
    return typeof directory === 'string' && this.owns(this.realpath(directory));
  }

  /** Whether `file` is one of the module's files. */
  isModuleFile(file) {
    return typeof file === 'string' && file !== '' && this.startsInside(path.dirname(file));
  }

  /**
   * Why a resolved path breaks the rule, or `null` when it does not.
   *
   * `kind` is `module` for an import, `context` for the directory a computed
   * import ranges over and `loader` for an inline loader.
   */
  verdict(kind, resolved) {
    if (resolved === false) {
      return 'which resolves to an empty module (a Node.js built-in or a request aliased away)';
    }
    if (typeof resolved !== 'string' || resolved === '') {
      return 'which names no file for the bundler to resolve';
    }
    if (isUri(resolved)) return 'a URI the bundler reads without resolving it';
    const real = this.realpath(resolved) ?? path.resolve(resolved);
    if (kind === 'context') {
      if (this.owns(real)) return null;
      return (
        `which ranges over ${this.display(real)}, a directory outside the module ` +
        "(a computed import may only range over the module's own directories)"
      );
    }
    if (kind === 'loader') {
      if (isInside(this.nextRoot, real)) return null;
      return `which resolves to ${this.display(real)}; a module may only name Next's own loaders`;
    }
    if (this.owns(real) || this.facade.has(real)) return null;
    const roots = [...this.frameworkRoots, ...this.runtimeRoots];
    if (roots.some((root) => isInside(root, real))) return null;
    return `which resolves to ${this.display(real)}, outside the module`;
  }

  /** A path as a reader of the build log wants it. */
  display(real) {
    if (this.owns(real)) {
      const relative = path.relative(this.moduleDir, real);
      return relative === '' ? "the module's root directory" : `the module's ${relative}`;
    }
    return isInside(this.frontendDir, real) ? path.relative(this.frontendDir, real) : real;
  }

  /**
   * Who made a request.
   *
   * A resolution a loader makes for a file carries no issuer, only the
   * directory it starts in; the bundler's own error names the file.
   */
  requester(issuer, directory) {
    if (this.isModuleFile(issuer)) return this.display(this.realpath(issuer) ?? issuer);
    return `a file in ${this.display(this.realpath(directory))}`;
  }

  error(requester, verb, request, why) {
    const error = new Error(`[site-ui] ${requester} ${verb} '${request}', ${why}. ${ALLOWED}`);
    // A loader that trips on this reports it as its own build failure, and the
    // stack would bury the one line that says what to change.
    error.hideStack = true;
    return error;
  }
}

const VERBS = { module: 'imports', context: 'computes an import from', loader: 'names the loader' };

/**
 * Judge every resolution that starts in the module, by the file it ends at.
 *
 * Tapped on the resolver's entry hook ahead of webpack's resolver cache (stage
 * -100), so a cached answer is judged too: the persistent cache survives a
 * change of module, and a result another build accepted proves nothing about
 * this one. The resolution is re-entered with a marker to learn where it ends,
 * the way webpack's cache re-enters it on a miss.
 */
class ContainmentResolverPlugin {
  constructor(containment, { loaders = false } = {}) {
    this.containment = containment;
    this.loaders = loaders;
  }

  apply(resolver) {
    const containment = this.containment;
    const kind = this.loaders ? 'loader' : resolver.options.resolveToContext ? 'context' : 'module';
    const hook = resolver.getHook('resolve');

    hook.tapAsync({ name: PLUGIN, stage: -110 }, (request, resolveContext, callback) => {
      if (request[JUDGED]) return callback();
      // A rule's loaders are resolved with the module file as their issuer but
      // are the application's choice, so a loader counts only when it is named
      // from the module — resolved from a module directory.
      const issuer = kind === 'module' && request.context ? request.context.issuer : undefined;
      if (!containment.isModuleFile(issuer) && !containment.startsInside(request.path)) {
        return callback();
      }

      // A computed import reports each directory through `yield` rather than
      // its callback; hold them back until each has been judged.
      const yielded = typeof resolveContext.yield === 'function' ? [] : null;
      const inner = { ...resolveContext, stack: new Set() };
      if (yielded) inner.yield = (result) => yielded.push(result);

      resolver.doResolve(hook, { ...request, [JUDGED]: true }, null, inner, (error, result) => {
        if (error) return callback(error);
        const results = yielded ?? [];
        if (result) results.push(result);
        for (const { path: resolved } of results) {
          const why = containment.verdict(kind, resolved);
          if (why === null) continue;
          const requester = containment.requester(issuer, request.path);
          return callback(containment.error(requester, VERBS[kind], request.request, why));
        }
        if (!yielded) return callback(null, result ?? null);
        for (const each of results) resolveContext.yield(each);
        return callback(null, null);
      });
    });
  }
}

/** Every dependency of a module, including the ones inside `import()` blocks. */
function* dependenciesOf(block) {
  yield* block.dependencies;
  for (const child of block.blocks) yield* dependenciesOf(child);
}

/** The specifier an external stands for, in whichever shape it was declared. */
function externalSpecifier(request) {
  if (Array.isArray(request)) return request[0];
  if (request && typeof request === 'object') {
    return request.commonjs ?? request.commonjs2 ?? request.module ?? Object.values(request)[0];
  }
  return request;
}

/**
 * Whether an external names React, React DOM or Next.js, and stays inside it.
 *
 * Judged by where the specifier lands, like everything else here, so any
 * subpath of the three packages counts. The narrower list of specifiers a
 * module may *write* is the source check's business.
 */
function isFrameworkExternal(specifier) {
  if (typeof specifier !== 'string') return false;
  const segments = specifier.split('/');
  return (
    FRAMEWORK_PACKAGES.includes(segments[0]) &&
    !segments.some((segment) => segment === '.' || segment === '..' || segment.includes('\\'))
  );
}

/**
 * Judge the dependencies webpack records without resolving.
 *
 * Once the module graph is complete, every dependency of a module file is
 * checked against the module it was bound to. A resolved module was already
 * judged by the resolver and costs one more lookup here; the two kinds that
 * never reached the resolver are why this exists.
 */
class ContainmentGraphPlugin {
  constructor(containment) {
    this.containment = containment;
  }

  apply(compiler) {
    const containment = this.containment;
    const { ExternalModule, NormalModule, WebpackError } = compiler.webpack;

    compiler.hooks.compilation.tap(PLUGIN, (compilation) => {
      compilation.hooks.finishModules.tap(PLUGIN, (modules) => {
        for (const issuer of modules) {
          if (!(issuer instanceof NormalModule)) continue;
          const file = issuer.resource.split('?')[0];
          if (!containment.isModuleFile(file)) continue;

          for (const dependency of dependenciesOf(issuer)) {
            const target = compilation.moduleGraph.getModule(dependency);
            if (!target || target === issuer) continue;
            let why = null;
            if (target instanceof ExternalModule) {
              if (!isFrameworkExternal(externalSpecifier(target.request))) {
                why = 'which the bundler leaves to a runtime require outside the module';
              }
            } else if (target instanceof NormalModule) {
              const resolved = target.resourceResolveData?.path ?? target.resource.split('?')[0];
              why = containment.verdict('module', resolved);
            }
            if (why === null) continue;
            const request = dependency.userRequest ?? dependency.request;
            const { message } = containment.error(
              containment.requester(file),
              'imports',
              request,
              why,
            );
            const error = new WebpackError(message);
            error.module = issuer;
            error.loc = dependency.loc;
            compilation.errors.push(error);
          }
        }
      });
    });
  }
}

/**
 * Install the guard into one of the webpack configurations Next.js builds.
 *
 * `next.config.js` calls this for every compilation — client, server and edge —
 * with the resolution it computed when it loaded. A neutral build is left as it
 * was.
 */
function installSiteUiContainment(config, frontendDir, resolution) {
  if (resolution.kind !== 'distribution') return config;
  const containment = new SiteUiContainment(frontendDir, resolution);
  config.resolve = config.resolve || {};
  config.resolve.plugins = [
    ...(config.resolve.plugins || []),
    new ContainmentResolverPlugin(containment),
  ];
  config.resolveLoader = config.resolveLoader || {};
  config.resolveLoader.plugins = [
    ...(config.resolveLoader.plugins || []),
    new ContainmentResolverPlugin(containment, { loaders: true }),
  ];
  config.plugins = [...(config.plugins || []), new ContainmentGraphPlugin(containment)];
  return config;
}

module.exports = { installSiteUiContainment, isFrameworkExternal };
