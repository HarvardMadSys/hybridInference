import { describe, expect, it } from 'vitest';

import type { OpenRouterProviderOption, ProviderRoute, ProviderRouteOption } from '@/lib/api/admin';

import { routeTargetLabel } from './routeTargetLabel';

function route(overrides: Partial<ProviderRoute>): ProviderRoute {
  return {
    model_id: 'model-a',
    strategy: 'routewise',
    route_id: 'model-a:api',
    route_type: 'concurrency',
    provider: 'featherless',
    upstream_provider: 'featherless',
    key_provider: 'featherless',
    base_url: 'https://example.test/v1',
    api_key_id: null,
    api_key: {
      id: null,
      provider: 'featherless',
      label: 'Provider default',
      key_prefix: null,
      source: 'default',
    },
    provider_model_id: 'provider/model-a',
    quota_limit: null,
    concurrency_limit: null,
    endpoint_id: 'model-a:api',
    yaml_weight: 1,
    effective_weight: 1,
    source: 'yaml',
    updated_at: null,
    updated_by: null,
    ...overrides,
  } as ProviderRoute;
}

const providerOptions = [
  { provider: 'featherless', label: 'Featherless' },
  { provider: 'lkeap', label: 'Tencent Token Plan' },
  { provider: 'openrouter', label: 'OpenRouter' },
] as unknown as ProviderRouteOption[];

const openRouterOptions = [
  { provider: 'akashml', label: 'AkashML' },
] as unknown as OpenRouterProviderOption[];

describe('routeTargetLabel', () => {
  it('uses the provider display name', () => {
    expect(routeTargetLabel(route({}), providerOptions, openRouterOptions)).toBe('Featherless');
  });

  it('shows the relay form when provider and upstream differ', () => {
    expect(
      routeTargetLabel(
        route({ provider: 'featherless', upstream_provider: 'lkeap' }),
        providerOptions,
        openRouterOptions,
      ),
    ).toBe('Featherless → Tencent Token Plan');
  });

  it('names the OpenRouter sub-provider, so pinned routes stay distinct', () => {
    const pinned = route({
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      key_provider: 'openrouter',
      openrouter_provider: 'akashml',
    });
    expect(routeTargetLabel(pinned, providerOptions, openRouterOptions)).toBe(
      'OpenRouter · AkashML',
    );
    expect(
      routeTargetLabel(
        route({ ...pinned, openrouter_provider: 'other' }),
        providerOptions,
        openRouterOptions,
      ),
    ).toBe('OpenRouter · other');
  });
});
