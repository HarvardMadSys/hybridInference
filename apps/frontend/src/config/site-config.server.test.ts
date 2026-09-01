import { afterEach, describe, expect, it, vi } from 'vitest';

import { buildTimeSiteConfig } from './site-config';
import { loadRuntimeSiteConfig } from './site-config.server';

const runtimeDocument = {
  schema_version: 1,
  distribution: { id: 'runtime', display_name: 'Runtime Console', release: '1' },
  site: { public_base_url: 'https://runtime.example.test', support_email: '' },
  features: { routers: ['fixed'], public_signup: true, rag: false },
  branding: null,
};

describe('loadRuntimeSiteConfig', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('fetches the v1 document from the runtime backend without caching', async () => {
    vi.stubEnv('BACKEND_INTERNAL_URL', 'http://runtime-backend:9090/');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => runtimeDocument,
    });
    vi.stubGlobal('fetch', fetchMock);

    const resolved = await loadRuntimeSiteConfig();

    expect(resolved.distribution.id).toBe('runtime');
    expect(resolved.features.rag).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://runtime-backend:9090/site-config',
      expect.objectContaining({
        cache: 'no-store',
        headers: { accept: 'application/json' },
      }),
    );
  });

  it('enables the public agents feature only when both private destinations exist', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => runtimeDocument }),
    );
    vi.stubEnv('AGENT_WEB_INTERNAL_URL', 'http://agent-web:3000');
    vi.stubEnv('AGENT_CONTROL_PLANE_INTERNAL_URL', 'http://agent-api:8000');

    const resolved = await loadRuntimeSiteConfig();

    expect(resolved.features.agents).toBe(true);
    expect(JSON.stringify(resolved)).not.toContain('agent-web');
    expect(JSON.stringify(resolved)).not.toContain('agent-api');
  });

  it('keeps agents disabled when only one private destination exists', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => runtimeDocument }),
    );
    vi.stubEnv('AGENT_WEB_INTERNAL_URL', 'http://agent-web:3000');
    vi.stubEnv('AGENT_CONTROL_PLANE_INTERNAL_URL', '');

    expect((await loadRuntimeSiteConfig()).features.agents).toBe(false);
  });

  it('uses the exact build-time branding when the fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    const resolved = await loadRuntimeSiteConfig();

    expect(resolved.branding).toBe(buildTimeSiteConfig.branding);
    expect(resolved.features.agents).toBe(false);
  });
});
