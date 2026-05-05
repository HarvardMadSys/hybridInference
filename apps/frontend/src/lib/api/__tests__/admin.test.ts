import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { applyRoleQuota, previewRoleQuotaApply } from '../admin';
import { setAccessToken } from '../client';

const fetchMock = vi.fn();

// Build a JWT-shaped token that won't expire for a very long time
function makeFakeToken(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  return `${header}.${payload}.sig`;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  // client.ts reads sessionStorage for the access token; stub it for the test environment
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  // Provide a non-expired token so fetchWithAuth skips token refresh
  setAccessToken(makeFakeToken());
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('role quota client', () => {
  it('previewRoleQuotaApply hits GET preview endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ role: 'pro', quota: 250, keys_affected: 5, users_affected: 4 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const out = await previewRoleQuotaApply('pro');
    expect(out).toEqual({ role: 'pro', quota: 250, keys_affected: 5, users_affected: 4 });
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/quota/role-apply-preview?role=pro');
  });

  it('applyRoleQuota POSTs with role body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ role: 'pro', quota: 250, keys_updated: 5 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const out = await applyRoleQuota('pro');
    expect(out).toEqual({ role: 'pro', quota: 250, keys_updated: 5 });
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ role: 'pro' });
  });
});
