import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { buildTimeSiteConfig } from './site-config';
import { SITE_CONFIG_ERROR_DIGEST, SiteConfigLoadError } from './site-config-error';
import { loadRuntimeSiteConfig } from './site-config.server';

const runtimeDocument = {
  schema_version: 1,
  distribution: { id: 'runtime', display_name: 'Runtime Console', release: '1' },
  site: { public_base_url: 'https://runtime.example.test', support_email: '' },
  features: { routers: ['fixed'], public_signup: true, rag: false },
  branding: null,
};

describe('loadRuntimeSiteConfig', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([404, 500, 503])(
    'rejects HTTP %i instead of enabling the build-time feature defaults',
    async (status) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status }));

      await expect(loadRuntimeSiteConfig()).rejects.toMatchObject({
        message: `Runtime site configuration request returned HTTP ${status}.`,
        digest: SITE_CONFIG_ERROR_DIGEST,
      });
      expect(console.error).toHaveBeenCalledWith(expect.stringContaining(`HTTP ${status}`));
    },
  );

  it('rejects an invalid document instead of enabling the build-time feature defaults', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));

    await expect(loadRuntimeSiteConfig()).rejects.toThrow(SiteConfigLoadError);
  });

  it('keeps cross-request caching off and gives the runtime request a deadline', async () => {
    vi.stubEnv('BUILT_BACKEND_INTERNAL_URL', 'http://built-backend:9090/');
    vi.stubEnv('BACKEND_INTERNAL_URL', 'http://runtime-must-not-retarget:7070/');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => runtimeDocument,
    });
    vi.stubGlobal('fetch', fetchMock);

    const resolved = await loadRuntimeSiteConfig();

    expect(resolved.distribution.id).toBe('runtime');
    expect(resolved.features.rag).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://built-backend:9090/site-config',
      expect.objectContaining({
        cache: 'no-store',
        headers: { accept: 'application/json' },
      }),
    );
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBeInstanceOf(AbortSignal);
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

  it('reports network failures without logging sensitive connection details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('http://private-backend secret-key')),
    );

    await expect(loadRuntimeSiteConfig()).rejects.toMatchObject({
      message: 'Unable to connect to the runtime site configuration endpoint.',
      digest: SITE_CONFIG_ERROR_DIGEST,
    });
    expect(console.error).toHaveBeenCalledWith(
      'Unable to connect to the runtime site configuration endpoint.',
    );
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('private-backend');
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('secret-key');
  });

  it('reports invalid JSON without including response contents', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => {
          throw new SyntaxError('Unexpected secret-value in response');
        },
      }),
    );

    await expect(loadRuntimeSiteConfig()).rejects.toThrow(
      'Runtime site configuration response is not valid JSON.',
    );
    expect(JSON.stringify(vi.mocked(console.error).mock.calls)).not.toContain('secret-value');
  });

  it('accepts the valid neutral defaults without requiring custom operator settings', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          schema_version: 1,
          distribution: { id: 'neutral', display_name: '', release: '' },
          site: { public_base_url: '', support_email: '' },
          features: { routers: [], public_signup: null, rag: null },
          branding: null,
        }),
      }),
    );

    const resolved = await loadRuntimeSiteConfig();

    expect(resolved.distribution.id).toBe('neutral');
    expect(resolved.branding).toBe(buildTimeSiteConfig.branding);
    expect(resolved.features).toEqual(buildTimeSiteConfig.features);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('loads the next successful request after a failure without caching the error or enabling closed features', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...runtimeDocument,
          features: { routers: ['fixed'], public_signup: false, rag: false },
        }),
      });
    vi.stubGlobal('fetch', fetchMock);

    await expect(loadRuntimeSiteConfig()).rejects.toThrow(SiteConfigLoadError);
    const recovered = await loadRuntimeSiteConfig();

    expect(recovered.distribution.id).toBe('runtime');
    expect(recovered.features).toEqual({ publicSignup: false, rag: false, agents: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('aborts and rejects when the runtime request exceeds its deadline', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      const signal = init?.signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const pending = loadRuntimeSiteConfig();
    const rejection = expect(pending).rejects.toThrow('timed out after 3000 ms');
    await vi.advanceTimersByTimeAsync(3_000);
    await rejection;

    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  });

  it('keeps response body consumption under the same deadline', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => ({
      ok: true,
      json: () =>
        new Promise((_resolve, reject) => {
          const signal = init?.signal;
          signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
        }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    const pending = loadRuntimeSiteConfig();
    const rejection = expect(pending).rejects.toThrow('timed out after 3000 ms');
    await vi.advanceTimersByTimeAsync(3_000);
    await rejection;

    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  });
});
