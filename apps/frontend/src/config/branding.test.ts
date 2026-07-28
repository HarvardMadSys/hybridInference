import { afterEach, describe, expect, it, vi } from 'vitest';

describe('branding JSON overrides', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('falls back when team JSON has the wrong shape', async () => {
    vi.stubEnv('NEXT_PUBLIC_TEAM_JSON', '{}');

    const { branding } = await import('./branding');

    // The fallback is the neutral empty list: upstream ships no people.
    expect(Array.isArray(branding.team)).toBe(true);
    expect(branding.team).toHaveLength(0);
  });

  it('accepts a validated team override', async () => {
    vi.stubEnv(
      'NEXT_PUBLIC_TEAM_JSON',
      JSON.stringify([{ name: 'Example Operator', affiliations: ['Example Lab'] }]),
    );

    const { branding } = await import('./branding');

    expect(branding.team).toEqual([{ name: 'Example Operator', affiliations: ['Example Lab'] }]);
  });

  it('falls back when sponsor dimensions are invalid', async () => {
    vi.stubEnv(
      'NEXT_PUBLIC_SPONSORS_JSON',
      JSON.stringify([
        {
          name: 'Broken Sponsor',
          alt: 'Broken logo',
          src: '/broken.svg',
          className: '',
          width: 0,
          height: 10,
        },
      ]),
    );

    const { branding } = await import('./branding');

    expect(branding.sponsors).toHaveLength(0);
  });
});
