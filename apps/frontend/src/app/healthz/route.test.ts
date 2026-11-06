import { afterEach, describe, expect, it, vi } from 'vitest';

import { GET } from './route';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('/healthz', () => {
  it('answers 200 without asking the gateway anything', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const response = GET();

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    await expect(response.text()).resolves.toBe('ok\n');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
