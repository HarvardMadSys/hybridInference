import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import {
  applyRoleQuota,
  clearRouteWeight,
  listRouteWeights,
  listModelVisibility,
  previewRoleQuotaApply,
  setRouteWeight,
  updateModelVisibility,
} from '../admin';
import { setAccessToken } from '../client';

const fetchMock = vi.fn();

// Build a JWT-shaped token that won't expire during this test run (1h is more
// than enough — vitest aborts long before that).
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

describe('model visibility client', () => {
  it('listModelVisibility hits model visibility endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          models: [
            {
              model_id: 'gpt-4o-mini',
              baseline_required_role: 'free',
              override_required_role: null,
              effective_required_role: 'free',
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await listModelVisibility();

    expect(out.models[0]).toMatchObject({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: null,
      effective_required_role: 'free',
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/models/visibility');
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('Authorization')).toMatch(/^Bearer /);
  });

  it('updateModelVisibility PATCHes the model visibility endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: 'internal',
          effective_required_role: 'internal',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await updateModelVisibility('gpt-4o-mini', 'internal');

    expect(out.override_required_role).toBe('internal');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/models/gpt-4o-mini/visibility');
    expect(init.method).toBe('PATCH');
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json');
    expect((init.headers as Headers).get('Authorization')).toMatch(/^Bearer /);
    expect(JSON.parse(init.body as string)).toEqual({ required_role: 'internal' });
  });

  it('updateModelVisibility sends null required_role when clearing visibility role', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await updateModelVisibility('gpt-4o-mini', null);

    expect(out.override_required_role).toBeNull();
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ required_role: null });
  });
});

describe('route weight client', () => {
  it('listRouteWeights hits model route weight endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'gpt-4o-mini',
          routes: [
            {
              model_id: 'gpt-4o-mini',
              endpoint_id: 'gpt-4o-mini:local',
              provider: 'local',
              base_url: 'http://localhost:8000',
              yaml_weight: 1,
              override_weight: null,
              effective_weight: 1,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await listRouteWeights('gpt-4o-mini');

    expect(out[0]).toMatchObject({ endpoint_id: 'gpt-4o-mini:local', effective_weight: 1 });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/weights/gpt-4o-mini');
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('Authorization')).toMatch(/^Bearer /);
  });

  it('setRouteWeight PUTs weight body to endpoint route', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'gpt-4o-mini',
          endpoint_id: 'gpt-4o-mini:remote',
          provider: 'remote',
          base_url: 'https://api.example.test',
          yaml_weight: 2,
          override_weight: 4,
          effective_weight: 4,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await setRouteWeight('gpt-4o-mini', 'gpt-4o-mini:remote', 4);

    expect(out.override_weight).toBe(4);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/weights/gpt-4o-mini/gpt-4o-mini%3Aremote');
    expect(init.method).toBe('PUT');
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(init.body as string)).toEqual({ weight: 4 });
  });

  it('clearRouteWeight DELETEs endpoint route override', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'gpt-4o-mini',
          endpoint_id: 'gpt-4o-mini:remote',
          provider: 'remote',
          base_url: 'https://api.example.test',
          yaml_weight: 2,
          override_weight: null,
          effective_weight: 2,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await clearRouteWeight('gpt-4o-mini', 'gpt-4o-mini:remote');

    expect(out.override_weight).toBeNull();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/weights/gpt-4o-mini/gpt-4o-mini%3Aremote');
    expect(init.method).toBe('DELETE');
  });
});
