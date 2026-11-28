import type { OpenRouterProviderOption, ProviderRoute, ProviderRouteOption } from '@/lib/api/admin';

/** The OpenRouter provider value for automatic routing. */
const OPENROUTER_PROVIDER_AUTO = '';

export function routePrimaryProvider(route: ProviderRoute) {
  if (route.provider === route.upstream_provider && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.provider;
}

export function routePrimaryUpstreamProvider(route: ProviderRoute) {
  if (route.upstream_provider !== 'openrouter' && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.upstream_provider;
}

export function routeOpenRouterProvider(route: ProviderRoute) {
  if (route.openrouter_provider) return route.openrouter_provider;
  if (route.upstream_provider !== 'openrouter' && route.key_provider === 'openrouter') {
    return route.upstream_provider;
  }
  return OPENROUTER_PROVIDER_AUTO;
}

export function providerLabel(providerOptions: ProviderRouteOption[], provider: string) {
  return providerOptions.find((option) => option.provider === provider)?.label ?? provider;
}

export function openRouterProviderLabel(options: OpenRouterProviderOption[], provider: string) {
  return options.find((option) => option.provider === provider)?.label ?? provider;
}

/**
 * A route's target as a reader knows it: the provider's display name, the
 * `provider → upstream` relay form, or the OpenRouter sub-provider.
 */
export function routeTargetLabel(
  route: ProviderRoute,
  providerOptions: ProviderRouteOption[],
  openRouterOptions: OpenRouterProviderOption[],
) {
  const targetProvider = routePrimaryProvider(route);
  const targetUpstreamProvider = routePrimaryUpstreamProvider(route);
  const targetOpenRouterProvider = routeOpenRouterProvider(route);
  if (targetUpstreamProvider === 'openrouter') {
    if (targetOpenRouterProvider) {
      return `OpenRouter · ${openRouterProviderLabel(openRouterOptions, targetOpenRouterProvider)}`;
    }
    return 'OpenRouter';
  }
  if (targetProvider !== targetUpstreamProvider) {
    return `${providerLabel(providerOptions, targetProvider)} → ${providerLabel(
      providerOptions,
      targetUpstreamProvider,
    )}`;
  }
  return providerLabel(providerOptions, targetProvider);
}
