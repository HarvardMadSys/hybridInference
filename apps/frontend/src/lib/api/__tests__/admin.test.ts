import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import {
  applyRoleQuota,
  clearRouteWeight,
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listOpenRouterProviderOptions,
  listProviderRoutes,
  listRouteWeights,
  listRoutewiseSettings,
  updateUser,
  listModelVisibility,
  previewRoleQuotaApply,
  setRouteWeight,
  updateProviderRoute,
  updateProviderRouteStrategy,
  updateRoutewiseSetting,
  verifyProviderRoute,
  verifyProviderRouteCandidate,
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

describe('admin user client', () => {
  it('updateUser PATCHes disabled_models in the request body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ user_id: 'user-1', updated_fields: ['disabled_models'], message: 'ok' }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    );

    const out = await updateUser('user-1', {
      disabled_models: ['claude-3-5-sonnet', 'gpt-4o-mini'],
    });

    expect(out.updated_fields).toEqual(['disabled_models']);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/users/user-1');
    expect(init.method).toBe('PATCH');
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(init.body as string)).toEqual({
      disabled_models: ['claude-3-5-sonnet', 'gpt-4o-mini'],
    });
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
              strategy: 'routewise',
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

    expect(out[0]).toMatchObject({
      endpoint_id: 'gpt-4o-mini:local',
      strategy: 'routewise',
      effective_weight: 1,
    });
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
          strategy: 'routewise',
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
          strategy: 'routewise',
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

describe('provider route client', () => {
  it('listProviderRoutes hits model provider routes endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'routewise',
          provider_options: [
            {
              provider: 'openrouter',
              label: 'OpenRouter',
              kind: 'openrouter',
              key_provider: 'openrouter',
              default_base_url: 'https://openrouter.ai/api/v1',
            },
          ],
          openrouter_provider_options: [{ provider: 'parasail', label: 'Parasail' }],
          routes: [
            {
              model_id: 'minimax-fast',
              strategy: 'routewise',
              route_id: 'minimax-fast:featherless-api',
              route_type: 'concurrency',
              provider: 'featherless',
              upstream_provider: 'featherless',
              key_provider: 'featherless',
              base_url: 'https://api.featherless.ai/v1',
              api_key_id: null,
              api_key: {
                id: null,
                provider: 'featherless',
                label: 'Provider default',
                key_prefix: null,
                source: 'default',
              },
              provider_model_id: 'MiniMaxAI/MiniMax-M2.5',
              quota_limit: null,
              endpoint_id: 'minimax-fast:featherless-api',
              yaml_weight: 1,
              effective_weight: 1,
              source: 'yaml',
              updated_at: null,
              updated_by: null,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await listProviderRoutes('minimax-fast');

    expect(out.routes[0].route_id).toBe('minimax-fast:featherless-api');
    expect(out.provider_options[0].provider).toBe('openrouter');
    expect(out.openrouter_provider_options?.[0].provider).toBe('parasail');
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/provider-routes/minimax-fast');
  });

  it('listOpenRouterProviderOptions hits model endpoint discovery', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          provider_model_id: 'minimax/minimax-m2.5',
          providers: [
            { provider: 'inceptron', label: 'Inceptron' },
            { provider: 'chutes', label: 'Chutes' },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await listOpenRouterProviderOptions('minimax/minimax-m2.5');

    expect(out.providers.map((provider) => provider.provider)).toEqual(['inceptron', 'chutes']);
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/openrouter-providers?provider_model_id=minimax%2Fminimax-m2.5',
    );
  });

  it('updateProviderRoute PUTs provider target body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'routewise',
          route_id: 'minimax-fast:featherless-api',
          route_type: 'concurrency',
          provider: 'featherless',
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          openrouter_sort: null,
          key_provider: 'openrouter',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          api_key: {
            id: 'key-1',
            provider: 'openrouter',
            label: 'staging',
            key_prefix: 'sk-or...1234',
            source: 'db',
          },
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
          endpoint_id: 'minimax-fast:openrouter[parasail]-api',
          yaml_weight: 1,
          effective_weight: 1,
          source: 'override',
          updated_at: null,
          updated_by: '127.0.0.1',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await updateProviderRoute('minimax-fast', 'minimax-fast:featherless-api', {
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
    });

    expect(out.provider).toBe('featherless');
    expect(out.upstream_provider).toBe('openrouter');
    expect(out.openrouter_provider).toBe('parasail');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/provider-routes/minimax-fast/minimax-fast%3Afeatherless-api',
    );
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
    });
  });

  it('verifyProviderRoute POSTs provider target body to dry-run endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const out = await verifyProviderRoute('minimax-fast', 'minimax-fast:featherless-api', {
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
    });

    expect(out.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/provider-route-verifications/minimax-fast/minimax-fast%3Afeatherless-api',
    );
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
    });
  });

  it('deleteProviderRoute DELETEs provider route override', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'routewise',
          route_id: 'minimax-fast:featherless-api',
          route_type: 'concurrency',
          provider: 'featherless',
          upstream_provider: 'featherless',
          key_provider: 'featherless',
          base_url: 'https://api.featherless.ai/v1',
          api_key_id: null,
          api_key: {
            id: null,
            provider: 'featherless',
            label: 'Provider default',
            key_prefix: null,
            source: 'default',
          },
          provider_model_id: 'MiniMaxAI/MiniMax-M2.5',
          quota_limit: null,
          endpoint_id: 'minimax-fast:featherless-api',
          yaml_weight: 1,
          effective_weight: 1,
          source: 'yaml',
          updated_at: null,
          updated_by: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await deleteProviderRoute('minimax-fast', 'minimax-fast:featherless-api');

    expect(out.source).toBe('yaml');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/provider-routes/minimax-fast/minimax-fast%3Afeatherless-api',
    );
    expect(init.method).toBe('DELETE');
  });

  it('createProviderRouteCandidate POSTs runtime provider route payload', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'routewise',
          route_id: 'minimax-fast:openrouter[parasail]-api',
          route_type: 'on_demand',
          provider: 'openrouter',
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          openrouter_sort: null,
          key_provider: 'openrouter',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          api_key: {
            id: 'key-1',
            provider: 'openrouter',
            label: 'staging',
            key_prefix: 'sk-or...1234',
            source: 'db',
          },
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
          endpoint_id: 'minimax-fast:openrouter[parasail]-api',
          yaml_weight: 1,
          effective_weight: 1,
          source: 'runtime',
          updated_at: null,
          updated_by: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await createProviderRouteCandidate('minimax-fast', {
      route_type: 'on_demand',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: null,
      weight: 1,
    });

    expect(out.source).toBe('runtime');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/provider-route-candidates/minimax-fast');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      route_type: 'on_demand',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: null,
      weight: 1,
    });
  });

  it('verifyProviderRouteCandidate POSTs runtime route body to dry-run endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const out = await verifyProviderRouteCandidate('minimax-fast', {
      route_type: 'on_demand',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: null,
      weight: 1,
    });

    expect(out.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/provider-route-candidate-verifications/minimax-fast',
    );
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      route_type: 'on_demand',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      openrouter_sort: null,
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: null,
      weight: 1,
    });
  });

  it('deleteProviderRouteCandidate DELETEs runtime provider route', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'routewise',
          provider_options: [],
          routes: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await deleteProviderRouteCandidate(
      'minimax-fast',
      'minimax-fast:openrouter[parasail]-api',
    );

    expect(out.routes).toEqual([]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      '/admin/routing/provider-route-candidates/minimax-fast/minimax-fast%3Aopenrouter%5Bparasail%5D-api',
    );
    expect(init.method).toBe('DELETE');
  });

  it('updateProviderRouteStrategy PATCHes model route strategy', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          model_id: 'minimax-fast',
          strategy: 'fixed',
          provider_options: [],
          routes: [
            {
              model_id: 'minimax-fast',
              strategy: 'fixed',
              route_id: 'minimax-fast:featherless-api',
              route_type: 'concurrency',
              provider: 'featherless',
              upstream_provider: 'featherless',
              key_provider: 'featherless',
              base_url: 'https://api.featherless.ai/v1',
              api_key_id: null,
              api_key: {
                id: null,
                provider: 'featherless',
                label: 'Provider default',
                key_prefix: null,
                source: 'default',
              },
              provider_model_id: 'MiniMaxAI/MiniMax-M2.5',
              quota_limit: null,
              endpoint_id: 'minimax-fast:featherless-api',
              yaml_weight: 1,
              effective_weight: 1,
              source: 'yaml',
              updated_at: null,
              updated_by: null,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await updateProviderRouteStrategy('minimax-fast', 'fixed');

    expect(out.strategy).toBe('fixed');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routing/provider-route-strategies/minimax-fast');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ strategy: 'fixed' });
  });
});

