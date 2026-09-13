// `/agents` reaches the standalone cloud agent — and only when one is deployed.
//
// Executes the real config rather than reading it, because the property that
// matters survives refactoring the file: what ends up in `beforeFiles` after
// the variables are resolved. A text scan broke the first time these rules
// moved into a local variable, which is exactly the kind of edit that must not
// count as a regression.
//
// Two failures this catches, both silent:
//
// **`afterFiles` loses.** A rewrite returned in a flat array is `afterFiles`,
// which Next checks *after* filesystem routes — including the new runtime
// `/agents` handler. Legacy rewrites therefore have to remain beforeFiles.
//
// **Opt-in.** Every deployment running no standalone agent must be untouched.
// Unconditional, these legacy rules would bypass the runtime handler.

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const WEB = 'http://agent-web:3000';
const API = 'http://agent-control-plane:8000';

async function loadRewrites(env: Record<string, string | undefined>) {
  return loadConfig(env).rewrites();
}

function loadConfig(env: Record<string, string | undefined>) {
  for (const [k, v] of Object.entries(env)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  // CommonJS, and cached by path — drop it so each case re-reads the env.
  const path = require.resolve('./next.config.js');
  delete require.cache[path];
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- deliberate: see above.
  return require('./next.config.js');
}

describe('the /agents rewrites', () => {
  const saved = { ...process.env };
  beforeEach(() => {
    process.env = { ...saved };
  });
  afterEach(() => {
    process.env = { ...saved };
  });

  it('is unset by default, so the runtime handler decides whether the service exists', async () => {
    const { beforeFiles } = await loadRewrites({
      AGENT_WEB_INTERNAL_URL: undefined,
      AGENT_CONTROL_PLANE_INTERNAL_URL: undefined,
    });
    expect(beforeFiles).toEqual([]);
  });

  it('needs both URLs — one alone would route half the app into a 502', async () => {
    const { beforeFiles } = await loadRewrites({
      AGENT_WEB_INTERNAL_URL: WEB,
      AGENT_CONTROL_PLANE_INTERNAL_URL: undefined,
    });
    expect(beforeFiles).toEqual([]);
  });

  it('puts the agent rules in beforeFiles, where they beat the local pages', async () => {
    const { beforeFiles, afterFiles } = await loadRewrites({
      AGENT_WEB_INTERNAL_URL: WEB,
      AGENT_CONTROL_PLANE_INTERNAL_URL: API,
    });

    expect(beforeFiles.map((r: { source: string }) => r.source)).toEqual([
      '/agents/api/:path*',
      '/agents/:path*',
      '/agents',
    ]);
    // The gateway's own API rewrites must survive untouched.
    expect(afterFiles.length).toBeGreaterThan(10);
  });

  it('matches the API prefix before the web prefix', async () => {
    const { beforeFiles } = await loadRewrites({
      AGENT_WEB_INTERNAL_URL: WEB,
      AGENT_CONTROL_PLANE_INTERNAL_URL: API,
    });
    const sources = beforeFiles.map((r: { source: string }) => r.source);
    // `/agents/api/*` is a prefix of `/agents/*`. Reversed, every control-plane
    // call is served the web app's HTML — a 200 that is not JSON, which reads
    // as a client bug rather than a routing one.
    expect(sources.indexOf('/agents/api/:path*')).toBeLessThan(sources.indexOf('/agents/:path*'));
  });

  it('pins server-side backend fetches to the rewrite target selected at build time', async () => {
    const backend = 'http://custom-backend:9090';
    const config = loadConfig({ BACKEND_INTERNAL_URL: backend });
    const { afterFiles } = await config.rewrites();

    expect(config.env.BUILT_BACKEND_INTERNAL_URL).toBe(backend);
    expect(
      afterFiles.every(({ destination }: { destination: string }) =>
        destination.startsWith(backend),
      ),
    ).toBe(true);
  });

  it('strips the prefix for the API and keeps it for the web app', async () => {
    const { beforeFiles } = await loadRewrites({
      AGENT_WEB_INTERNAL_URL: WEB,
      AGENT_CONTROL_PLANE_INTERNAL_URL: API,
    });
    const bySource = Object.fromEntries(
      beforeFiles.map((r: { source: string; destination: string }) => [r.source, r.destination]),
    );

    // The control plane serves `/v1/agent/...` at its root and knows nothing
    // about `/agents`.
    expect(bySource['/agents/api/:path*']).toBe(`${API}/:path*`);
    // That app is built with `basePath=/agents` and generates its own links
    // already carrying it. Strip here and every asset 404s while the HTML
    // still loads.
    expect(bySource['/agents/:path*']).toBe(`${WEB}/agents/:path*`);
  });
});
