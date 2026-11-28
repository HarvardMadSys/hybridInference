import { afterEach, describe, expect, it, vi } from 'vitest';

describe('neutral branding', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('ignores the retired build-time branding variables', async () => {
    // Branding comes only from the gateway's /site-config; an image built with
    // the old NEXT_PUBLIC_* identity values must still be the neutral console.
    vi.stubEnv('NEXT_PUBLIC_APP_NAME', 'Build-time Name');
    vi.stubEnv('NEXT_PUBLIC_STORAGE_KEY_PREFIX', 'build-time');
    vi.stubEnv(
      'NEXT_PUBLIC_TEAM_JSON',
      JSON.stringify([{ name: 'Example Operator', affiliations: ['Example Lab'] }]),
    );

    const { branding } = await import('./branding');

    expect(branding.appName).toBe('HybridInference');
    expect(branding.storageKeyPrefix).toBe('hybridinference');
    expect(branding.team).toEqual([]);
  });
});