describe('routewise settings client', () => {
  it('listRoutewiseSettings hits routewise settings endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          settings: [
            {
              key: 'routewise_latency_slo_sec',
              value: 2.5,
              value_type: 'float',
              default_value: 3,
              description: 'Latency SLO in seconds for Routewise LP decisions',
              min: 0.1,
              max: null,
            },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await listRoutewiseSettings();

    expect(out.settings[0]).toMatchObject({
      key: 'routewise_latency_slo_sec',
      value: 2.5,
      value_type: 'float',
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routewise/settings');
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('Authorization')).toMatch(/^Bearer /);
  });

  it('updateRoutewiseSetting PATCHes the routewise setting endpoint', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          key: 'routewise_latency_slo_sec',
          value: 2.5,
          value_type: 'float',
          default_value: 3,
          description: 'Latency SLO in seconds for Routewise LP decisions',
          min: 0.1,
          max: null,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const out = await updateRoutewiseSetting('routewise_latency_slo_sec', 2.5);

    expect(out).toMatchObject({
      key: 'routewise_latency_slo_sec',
      value: 2.5,
      value_type: 'float',
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/admin/routewise/settings/routewise_latency_slo_sec');
    expect(init.method).toBe('PATCH');
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json');
    expect((init.headers as Headers).get('Authorization')).toMatch(/^Bearer /);
    expect(JSON.parse(init.body as string)).toEqual({ value: 2.5 });
  });
});
