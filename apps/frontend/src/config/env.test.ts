import { afterEach, describe, expect, it, vi } from 'vitest';

describe('frontend API base configuration', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('preserves an explicit empty API base for same-origin requests', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE', '');

    const { config } = await import('./env');

    expect(config.apiBase).toBe('');
  });

  it('falls back to the local backend when the API base is absent', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE', undefined);

    const { config } = await import('./env');

    expect(config.apiBase).toBe('http://localhost:8080');
  });

  it('preserves an explicitly configured API origin', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE', 'https://gateway.example.com');

    const { config } = await import('./env');

    expect(config.apiBase).toBe('https://gateway.example.com');
  });
});
