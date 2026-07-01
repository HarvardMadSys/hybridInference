'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
  OpenRouterProviderOption,
  OpenRouterSortPolicy,
  ProviderApiKeyItem,
  ProviderRoute,
  ProviderRouteStrategy,
  ProviderRouteType,
  ProviderRouteOption,
  Role,
  RouteWeight,
  clearRouteWeight,
  createProviderRouteModel,
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listRouteWeights,
  listProviderKeys,
  listOpenRouterProviderOptions,
  listProviderRoutes,
  setRouteWeight,
  updateProviderRoute,
  updateProviderRouteCandidate,
  updateProviderRouteStrategy,
  verifyProviderRoute,
  verifyProviderRouteModel,
  verifyProviderRouteCandidate,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';

type RouteForm = {
  upstreamProvider: string;
  openRouterProvider: string;
  customOpenRouterProvider: string;
  openRouterSort: string;
  baseUrl: string;
  apiKeyId: string;
  providerModelId: string;
  quotaLimit: string;
};

type CreateRouteForm = {
  routeType: ProviderRouteType;
  upstreamProvider: string;
  openRouterProvider: string;
  customOpenRouterProvider: string;
  openRouterSort: string;
  baseUrl: string;
  apiKeyId: string;
  providerModelId: string;
  quotaLimit: string;
  concurrencyLimit: string;
  weight: string;
  pricingPrompt: string;
  pricingCompletion: string;
  pricingCacheReads: string;
  pricingCacheWrites: string;
};

const emptyCreateForm: CreateRouteForm = {
  routeType: 'on_demand',
  upstreamProvider: '',
  openRouterProvider: '',
  customOpenRouterProvider: '',
  openRouterSort: '',
  baseUrl: '',
  apiKeyId: '',
  providerModelId: '',
  quotaLimit: '5000',
  concurrencyLimit: '1',
  weight: '1',
  pricingPrompt: '',
  pricingCompletion: '',
  pricingCacheReads: '',
  pricingCacheWrites: '',
};

const OPENROUTER_PROVIDER_AUTO = '';
const OPENROUTER_PROVIDER_CUSTOM = '__custom__';
const OPENROUTER_ROUTING_AUTO = 'auto';
const OPENROUTER_ROUTING_SORT_PREFIX = 'sort:';
const OPENROUTER_ROUTING_PROVIDER_PREFIX = 'provider:';
const OPENROUTER_AUTO_OPTION: OpenRouterProviderOption = {
  provider: OPENROUTER_PROVIDER_AUTO,
  label: 'Auto',
};
const OPENROUTER_CUSTOM_OPTION: OpenRouterProviderOption = {
  provider: OPENROUTER_PROVIDER_CUSTOM,
  label: 'Custom...',
};
const ROUTE_TYPE_OPTIONS: Array<{ value: ProviderRouteType; label: string }> = [
  { value: 'on_demand', label: 'on_demand' },
  { value: 'quota', label: 'quota' },
  { value: 'concurrency', label: 'concurrency' },
];
const OPENROUTER_PROVIDER_SLUG_RE = /^[A-Za-z0-9_.-]+$/;
const OPENROUTER_SORT_ROUTING_OPTIONS: Array<{
  value: `${typeof OPENROUTER_ROUTING_SORT_PREFIX}${OpenRouterSortPolicy}`;
  label: string;
}> = [
  { value: 'sort:price', label: 'Sort by price' },
  { value: 'sort:throughput', label: 'Sort by throughput' },
  { value: 'sort:latency', label: 'Sort by latency' },
];
const EMPTY_PROVIDER_ROUTES: ProviderRoute[] = [];
const MODEL_ROLE_OPTIONS: Array<{ value: Role; label: string }> = [
  { value: 'admin', label: 'Admin only' },
  { value: 'internal', label: 'Internal and admins' },
  { value: 'pro', label: 'Pro, internal, and admins' },
  { value: 'free', label: 'All users' },
];

function routeKey(route: Pick<ProviderRoute, 'model_id' | 'route_id'>) {
  return `${route.model_id}\u0000${route.route_id}`;
}

function routeWeightKey(route: Pick<RouteWeight, 'model_id' | 'endpoint_id'>) {
  return `${route.model_id}\u0000${route.endpoint_id}`;
}

function pricingValueValid(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return false;
  const parsed = Number.parseFloat(trimmed);
  return Number.isFinite(parsed) && parsed >= 0;
}

function optionalPricingValueValid(value: string) {
  return !value.trim() || pricingValueValid(value);
}

function runtimePricingPayload(form: CreateRouteForm): Record<string, string> {
  const pricing: Record<string, string> = {
    prompt: form.pricingPrompt.trim(),
    completion: form.pricingCompletion.trim(),
  };
  const cacheReads = form.pricingCacheReads.trim();
  const cacheWrites = form.pricingCacheWrites.trim();
  if (cacheReads) pricing.input_cache_reads = cacheReads;
  if (cacheWrites) pricing.input_cache_writes = cacheWrites;
  return pricing;
}

function formatWeight(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function parseWeightDraft(value: string) {
  if (value.trim() === '') {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return null;
  }
  return parsed;
}

function keyLabel(route: ProviderRoute) {
  if (route.api_key.source === 'default') return `Configured default ${route.api_key.provider} key`;
  const label = route.api_key.label ? `${route.api_key.label} · ` : '';
  return `${label}${route.api_key.key_prefix ?? route.api_key.source}`;
}

function defaultKeyOptionLabel(provider: string) {
  return provider ? `Configured default ${provider} key` : 'No provider selected';
}

function noProviderOptionLabel(routeType: ProviderRouteType) {
  if (routeType === 'quota') return 'No quota providers available';
  if (routeType === 'concurrency') return 'No concurrency providers available';
  return 'No providers available';
}

function noProviderHelpText(routeType: ProviderRouteType) {
  if (routeType === 'quota') {
    return 'This model already has every configured quota provider.';
  }
  if (routeType === 'concurrency') {
    return 'This model already has every configured concurrency provider.';
  }
  return 'There are no configured providers left for this route type.';
}

function sourceLabel(route: ProviderRoute) {
  if (route.source === 'override') return 'Override active';
  if (route.source === 'runtime') return 'Runtime added';
  return 'Config default';
}

function routeLimitLabel(route: ProviderRoute, isRoutewise: boolean) {
  if (route.quota_limit) return route.quota_limit.toLocaleString();
  if (route.concurrency_limit) return route.concurrency_limit.toLocaleString();
  if (!isRoutewise) {
    return `${formatWeight(route.effective_weight)} / ${formatWeight(route.yaml_weight)}`;
  }
  return '—';
}

function canEditConcurrencyLimit(route: ProviderRoute) {
  return (
    route.source === 'runtime' &&
    route.route_type === 'concurrency' &&
    route.upstream_provider === 'openrouter' &&
    Boolean(route.openrouter_provider)
  );
}

function routeLimitHeading(isRoutewise: boolean) {
  return isRoutewise ? 'Limit' : 'Weight';
}

function routeTypeAllowedForStrategy(routeType: ProviderRouteType, isRoutewise: boolean) {
  return routeType === 'on_demand' || isRoutewise;
}

function routeTypeOptionsForStrategy(isRoutewise: boolean) {
  return ROUTE_TYPE_OPTIONS.filter((option) =>
    routeTypeAllowedForStrategy(option.value, isRoutewise),
  );
}

function optionFor(providerOptions: ProviderRouteOption[], provider: string) {
  return providerOptions.find((option) => option.provider === provider);
}

function openRouterProviderFromKind(kind: string) {
  return /^openrouter\[([A-Za-z0-9_.-]+)\]$/.exec(kind)?.[1] ?? null;
}

function isOpenRouterOption(option: ProviderRouteOption) {
  return option.kind === 'openrouter' || Boolean(openRouterProviderFromKind(option.kind));
}

function primaryProviderOptions(providerOptions: ProviderRouteOption[]) {
  const options: ProviderRouteOption[] = [];
  let openRouterOption: ProviderRouteOption | null = null;
  const seen = new Set<string>();

  for (const option of providerOptions) {
    if (isOpenRouterOption(option)) {
      openRouterOption ??= {
        provider: 'openrouter',
        label: 'OpenRouter',
        kind: 'openrouter',
        key_provider: option.key_provider,
        default_base_url: option.default_base_url,
      };
      continue;
    }
    if (seen.has(option.provider)) continue;
    seen.add(option.provider);
    options.push(option);
  }

  if (openRouterOption && !seen.has('openrouter')) {
    options.push(openRouterOption);
  }
  return options;
}

function openRouterProviderOptionsFor(
  providerOptions: ProviderRouteOption[],
  explicitOptions: OpenRouterProviderOption[] = [],
) {
  const pins = new Map<string, string>();
  for (const option of explicitOptions) {
    if (option.provider) pins.set(option.provider, option.label);
  }
  for (const option of providerOptions) {
    const provider = openRouterProviderFromKind(option.kind);
    if (provider && !pins.has(provider)) {
      pins.set(provider, option.label.replace(/\s+via OpenRouter$/i, ''));
    }
  }
  return [
    OPENROUTER_AUTO_OPTION,
    ...Array.from(pins, ([provider, label]) => ({ provider, label })),
  ];
}

function optionSupportsRouteType(option: ProviderRouteOption, routeType: ProviderRouteType) {
  if (option.provider === 'chutes') return routeType === 'quota';
  if (option.provider === 'featherless') return routeType === 'concurrency';
  if (option.provider === 'openrouter') {
    return routeType === 'on_demand' || routeType === 'concurrency';
  }
  return routeType === 'on_demand';
}

function routePrimaryProvider(route: ProviderRoute) {
  if (route.provider === route.upstream_provider && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.provider;
}

function routePrimaryUpstreamProvider(route: ProviderRoute) {
  if (route.upstream_provider !== 'openrouter' && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.upstream_provider;
}

function routeOpenRouterProvider(route: ProviderRoute) {
  if (route.openrouter_provider) return route.openrouter_provider;
  if (route.upstream_provider !== 'openrouter' && route.key_provider === 'openrouter') {
    return route.upstream_provider;
  }
  return OPENROUTER_PROVIDER_AUTO;
}

function providerLabel(providerOptions: ProviderRouteOption[], provider: string) {
  return optionFor(providerOptions, provider)?.label ?? provider;
}

function openRouterProviderLabel(options: OpenRouterProviderOption[], provider: string) {
  return options.find((option) => option.provider === provider)?.label ?? provider;
}

function ensureOpenRouterProviderOption(options: OpenRouterProviderOption[], provider: string) {
  if (
    !provider ||
    provider === OPENROUTER_PROVIDER_CUSTOM ||
    options.some((option) => option.provider === provider)
  ) {
    return options;
  }
  return [...options, { provider, label: provider }];
}

function withCustomOpenRouterOption(options: OpenRouterProviderOption[]) {
  return options.some((option) => option.provider === OPENROUTER_PROVIDER_CUSTOM)
    ? options
    : [...options, OPENROUTER_CUSTOM_OPTION];
}

function openRouterRoutingValue(selectedProvider: string, sort: string) {
  if (selectedProvider) return `${OPENROUTER_ROUTING_PROVIDER_PREFIX}${selectedProvider}`;
  if (sort) return `${OPENROUTER_ROUTING_SORT_PREFIX}${sort}`;
  return OPENROUTER_ROUTING_AUTO;
}

function openRouterProviderFromRoutingValue(value: string) {
  return value.startsWith(OPENROUTER_ROUTING_PROVIDER_PREFIX)
    ? value.slice(OPENROUTER_ROUTING_PROVIDER_PREFIX.length)
    : OPENROUTER_PROVIDER_AUTO;
}

function openRouterSortFromRoutingValue(value: string) {
  return value.startsWith(OPENROUTER_ROUTING_SORT_PREFIX)
    ? value.slice(OPENROUTER_ROUTING_SORT_PREFIX.length)
    : '';
}

function openRouterRoutingOptions(
  options: OpenRouterProviderOption[],
  allowAutomaticRouting = true,
) {
  const providerOptions = options
    .filter((option) => option.provider !== OPENROUTER_PROVIDER_AUTO)
    .map((option) => ({
      value: `${OPENROUTER_ROUTING_PROVIDER_PREFIX}${option.provider}`,
      label:
        option.provider === OPENROUTER_PROVIDER_CUSTOM
          ? 'Custom provider...'
          : `Provider: ${option.label}`,
    }));
  if (!allowAutomaticRouting) return providerOptions;
  return [
    { value: OPENROUTER_ROUTING_AUTO, label: 'Auto' },
    ...OPENROUTER_SORT_ROUTING_OPTIONS,
    ...providerOptions,
  ];
}

function resolvedOpenRouterProvider(selectedProvider: string, customProvider: string) {
  if (selectedProvider === OPENROUTER_PROVIDER_CUSTOM) {
    const cleaned = customProvider.trim();
    return cleaned || null;
  }
  return selectedProvider || null;
}

function formSignature(value: unknown) {
  return JSON.stringify(value);
}

function customOpenRouterProviderValid(selectedProvider: string, customProvider: string) {
  if (selectedProvider !== OPENROUTER_PROVIDER_CUSTOM) return true;
  return OPENROUTER_PROVIDER_SLUG_RE.test(customProvider.trim());
}

function openRouterSortForPayload(
  upstreamProvider: string,
  selectedOpenRouterProvider: string,
  sort: string,
) {
  if (upstreamProvider !== 'openrouter') return null;
  if (selectedOpenRouterProvider !== OPENROUTER_PROVIDER_AUTO) return null;
  return sort ? (sort as OpenRouterSortPolicy) : null;
}

function defaultProviderModelIdFor(provider: string, routes: ProviderRoute[]) {
  const matchingRoute = routes.find(
    (route) =>
      routePrimaryUpstreamProvider(route) === provider &&
      typeof route.provider_model_id === 'string' &&
      route.provider_model_id.trim(),
  );
  return matchingRoute?.provider_model_id ?? '';
}

function openRouterProviderOptionsForCreate(
  allOptions: OpenRouterProviderOption[],
  routeType: ProviderRouteType,
  routes: ProviderRoute[],
) {
  if (routeType !== 'on_demand' && routeType !== 'concurrency') return [];
  const usedPins = new Set(
    routes
      .filter(
        (route) =>
          route.route_type === routeType && routePrimaryUpstreamProvider(route) === 'openrouter',
      )
      .map(routeOpenRouterProvider),
  );
  return allOptions.filter(
    (option) =>
      !usedPins.has(option.provider) &&
      (routeType === 'on_demand' || option.provider !== OPENROUTER_PROVIDER_AUTO),
  );
}

function createProviderOptionsFor(
  providerOptions: ProviderRouteOption[],
  routeType: ProviderRouteType,
  routes: ProviderRoute[] = [],
) {
  const usedProviders = new Set(
    routes
      .filter((route) => route.route_type === routeType)
      .map((route) => routePrimaryProvider(route)),
  );
  return providerOptions.filter(
    (option) =>
      optionSupportsRouteType(option, routeType) &&
      (option.provider === 'openrouter' || !usedProviders.has(option.provider)),
  );
}

interface ProviderRoutesTabProps {
  showRoutewiseSettings?: boolean;
}

export function ProviderRoutesTab({ showRoutewiseSettings = false }: ProviderRoutesTabProps = {}) {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [routeWeights, setRouteWeights] = useState<RouteWeight[]>([]);
  const [draftWeights, setDraftWeights] = useState<Record<string, string>>({});
  const [draftConcurrencyLimits, setDraftConcurrencyLimits] = useState<Record<string, string>>({});
  const [savingWeightKey, setSavingWeightKey] = useState<string | null>(null);
  const [savingConcurrencyKey, setSavingConcurrencyKey] = useState<string | null>(null);
  const [providerOptions, setProviderOptions] = useState<ProviderRouteOption[]>([]);
  const [openRouterProviderOptions, setOpenRouterProviderOptions] = useState<
    OpenRouterProviderOption[]
  >([]);
  const [discoveredOpenRouterProviderOptions, setDiscoveredOpenRouterProviderOptions] = useState<
    OpenRouterProviderOption[]
  >([]);
  const [openRouterProvidersLoading, setOpenRouterProvidersLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(false);
  const [editingRoute, setEditingRoute] = useState<ProviderRoute | null>(null);
  const [addingRoute, setAddingRoute] = useState(false);
  const [creatingModel, setCreatingModel] = useState(false);
  const [newModelId, setNewModelId] = useState('');
  const [newModelStrategy, setNewModelStrategy] = useState<ProviderRouteStrategy>('fixed');
  const [newModelRequiredRole, setNewModelRequiredRole] = useState<Role>('admin');
  const [form, setForm] = useState<RouteForm>({
    upstreamProvider: '',
    openRouterProvider: '',
    customOpenRouterProvider: '',
    openRouterSort: '',
    baseUrl: '',
    apiKeyId: '',
    providerModelId: '',
    quotaLimit: '',
  });
  const [createForm, setCreateForm] = useState<CreateRouteForm>(emptyCreateForm);
  const [keyOptions, setKeyOptions] = useState<ProviderApiKeyItem[]>([]);
  const [createKeyOptions, setCreateKeyOptions] = useState<ProviderApiKeyItem[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [createKeysLoading, setCreateKeysLoading] = useState(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [creatingRoute, setCreatingRoute] = useState(false);
  const [verifyingKey, setVerifyingKey] = useState<string | null>(null);
  const [verifyingCreateRoute, setVerifyingCreateRoute] = useState(false);
  const [verifiedEditSignature, setVerifiedEditSignature] = useState<string | null>(null);
  const [verifiedCreateSignature, setVerifiedCreateSignature] = useState<string | null>(null);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [savingStrategy, setSavingStrategy] = useState(false);

  const loadRoutes = useCallback(async () => {
    setLoading(true);
    try {
      const [resp, loadedWeights] = await Promise.all([listProviderRoutes(), listRouteWeights()]);
      setRoutes(resp.routes);
      setRouteWeights(loadedWeights);
      setProviderOptions(resp.provider_options);
      setOpenRouterProviderOptions(resp.openrouter_provider_options ?? []);
      const models = Array.from(new Set(resp.routes.map((route) => route.model_id))).sort();
      setSelectedModel((current) => current || models[0] || '');
    } catch (err) {
      toast.error(`Failed to load provider routes: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRoutes();
  }, [loadRoutes]);

  const models = useMemo(
    () => Array.from(new Set(routes.map((route) => route.model_id))).sort(),
    [routes],
  );
  const selectedRoutes = useMemo(
    () => routes.filter((route) => route.model_id === selectedModel),
    [routes, selectedModel],
  );
  const createFormOpen = addingRoute || creatingModel;
  const createTargetRoutes = creatingModel ? EMPTY_PROVIDER_ROUTES : selectedRoutes;
  const newModelIdValue = newModelId.trim();
  const newModelIdAvailable = !newModelIdValue || !models.includes(newModelIdValue);
  const routeWeightByKey = useMemo(() => {
    const byKey = new Map<string, RouteWeight>();
    for (const routeWeight of routeWeights) {
      byKey.set(routeWeightKey(routeWeight), routeWeight);
    }
    return byKey;
  }, [routeWeights]);
  const providerSelectBaseOptions = useMemo(
    () => primaryProviderOptions(providerOptions),
    [providerOptions],
  );
  const openRouterSelectOptions = useMemo(
    () =>
      openRouterProviderOptionsFor(
        providerOptions,
        discoveredOpenRouterProviderOptions.length > 0
          ? discoveredOpenRouterProviderOptions
          : openRouterProviderOptions,
      ),
    [discoveredOpenRouterProviderOptions, openRouterProviderOptions, providerOptions],
  );
  const editOpenRouterSelectOptions = useMemo(
    () =>
      withCustomOpenRouterOption(
        ensureOpenRouterProviderOption(openRouterSelectOptions, form.openRouterProvider),
      ),
    [form.openRouterProvider, openRouterSelectOptions],
  );
  const selectedOpenRouterProvider = resolvedOpenRouterProvider(
    form.openRouterProvider,
    form.customOpenRouterProvider,
  );
  const selectedCreateOpenRouterProvider = resolvedOpenRouterProvider(
    createForm.openRouterProvider,
    createForm.customOpenRouterProvider,
  );
  const activeOpenRouterProviderModelId =
    createFormOpen && createForm.upstreamProvider === 'openrouter'
      ? createForm.providerModelId.trim()
      : editingRoute && form.upstreamProvider === 'openrouter'
        ? form.providerModelId.trim()
        : '';
  const strategy = selectedRoutes[0]?.strategy ?? 'fixed';
  const isRoutewise = strategy === 'routewise';
  const createStrategy = creatingModel ? newModelStrategy : strategy;
  const createIsRoutewise = createStrategy === 'routewise';
  const createRouteTypeOptions = useMemo(
    () => routeTypeOptionsForStrategy(createIsRoutewise),
    [createIsRoutewise],
  );
  const showCreateWeight = !createIsRoutewise;
  const showCreateLimits =
    createForm.routeType === 'quota' || createForm.routeType === 'concurrency' || showCreateWeight;
  const editsLocalQuota =
    editingRoute?.route_type === 'quota' &&
    (form.upstreamProvider !== routePrimaryProvider(editingRoute) ||
      (form.upstreamProvider === 'openrouter' &&
        (selectedOpenRouterProvider ?? OPENROUTER_PROVIDER_AUTO) !==
          routeOpenRouterProvider(editingRoute)));
  const parsedQuotaLimit = editsLocalQuota ? Number.parseInt(form.quotaLimit, 10) : null;
  const quotaLimitValid =
    !editsLocalQuota ||
    (parsedQuotaLimit !== null && Number.isInteger(parsedQuotaLimit) && parsedQuotaLimit > 0);
  const parsedCreateQuotaLimit =
    createForm.routeType === 'quota' ? Number.parseInt(createForm.quotaLimit, 10) : null;
  const parsedCreateConcurrencyLimit =
    createForm.routeType === 'concurrency'
      ? Number.parseInt(createForm.concurrencyLimit, 10)
      : null;
  const parsedCreateWeight = Number.parseFloat(createForm.weight);
  const createPricingValid =
    !creatingModel ||
    (pricingValueValid(createForm.pricingPrompt) &&
      pricingValueValid(createForm.pricingCompletion) &&
      optionalPricingValueValid(createForm.pricingCacheReads) &&
      optionalPricingValueValid(createForm.pricingCacheWrites));
  const createQuotaValid =
    createForm.routeType !== 'quota' ||
    (parsedCreateQuotaLimit !== null &&
      Number.isInteger(parsedCreateQuotaLimit) &&
      parsedCreateQuotaLimit > 0);
  const createConcurrencyValid =
    createForm.routeType !== 'concurrency' ||
    (parsedCreateConcurrencyLimit !== null &&
      Number.isInteger(parsedCreateConcurrencyLimit) &&
      parsedCreateConcurrencyLimit > 0);
  const createWeightValid =
    createIsRoutewise || (Number.isFinite(parsedCreateWeight) && parsedCreateWeight > 0);
  const createRouteTypeValid = routeTypeAllowedForStrategy(createForm.routeType, createIsRoutewise);
  const formOpenRouterProviderValid = customOpenRouterProviderValid(
    form.openRouterProvider,
    form.customOpenRouterProvider,
  );
  const createOpenRouterProviderValid = customOpenRouterProviderValid(
    createForm.openRouterProvider,
    createForm.customOpenRouterProvider,
  );
  const createFormValid =
    (creatingModel ? Boolean(newModelIdValue) && newModelIdAvailable : Boolean(selectedModel)) &&
    Boolean(createForm.upstreamProvider) &&
    Boolean(createForm.baseUrl.trim()) &&
    Boolean(createForm.providerModelId.trim()) &&
    createOpenRouterProviderValid &&
    createRouteTypeValid &&
    createQuotaValid &&
    createConcurrencyValid &&
    createWeightValid &&
    createPricingValid;
  const editFormValid =
    Boolean(editingRoute) &&
    Boolean(form.upstreamProvider) &&
    Boolean(form.baseUrl.trim()) &&
    Boolean(form.providerModelId.trim()) &&
    formOpenRouterProviderValid &&
    quotaLimitValid;
  const editRoutePayload = useMemo(() => {
    if (!editingRoute) {
      return null;
    }
    return {
      upstream_provider: form.upstreamProvider,
      openrouter_provider:
        form.upstreamProvider === 'openrouter' ? selectedOpenRouterProvider : null,
      openrouter_sort: openRouterSortForPayload(
        form.upstreamProvider,
        form.openRouterProvider,
        form.openRouterSort,
      ),
      base_url: form.baseUrl.trim(),
      api_key_id: form.apiKeyId || null,
      provider_model_id: form.providerModelId.trim(),
      quota_limit: editsLocalQuota ? parsedQuotaLimit : null,
    };
  }, [
    editingRoute,
    editsLocalQuota,
    form.apiKeyId,
    form.baseUrl,
    form.openRouterProvider,
    form.openRouterSort,
    form.providerModelId,
    form.upstreamProvider,
    parsedQuotaLimit,
    selectedOpenRouterProvider,
  ]);
  const runtimePricing = runtimePricingPayload(createForm);
  const createRoutePayload = useMemo(
    () => ({
      route_type: createForm.routeType,
      upstream_provider: createForm.upstreamProvider,
      openrouter_provider:
        createForm.upstreamProvider === 'openrouter' ? selectedCreateOpenRouterProvider : null,
      openrouter_sort: openRouterSortForPayload(
        createForm.upstreamProvider,
        createForm.openRouterProvider,
        createForm.openRouterSort,
      ),
      base_url: createForm.baseUrl.trim(),
      api_key_id: createForm.apiKeyId || null,
      provider_model_id: createForm.providerModelId.trim(),
      quota_limit: createForm.routeType === 'quota' ? parsedCreateQuotaLimit : null,
      concurrency_limit:
        createForm.routeType === 'concurrency' ? parsedCreateConcurrencyLimit : null,
      weight: createIsRoutewise ? 1 : parsedCreateWeight,
    }),
    [
      createForm.apiKeyId,
      createForm.baseUrl,
      createForm.openRouterProvider,
      createForm.openRouterSort,
      createForm.providerModelId,
      createForm.routeType,
      createForm.upstreamProvider,
      parsedCreateConcurrencyLimit,
      parsedCreateQuotaLimit,
      parsedCreateWeight,
      selectedCreateOpenRouterProvider,
      createIsRoutewise,
    ],
  );
  const editVerificationSignature = (() => {
    if (!editingRoute || !editRoutePayload) {
      return null;
    }
    return formSignature({
      model_id: editingRoute.model_id,
      route_id: editingRoute.route_id,
      payload: editRoutePayload,
    });
  })();
  const createVerificationSignature = formSignature({
    model_id: creatingModel ? newModelIdValue : selectedModel,
    strategy: creatingModel ? newModelStrategy : strategy,
    required_role: creatingModel ? newModelRequiredRole : undefined,
    pricing: creatingModel ? runtimePricing : undefined,
    payload: createRoutePayload,
  });
  const editRouteVerified =
    Boolean(editVerificationSignature) && verifiedEditSignature === editVerificationSignature;
  const createRouteVerified = verifiedCreateSignature === createVerificationSignature;

  useEffect(() => {
    if (!editingRoute) {
      setForm({
        upstreamProvider: '',
        openRouterProvider: '',
        customOpenRouterProvider: '',
        openRouterSort: '',
        baseUrl: '',
        apiKeyId: '',
        providerModelId: '',
        quotaLimit: '',
      });
      setKeyOptions([]);
      return;
    }
    setForm({
      upstreamProvider: routePrimaryUpstreamProvider(editingRoute),
      openRouterProvider: routeOpenRouterProvider(editingRoute),
      customOpenRouterProvider: '',
      openRouterSort: editingRoute.openrouter_sort ?? '',
      baseUrl: editingRoute.base_url,
      apiKeyId: editingRoute.api_key_id ?? '',
      providerModelId: editingRoute.provider_model_id ?? '',
      quotaLimit: editingRoute.quota_limit ? String(editingRoute.quota_limit) : '',
    });
  }, [editingRoute]);

  const keyProvider = useMemo(() => {
    const provider = optionFor(providerSelectBaseOptions, form.upstreamProvider);
    return provider?.key_provider ?? form.upstreamProvider;
  }, [form.upstreamProvider, providerSelectBaseOptions]);

  const createKeyProvider = useMemo(() => {
    const provider = optionFor(providerSelectBaseOptions, createForm.upstreamProvider);
    return provider?.key_provider ?? createForm.upstreamProvider;
  }, [createForm.upstreamProvider, providerSelectBaseOptions]);

  const createOpenRouterProviderOptions = useMemo(
    () =>
      openRouterProviderOptionsForCreate(
        openRouterSelectOptions,
        createForm.routeType,
        createTargetRoutes,
      ),
    [createForm.routeType, openRouterSelectOptions, createTargetRoutes],
  );
  const createOpenRouterSelectOptions = useMemo(
    () => withCustomOpenRouterOption(createOpenRouterProviderOptions),
    [createOpenRouterProviderOptions],
  );
  const createOpenRouterRoutingOptions = useMemo(
    () =>
      openRouterRoutingOptions(createOpenRouterSelectOptions, createForm.routeType === 'on_demand'),
    [createForm.routeType, createOpenRouterSelectOptions],
  );
  const editOpenRouterRoutingOptions = useMemo(
    () =>
      openRouterRoutingOptions(
        editOpenRouterSelectOptions,
        editingRoute?.route_type === 'on_demand',
      ),
    [editingRoute?.route_type, editOpenRouterSelectOptions],
  );

  const createProviderOptions = useMemo(
    () =>
      createProviderOptionsFor(providerSelectBaseOptions, createForm.routeType, createTargetRoutes),
    [createForm.routeType, providerSelectBaseOptions, createTargetRoutes],
  );

  useEffect(() => {
    if (!createFormOpen || createRouteTypeValid) return;
    const routeType: ProviderRouteType = 'on_demand';
    const nextOpenRouterOptions = openRouterProviderOptionsForCreate(
      openRouterSelectOptions,
      routeType,
      createTargetRoutes,
    );
    const nextProvider = createProviderOptionsFor(
      providerSelectBaseOptions,
      routeType,
      createTargetRoutes,
    )[0];
    setCreateForm((current) => ({
      ...current,
      routeType,
      upstreamProvider: nextProvider?.provider ?? '',
      openRouterProvider:
        nextProvider?.provider === 'openrouter'
          ? (nextOpenRouterOptions[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
          : OPENROUTER_PROVIDER_AUTO,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: nextProvider?.default_base_url ?? '',
      apiKeyId: '',
      providerModelId: nextProvider
        ? defaultProviderModelIdFor(nextProvider.provider, createTargetRoutes)
        : current.providerModelId,
    }));
    setVerifiedCreateSignature(null);
  }, [
    createFormOpen,
    createRouteTypeValid,
    createTargetRoutes,
    openRouterSelectOptions,
    providerSelectBaseOptions,
  ]);

  const providerSelectOptions = useMemo(() => {
    if (!form.upstreamProvider || optionFor(providerSelectBaseOptions, form.upstreamProvider)) {
      return providerSelectBaseOptions;
    }
    return [
      ...providerSelectBaseOptions,
      {
        provider: form.upstreamProvider,
        label: form.upstreamProvider,
        kind: form.upstreamProvider,
        key_provider: form.upstreamProvider,
        default_base_url: form.baseUrl,
      },
    ];
  }, [form.baseUrl, form.upstreamProvider, providerSelectBaseOptions]);

  const loadKeys = useCallback(async (provider: string) => {
    if (!provider) {
      setKeyOptions([]);
      return;
    }
    setKeysLoading(true);
    try {
      const resp = await listProviderKeys(provider);
      setKeyOptions(resp.keys.filter((key) => key.id));
    } catch (err) {
      setKeyOptions([]);
      toast.error(`Failed to load provider keys: ${getErrorMessage(err)}`);
    } finally {
      setKeysLoading(false);
    }
  }, []);

  const loadCreateKeys = useCallback(async (provider: string) => {
    if (!provider) {
      setCreateKeyOptions([]);
      return;
    }
    setCreateKeysLoading(true);
    try {
      const resp = await listProviderKeys(provider);
      setCreateKeyOptions(resp.keys.filter((key) => key.id));
    } catch (err) {
      setCreateKeyOptions([]);
      toast.error(`Failed to load provider keys: ${getErrorMessage(err)}`);
    } finally {
      setCreateKeysLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!editingRoute || !keyProvider) return;
    void loadKeys(keyProvider);
  }, [editingRoute, keyProvider, loadKeys]);

  useEffect(() => {
    if (!createFormOpen || !createKeyProvider) return;
    void loadCreateKeys(createKeyProvider);
  }, [createFormOpen, createKeyProvider, loadCreateKeys]);

  useEffect(() => {
    setDiscoveredOpenRouterProviderOptions([]);
    if (!activeOpenRouterProviderModelId.includes('/')) return undefined;
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      setOpenRouterProvidersLoading(true);
      void listOpenRouterProviderOptions(activeOpenRouterProviderModelId)
        .then((resp) => {
          if (cancelled) return;
          setDiscoveredOpenRouterProviderOptions(resp.providers);
        })
        .catch(() => {
          // Keep the fallback options returned by the route list endpoint.
        })
        .finally(() => {
          if (!cancelled) setOpenRouterProvidersLoading(false);
        });
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [activeOpenRouterProviderModelId]);

  useEffect(() => {
    if (!createFormOpen) return;
    if (createProviderOptions.length === 0) {
      setCreateForm((current) => ({
        ...current,
        upstreamProvider: '',
        openRouterProvider: '',
        customOpenRouterProvider: '',
        openRouterSort: '',
        baseUrl: '',
        apiKeyId: '',
      }));
      return;
    }
    if (optionFor(createProviderOptions, createForm.upstreamProvider)) return;
    const nextProvider = createProviderOptions[0];
    setCreateForm((current) => ({
      ...current,
      upstreamProvider: nextProvider.provider,
      openRouterProvider:
        nextProvider.provider === 'openrouter'
          ? (createOpenRouterProviderOptions[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
          : OPENROUTER_PROVIDER_AUTO,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: nextProvider.default_base_url,
      apiKeyId: '',
    }));
  }, [
    createFormOpen,
    createForm.upstreamProvider,
    createOpenRouterProviderOptions,
    createProviderOptions,
  ]);

  const updateRoute = useCallback((updated: ProviderRoute) => {
    setRoutes((current) =>
      current.map((route) => (routeKey(route) === routeKey(updated) ? updated : route)),
    );
  }, []);

  const replaceRouteWeight = useCallback((updated: RouteWeight) => {
    setRouteWeights((current) => {
      const key = routeWeightKey(updated);
      const replaced = current.map((weight) => (routeWeightKey(weight) === key ? updated : weight));
      return replaced.some((weight) => routeWeightKey(weight) === key)
        ? replaced
        : [...current, updated];
    });
  }, []);

  const replaceRoute = useCallback(
    (updated: ProviderRoute) => {
      updateRoute(updated);
      setEditingRoute(updated);
    },
    [updateRoute],
  );

  const replaceModelRoutes = useCallback((modelId: string, updated: ProviderRoute[]) => {
    setRoutes((current) => [...current.filter((route) => route.model_id !== modelId), ...updated]);
  }, []);

  const onUpstreamProviderChange = (upstreamProvider: string) => {
    const selected = optionFor(providerSelectOptions, upstreamProvider);
    setForm((current) => ({
      upstreamProvider,
      openRouterProvider:
        upstreamProvider === 'openrouter'
          ? current.openRouterProvider ||
            (editingRoute ? routeOpenRouterProvider(editingRoute) : OPENROUTER_PROVIDER_AUTO)
          : OPENROUTER_PROVIDER_AUTO,
      customOpenRouterProvider:
        upstreamProvider === 'openrouter' ? current.customOpenRouterProvider : '',
      openRouterSort:
        upstreamProvider === 'openrouter'
          ? (editingRoute?.openrouter_sort ?? current.openRouterSort)
          : '',
      baseUrl: selected?.default_base_url || current.baseUrl,
      apiKeyId: '',
      providerModelId:
        editingRoute && upstreamProvider === routePrimaryUpstreamProvider(editingRoute)
          ? (editingRoute.provider_model_id ?? current.providerModelId)
          : defaultProviderModelIdFor(upstreamProvider, selectedRoutes) || current.providerModelId,
      quotaLimit: current.quotaLimit,
    }));
  };

  const openAddForm = () => {
    const firstProvider = createProviderOptionsFor(
      providerSelectBaseOptions,
      emptyCreateForm.routeType,
      selectedRoutes,
    )[0];
    const availableOpenRouterProviders = openRouterProviderOptionsForCreate(
      openRouterSelectOptions,
      emptyCreateForm.routeType,
      selectedRoutes,
    );
    const nextOpenRouterProvider =
      firstProvider?.provider === 'openrouter'
        ? (availableOpenRouterProviders[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
        : OPENROUTER_PROVIDER_AUTO;
    setCreateForm({
      ...emptyCreateForm,
      upstreamProvider: firstProvider?.provider ?? '',
      openRouterProvider: nextOpenRouterProvider,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: firstProvider?.default_base_url ?? '',
      providerModelId: firstProvider
        ? defaultProviderModelIdFor(firstProvider.provider, selectedRoutes)
        : '',
    });
    setCreateKeyOptions([]);
    setVerifiedCreateSignature(null);
    setAddingRoute(true);
    setCreatingModel(false);
    setEditingRoute(null);
  };

  const openCreateModelForm = () => {
    const firstProvider = createProviderOptionsFor(
      providerSelectBaseOptions,
      emptyCreateForm.routeType,
      [],
    )[0];
    const availableOpenRouterProviders = openRouterProviderOptionsForCreate(
      openRouterSelectOptions,
      emptyCreateForm.routeType,
      [],
    );
    const nextOpenRouterProvider =
      firstProvider?.provider === 'openrouter'
        ? (availableOpenRouterProviders[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
        : OPENROUTER_PROVIDER_AUTO;
    setCreateForm({
      ...emptyCreateForm,
      upstreamProvider: firstProvider?.provider ?? '',
      openRouterProvider: nextOpenRouterProvider,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: firstProvider?.default_base_url ?? '',
      providerModelId: '',
    });
    setNewModelId('');
    setNewModelStrategy('fixed');
    setNewModelRequiredRole('admin');
    setCreateKeyOptions([]);
    setVerifiedCreateSignature(null);
    setCreatingModel(true);
    setAddingRoute(false);
    setEditingRoute(null);
  };

  const onCreateProviderChange = (upstreamProvider: string) => {
    const selected = optionFor(createProviderOptions, upstreamProvider);
    setCreateForm((current) => ({
      ...current,
      upstreamProvider,
      openRouterProvider:
        upstreamProvider === 'openrouter'
          ? (createOpenRouterProviderOptions[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
          : OPENROUTER_PROVIDER_AUTO,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: selected?.default_base_url || current.baseUrl,
      apiKeyId: '',
      providerModelId:
        defaultProviderModelIdFor(upstreamProvider, createTargetRoutes) || current.providerModelId,
    }));
  };

  const onCreateRouteTypeChange = (routeType: ProviderRouteType) => {
    if (!routeTypeAllowedForStrategy(routeType, createIsRoutewise)) return;
    const nextOpenRouterOptions = openRouterProviderOptionsForCreate(
      openRouterSelectOptions,
      routeType,
      createTargetRoutes,
    );
    const nextProvider = createProviderOptionsFor(
      providerSelectBaseOptions,
      routeType,
      createTargetRoutes,
    )[0];
    setCreateForm((current) => ({
      ...current,
      routeType,
      upstreamProvider: nextProvider?.provider ?? '',
      openRouterProvider:
        nextProvider?.provider === 'openrouter'
          ? (nextOpenRouterOptions[0]?.provider ?? OPENROUTER_PROVIDER_CUSTOM)
          : OPENROUTER_PROVIDER_AUTO,
      customOpenRouterProvider: '',
      openRouterSort: '',
      baseUrl: nextProvider?.default_base_url ?? '',
      apiKeyId: '',
      providerModelId: nextProvider
        ? defaultProviderModelIdFor(nextProvider.provider, createTargetRoutes)
        : current.providerModelId,
    }));
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingRoute || !editRoutePayload || !editFormValid) return;
    const key = routeKey(editingRoute);
    setSavingKey(key);
    try {
      const updated = await updateProviderRoute(
        editingRoute.model_id,
        editingRoute.route_id,
        editRoutePayload,
      );
      replaceRoute(updated);
      toast.success('Provider route updated');
    } catch (err) {
      toast.error(`Update failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingKey(null);
    }
  };

  const onVerify = async () => {
    if (!editingRoute || !editRoutePayload || !editFormValid || !editVerificationSignature) return;
    const key = routeKey(editingRoute);
    setVerifyingKey(key);
    try {
      await verifyProviderRoute(editingRoute.model_id, editingRoute.route_id, editRoutePayload);
      setVerifiedEditSignature(editVerificationSignature);
      toast.success('Provider route verified');
    } catch (err) {
      setVerifiedEditSignature(null);
      toast.error(`Verify failed: ${getErrorMessage(err)}`);
    } finally {
      setVerifyingKey(null);
    }
  };

  const onCreateSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!createFormValid) return;
    setCreatingRoute(true);
    try {
      let created: ProviderRoute;
      if (creatingModel) {
        created = await createProviderRouteModel({
          ...createRoutePayload,
          model_id: newModelIdValue,
          strategy: newModelStrategy,
          required_role: newModelRequiredRole,
          pricing: runtimePricing,
        });
      } else {
        created = await createProviderRouteCandidate(selectedModel, createRoutePayload);
      }
      setRoutes((current) => [...current, created]);
      if (creatingModel) {
        setSelectedModel(created.model_id);
      }
      setAddingRoute(false);
      setCreatingModel(false);
      setVerifiedCreateSignature(null);
      toast.success(creatingModel ? 'Model created' : 'Provider route added');
    } catch (err) {
      toast.error(`${creatingModel ? 'Create' : 'Add'} failed: ${getErrorMessage(err)}`);
    } finally {
      setCreatingRoute(false);
    }
  };

  const onCreateVerify = async () => {
    if (!createFormValid) return;
    setVerifyingCreateRoute(true);
    try {
      if (creatingModel) {
        await verifyProviderRouteModel({
          ...createRoutePayload,
          model_id: newModelIdValue,
          strategy: newModelStrategy,
          required_role: newModelRequiredRole,
          pricing: runtimePricing,
        });
      } else {
        await verifyProviderRouteCandidate(selectedModel, createRoutePayload);
      }
      setVerifiedCreateSignature(createVerificationSignature);
      toast.success(creatingModel ? 'Model route verified' : 'Provider route verified');
    } catch (err) {
      setVerifiedCreateSignature(null);
      toast.error(`Verify failed: ${getErrorMessage(err)}`);
    } finally {
      setVerifyingCreateRoute(false);
    }
  };

  const onResetYaml = async (route: ProviderRoute) => {
    const key = routeKey(route);
    setResettingKey(key);
    try {
      const updated = await deleteProviderRoute(route.model_id, route.route_id);
      updateRoute(updated);
      setEditingRoute((current) => (current && routeKey(current) === key ? null : current));
      toast.success('Reset route to config');
    } catch (err) {
      toast.error(`Reset failed: ${getErrorMessage(err)}`);
    } finally {
      setResettingKey(null);
    }
  };

  const onDeleteRuntimeRoute = async (route: ProviderRoute) => {
    const key = routeKey(route);
    setDeletingKey(key);
    try {
      const updated = await deleteProviderRouteCandidate(route.model_id, route.route_id);
      replaceModelRoutes(updated.model_id ?? route.model_id, updated.routes);
      setEditingRoute((current) => (current && routeKey(current) === key ? null : current));
      toast.success('Provider route deleted');
    } catch (err) {
      toast.error(`Delete failed: ${getErrorMessage(err)}`);
    } finally {
      setDeletingKey(null);
    }
  };

  const onStrategyChange = async (nextStrategy: ProviderRouteStrategy) => {
    if (!selectedModel || nextStrategy === strategy) return;
    setSavingStrategy(true);
    try {
      const updated = await updateProviderRouteStrategy(selectedModel, nextStrategy);
      replaceModelRoutes(updated.model_id ?? selectedModel, updated.routes);
      setEditingRoute(null);
      toast.success('Routing policy updated');
    } catch (err) {
      toast.error(`Policy update failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingStrategy(false);
    }
  };

  const onSaveWeight = async (route: ProviderRoute) => {
    const key = routeWeightKey({ model_id: route.model_id, endpoint_id: route.endpoint_id });
    const currentWeight = routeWeightByKey.get(key);
    const draft =
      draftWeights[key] ?? formatWeight(currentWeight?.effective_weight ?? route.effective_weight);
    const parsed = parseWeightDraft(draft);
    if (parsed === null) {
      toast.error('Weight must be a non-negative number.');
      return;
    }

    setSavingWeightKey(key);
    try {
      const updated = await setRouteWeight(route.model_id, route.endpoint_id, parsed);
      replaceRouteWeight(updated);
      setDraftWeights((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      toast.success(`Updated ${route.endpoint_id} weight.`);
    } catch (err) {
      toast.error(`Weight update failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingWeightKey(null);
    }
  };

  const onSaveConcurrencyLimit = async (route: ProviderRoute) => {
    const key = routeKey(route);
    const draft = draftConcurrencyLimits[key] ?? String(route.concurrency_limit ?? '');
    const trimmed = draft.trim();
    const parsed = /^\d+$/.test(trimmed) ? Number.parseInt(trimmed, 10) : null;
    if (parsed === null || parsed < 1) {
      toast.error('Concurrency limit must be a positive integer.');
      return;
    }

    setSavingConcurrencyKey(key);
    try {
      const updated = await updateProviderRouteCandidate(route.model_id, route.route_id, {
        concurrency_limit: parsed,
      });
      updateRoute(updated);
      setDraftConcurrencyLimits((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      toast.success('Concurrency limit updated');
    } catch (err) {
      toast.error(`Concurrency update failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingConcurrencyKey(null);
    }
  };

  const onClearWeight = async (route: ProviderRoute) => {
    const key = routeWeightKey({ model_id: route.model_id, endpoint_id: route.endpoint_id });
    setSavingWeightKey(key);
    try {
      const updated = await clearRouteWeight(route.model_id, route.endpoint_id);
      replaceRouteWeight(updated);
      setDraftWeights((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      toast.success(`Cleared ${route.endpoint_id} weight override.`);
    } catch (err) {
      toast.error(`Weight reset failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingWeightKey(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <label className="text-[12px] font-medium text-gray-500" htmlFor="provider-route-model">
            Selected model
          </label>
          <select
            id="provider-route-model"
            value={selectedModel}
            onChange={(event) => {
              setSelectedModel(event.target.value);
              setEditingRoute(null);
              setAddingRoute(false);
              setCreatingModel(false);
            }}
            className="mt-1 w-full min-w-[240px] max-w-sm rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
          >
            {models.length === 0 && <option value="">(no models loaded)</option>}
            {models.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          {loading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          <label className="sr-only" htmlFor="provider-route-strategy">
            Routing policy
          </label>
          <select
            id="provider-route-strategy"
            value={strategy}
            onChange={(event) => void onStrategyChange(event.target.value as ProviderRouteStrategy)}
            disabled={!selectedModel || loading || savingStrategy}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-medium text-gray-700 focus:border-gray-400 focus:outline-none disabled:opacity-50"
          >
            <option value="routewise">routewise</option>
            <option value="fixed">fixed</option>
          </select>
          {savingStrategy && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          <button
            type="button"
            onClick={openAddForm}
            disabled={!selectedModel || loading || providerOptions.length === 0}
            className="rounded-lg border border-gray-900 bg-gray-900 px-3 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Add provider
          </button>
          <button
            type="button"
            onClick={openCreateModelForm}
            disabled={loading || providerOptions.length === 0}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Create model
          </button>
        </div>
      </div>

      {showRoutewiseSettings && isRoutewise && (
        <RoutewiseSettingsPanel
          modelId={selectedModel}
          endpoints={selectedRoutes.map((route) => ({
            endpointId: route.endpoint_id,
            label: `${route.route_type} · ${route.endpoint_id}`,
          }))}
        />
      )}

      {loading && routes.length === 0 ? (
        <div className="flex justify-center py-24">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : selectedRoutes.length === 0 ? (
        <div className="py-24 text-center">
          <p className="text-[13px] text-gray-400">No provider routes available.</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-gray-200 bg-white text-[13px]">
          <div className="hidden grid-cols-[minmax(0,1.1fr)_minmax(0,1.6fr)_minmax(0,.95fr)_minmax(176px,.9fr)_minmax(86px,.6fr)_minmax(96px,.6fr)] gap-3 rounded-t-lg bg-gray-50 px-4 py-2 text-[12px] font-semibold uppercase tracking-wide text-gray-500 lg:grid">
            <div>Candidate</div>
            <div>Target</div>
            <div>API key</div>
            <div>{routeLimitHeading(isRoutewise)}</div>
            <div>Status</div>
            <div className="text-right">Actions</div>
          </div>
          <div className="divide-y divide-gray-100">
            {selectedRoutes.map((route) => {
              const key = routeKey(route);
              const isEditing = editingRoute ? routeKey(editingRoute) === key : false;
              const targetProvider = routePrimaryProvider(route);
              const targetUpstreamProvider = routePrimaryUpstreamProvider(route);
              const targetOpenRouterProvider = routeOpenRouterProvider(route);
              const weightKey = routeWeightKey({
                model_id: route.model_id,
                endpoint_id: route.endpoint_id,
              });
              const routeWeight = routeWeightByKey.get(weightKey);
              const yamlWeight = routeWeight?.yaml_weight ?? route.yaml_weight;
              const effectiveWeight = routeWeight?.effective_weight ?? route.effective_weight;
              const draftWeight = draftWeights[weightKey] ?? formatWeight(effectiveWeight);
              const parsedDraftWeight = parseWeightDraft(draftWeight);
              const weightDirty =
                parsedDraftWeight !== null && parsedDraftWeight !== effectiveWeight;
              const isSavingWeight = savingWeightKey === weightKey;
              const hasWeightOverride = routeWeight?.override_weight != null;
              const editableConcurrencyLimit = canEditConcurrencyLimit(route);
              const draftConcurrencyLimit =
                draftConcurrencyLimits[key] ?? String(route.concurrency_limit ?? '');
              const trimmedConcurrencyLimit = draftConcurrencyLimit.trim();
              const parsedDraftConcurrencyLimit = /^\d+$/.test(trimmedConcurrencyLimit)
                ? Number.parseInt(trimmedConcurrencyLimit, 10)
                : null;
              const concurrencyLimitDirty =
                parsedDraftConcurrencyLimit !== null &&
                parsedDraftConcurrencyLimit !== route.concurrency_limit;
              const concurrencyLimitValid =
                parsedDraftConcurrencyLimit !== null && parsedDraftConcurrencyLimit > 0;
              const isSavingConcurrencyLimit = savingConcurrencyKey === key;
              return (
                <div
                  key={key}
                  className={`grid gap-3 px-4 py-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.6fr)_minmax(0,.95fr)_minmax(176px,.9fr)_minmax(86px,.6fr)_minmax(96px,.6fr)] ${
                    isEditing ? 'bg-gray-50' : 'bg-white'
                  }`}
                >
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      Candidate
                    </div>
                    <div className="font-medium text-gray-900">{route.route_type}</div>
                    <div className="mt-1 break-all font-mono text-[11px] leading-5 text-gray-400">
                      {route.endpoint_id}
                    </div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      Target
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-medium text-gray-800">
                      <span>{providerLabel(providerSelectBaseOptions, targetProvider)}</span>
                      {targetProvider !== targetUpstreamProvider && (
                        <>
                          <span className="text-gray-300">→</span>
                          <span>
                            {providerLabel(providerSelectBaseOptions, targetUpstreamProvider)}
                          </span>
                        </>
                      )}
                    </div>
                    {targetUpstreamProvider === 'openrouter' && targetOpenRouterProvider && (
                      <div className="mt-1 text-[11px] leading-5 text-gray-500">
                        OpenRouter provider:{' '}
                        {openRouterProviderLabel(openRouterSelectOptions, targetOpenRouterProvider)}
                      </div>
                    )}
                    {targetUpstreamProvider === 'openrouter' && !targetOpenRouterProvider && (
                      <div className="mt-1 text-[11px] leading-5 text-gray-500">
                        OpenRouter routing:{' '}
                        {route.openrouter_sort ? `Sort by ${route.openrouter_sort}` : 'Auto'}
                      </div>
                    )}
                    <div className="mt-1 break-all text-[11px] leading-5 text-gray-400">
                      {route.base_url}
                    </div>
                    <div className="mt-1 break-all font-mono text-[12px] leading-5 text-gray-600">
                      {route.provider_model_id ?? '—'}
                    </div>
                  </div>
                  <div className="min-w-0 text-gray-600">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      API key
                    </div>
                    <div className="break-words leading-5">{keyLabel(route)}</div>
                  </div>
                  <div className="text-gray-600">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      {routeLimitHeading(isRoutewise)}
                    </div>
                    {isRoutewise ? (
                      editableConcurrencyLimit ? (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <input
                            aria-label={`Concurrency limit for ${route.endpoint_id}`}
                            className="h-8 w-20 rounded-md border border-gray-300 px-2 text-right text-[13px] text-gray-900"
                            disabled={isSavingConcurrencyLimit}
                            min={1}
                            step={1}
                            type="number"
                            value={draftConcurrencyLimit}
                            onChange={(event) =>
                              setDraftConcurrencyLimits((current) => ({
                                ...current,
                                [key]: event.target.value,
                              }))
                            }
                          />
                          <button
                            aria-label={`Save ${route.endpoint_id} concurrency limit`}
                            type="button"
                            disabled={
                              isSavingConcurrencyLimit ||
                              !concurrencyLimitDirty ||
                              !concurrencyLimitValid
                            }
                            onClick={() => void onSaveConcurrencyLimit(route)}
                            className="h-8 rounded-md bg-gray-900 px-2.5 text-[12px] font-medium text-white disabled:opacity-50"
                          >
                            Save
                          </button>
                        </div>
                      ) : (
                        <div>{routeLimitLabel(route, isRoutewise)}</div>
                      )
                    ) : (
                      <div className="space-y-2">
                        <div className="text-[11px] leading-4 text-gray-400">
                          YAML {formatWeight(yamlWeight)} · Effective{' '}
                          {formatWeight(effectiveWeight)}
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <input
                            aria-label={`Runtime weight for ${route.endpoint_id}`}
                            className="h-8 w-20 rounded-md border border-gray-300 px-2 text-right text-[13px] text-gray-900"
                            disabled={isSavingWeight}
                            min={0}
                            step={0.1}
                            type="number"
                            value={draftWeight}
                            onChange={(event) =>
                              setDraftWeights((current) => ({
                                ...current,
                                [weightKey]: event.target.value,
                              }))
                            }
                          />
                          <button
                            aria-label={`Save ${route.endpoint_id} weight`}
                            type="button"
                            disabled={isSavingWeight || !weightDirty}
                            onClick={() => void onSaveWeight(route)}
                            className="h-8 rounded-md bg-gray-900 px-2.5 text-[12px] font-medium text-white disabled:opacity-50"
                          >
                            Save
                          </button>
                          {hasWeightOverride ? (
                            <button
                              aria-label={`Clear ${route.endpoint_id} weight override`}
                              type="button"
                              disabled={isSavingWeight}
                              onClick={() => void onClearWeight(route)}
                              className="h-8 rounded-md border border-gray-300 px-2 text-[12px] font-medium text-gray-600 disabled:opacity-50"
                            >
                              Reset
                            </button>
                          ) : null}
                        </div>
                      </div>
                    )}
                  </div>
                  <div>
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      Status
                    </div>
                    <span
                      className={
                        route.source === 'override'
                          ? 'inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700'
                          : route.source === 'runtime'
                            ? 'inline-flex rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-700'
                            : 'inline-flex rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                      }
                    >
                      {sourceLabel(route)}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-start justify-end gap-1 justify-self-end">
                    {route.source === 'override' && (
                      <button
                        type="button"
                        onClick={() => void onResetYaml(route)}
                        disabled={resettingKey === key}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                      >
                        {resettingKey === key ? 'Resetting...' : 'Reset config'}
                      </button>
                    )}
                    {route.source === 'runtime' ? (
                      <button
                        type="button"
                        onClick={() => void onDeleteRuntimeRoute(route)}
                        disabled={deletingKey === key}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
                      >
                        {deletingKey === key ? 'Deleting…' : 'Delete'}
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => {
                          setEditingRoute(route);
                          setAddingRoute(false);
                          setCreatingModel(false);
                        }}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
                      >
                        Edit
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {createFormOpen && (
        <form onSubmit={onCreateSubmit} className="rounded-lg border border-gray-200 bg-white p-4">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h3 className="text-[14px] font-semibold text-gray-900">
                {creatingModel ? 'Create model' : 'Add provider route'}
              </h3>
              <p className="mt-1 font-mono text-[12px] text-gray-400">
                {creatingModel ? 'New runtime model' : selectedModel}
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                setAddingRoute(false);
                setCreatingModel(false);
              }}
              className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100"
            >
              Close
            </button>
          </div>

          {creatingModel && (
            <div className="mb-3 grid gap-3 sm:grid-cols-3">
              <div>
                <label className="text-[12px] font-medium text-gray-500" htmlFor="new-model-id">
                  Model ID
                </label>
                <input
                  id="new-model-id"
                  type="text"
                  value={newModelId}
                  onChange={(event) => {
                    setNewModelId(event.target.value);
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                  placeholder="deepseek-v4-flash"
                  required
                />
                {!newModelIdAvailable && (
                  <p className="mt-1 text-[11px] leading-5 text-red-600">
                    A model with this ID already exists.
                  </p>
                )}
              </div>
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-strategy"
                >
                  Initial routing policy
                </label>
                <select
                  id="new-model-strategy"
                  value={newModelStrategy}
                  onChange={(event) => {
                    setNewModelStrategy(event.target.value as ProviderRouteStrategy);
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  <option value="fixed">fixed</option>
                  <option value="routewise">routewise</option>
                </select>
              </div>
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-required-role"
                >
                  Visibility
                </label>
                <select
                  id="new-model-required-role"
                  value={newModelRequiredRole}
                  onChange={(event) => {
                    setNewModelRequiredRole(event.target.value as Role);
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  {MODEL_ROLE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {creatingModel && (
            <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-pricing-prompt"
                >
                  Prompt $/M
                </label>
                <input
                  id="new-model-pricing-prompt"
                  type="number"
                  min="0"
                  step="0.000001"
                  value={createForm.pricingPrompt}
                  onChange={(event) => {
                    setCreateForm((current) => ({
                      ...current,
                      pricingPrompt: event.target.value,
                    }));
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-pricing-completion"
                >
                  Completion $/M
                </label>
                <input
                  id="new-model-pricing-completion"
                  type="number"
                  min="0"
                  step="0.000001"
                  value={createForm.pricingCompletion}
                  onChange={(event) => {
                    setCreateForm((current) => ({
                      ...current,
                      pricingCompletion: event.target.value,
                    }));
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-pricing-cache-read"
                >
                  Cache read $/M
                </label>
                <input
                  id="new-model-pricing-cache-read"
                  type="number"
                  min="0"
                  step="0.000001"
                  value={createForm.pricingCacheReads}
                  onChange={(event) => {
                    setCreateForm((current) => ({
                      ...current,
                      pricingCacheReads: event.target.value,
                    }));
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                />
              </div>
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-model-pricing-cache-write"
                >
                  Cache write $/M
                </label>
                <input
                  id="new-model-pricing-cache-write"
                  type="number"
                  min="0"
                  step="0.000001"
                  value={createForm.pricingCacheWrites}
                  onChange={(event) => {
                    setCreateForm((current) => ({
                      ...current,
                      pricingCacheWrites: event.target.value,
                    }));
                    setVerifiedCreateSignature(null);
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                />
              </div>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-type">
                Route type
              </label>
              <select
                id="new-route-type"
                value={createForm.routeType}
                onChange={(event) =>
                  onCreateRouteTypeChange(event.target.value as ProviderRouteType)
                }
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
              >
                {createRouteTypeOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-provider">
                Provider
              </label>
              <select
                id="new-route-provider"
                value={createForm.upstreamProvider}
                onChange={(event) => onCreateProviderChange(event.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                required
              >
                {createProviderOptions.length === 0 && (
                  <option value="">{noProviderOptionLabel(createForm.routeType)}</option>
                )}
                {createProviderOptions.map((option) => (
                  <option key={option.provider} value={option.provider}>
                    {option.label}
                  </option>
                ))}
              </select>
              {createProviderOptions.length === 0 && (
                <p className="mt-1 text-[11px] leading-5 text-gray-400">
                  {noProviderHelpText(createForm.routeType)}
                </p>
              )}
            </div>
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-api-key">
                API key
              </label>
              <select
                id="new-route-api-key"
                value={createForm.apiKeyId}
                onChange={(event) =>
                  setCreateForm((current) => ({ ...current, apiKeyId: event.target.value }))
                }
                disabled={createKeysLoading}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none disabled:opacity-50"
              >
                <option value="">{defaultKeyOptionLabel(createKeyProvider)}</option>
                {createKeyOptions.map((key) => (
                  <option key={key.id ?? key.key_prefix} value={key.id ?? ''}>
                    {key.label ? `${key.label} · ` : ''}
                    {key.key_prefix} ({key.source})
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-url">
                Base URL
              </label>
              <input
                id="new-route-url"
                type="url"
                value={createForm.baseUrl}
                onChange={(event) =>
                  setCreateForm((current) => ({ ...current, baseUrl: event.target.value }))
                }
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-model-id">
                Provider model ID
              </label>
              <input
                id="new-route-model-id"
                type="text"
                value={createForm.providerModelId}
                onChange={(event) =>
                  setCreateForm((current) => ({
                    ...current,
                    providerModelId: event.target.value,
                  }))
                }
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>
          </div>

          {createForm.upstreamProvider === 'openrouter' && (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <div className="flex items-center gap-1.5">
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-openrouter-routing"
                  >
                    OpenRouter routing
                  </label>
                  {openRouterProvidersLoading && (
                    <span className="h-3 w-3 animate-spin rounded-full border border-gray-200 border-t-gray-700" />
                  )}
                </div>
                <select
                  id="new-route-openrouter-routing"
                  value={openRouterRoutingValue(
                    createForm.openRouterProvider,
                    createForm.openRouterSort,
                  )}
                  onChange={(event) => {
                    const routingValue = event.target.value;
                    const openRouterProvider = openRouterProviderFromRoutingValue(routingValue);
                    setCreateForm((current) => ({
                      ...current,
                      openRouterProvider,
                      openRouterSort: openRouterSortFromRoutingValue(routingValue),
                      customOpenRouterProvider:
                        openRouterProvider === OPENROUTER_PROVIDER_CUSTOM
                          ? current.customOpenRouterProvider
                          : '',
                    }));
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  {createOpenRouterRoutingOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
              {createForm.openRouterProvider === OPENROUTER_PROVIDER_CUSTOM && (
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-custom-openrouter-provider"
                  >
                    Custom OpenRouter provider
                  </label>
                  <input
                    id="new-route-custom-openrouter-provider"
                    type="text"
                    value={createForm.customOpenRouterProvider}
                    onChange={(event) =>
                      setCreateForm((current) => ({
                        ...current,
                        customOpenRouterProvider: event.target.value,
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                    placeholder="provider-slug"
                    required
                  />
                </div>
              )}
            </div>
          )}

          {showCreateLimits && (
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              {createForm.routeType === 'quota' && (
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-quota"
                  >
                    Local daily quota
                  </label>
                  <input
                    id="new-route-quota"
                    type="number"
                    min={1}
                    step={1}
                    value={createForm.quotaLimit}
                    onChange={(event) =>
                      setCreateForm((current) => ({ ...current, quotaLimit: event.target.value }))
                    }
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                    required
                  />
                </div>
              )}
              {createForm.routeType === 'concurrency' && (
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-concurrency"
                  >
                    Concurrency limit
                  </label>
                  <input
                    id="new-route-concurrency"
                    type="number"
                    min={1}
                    step={1}
                    value={createForm.concurrencyLimit}
                    onChange={(event) =>
                      setCreateForm((current) => ({
                        ...current,
                        concurrencyLimit: event.target.value,
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                    required
                  />
                </div>
              )}
              {showCreateWeight && (
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-weight"
                  >
                    Fixed weight
                  </label>
                  <input
                    id="new-route-weight"
                    type="number"
                    min={0.001}
                    step={0.001}
                    value={createForm.weight}
                    onChange={(event) =>
                      setCreateForm((current) => ({ ...current, weight: event.target.value }))
                    }
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                    required
                  />
                </div>
              )}
            </div>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onCreateVerify}
              disabled={verifyingCreateRoute || creatingRoute || !createFormValid}
              className={
                createRouteVerified
                  ? 'rounded-md border border-emerald-600 bg-emerald-600 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40'
                  : 'rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40'
              }
            >
              {verifyingCreateRoute ? 'Verifying…' : createRouteVerified ? 'Verified' : 'Verify'}
            </button>
            <button
              type="submit"
              disabled={creatingRoute || !createFormValid}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {creatingRoute
                ? creatingModel
                  ? 'Creating…'
                  : 'Adding…'
                : creatingModel
                  ? 'Create'
                  : 'Add'}
            </button>
          </div>
        </form>
      )}

      {editingRoute && (
        <form onSubmit={onSubmit} className="rounded-lg border border-gray-200 bg-white p-4">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h3 className="text-[14px] font-semibold text-gray-900">Edit provider route</h3>
              <p className="mt-1 font-mono text-[12px] text-gray-400">
                {editingRoute.model_id} / {editingRoute.provider} / {editingRoute.route_type}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setEditingRoute(null)}
              className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100"
            >
              Close
            </button>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="route-provider">
                Override provider
              </label>
              <select
                id="route-provider"
                value={form.upstreamProvider}
                onChange={(event) => onUpstreamProviderChange(event.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                required
              >
                {providerSelectOptions.map((option) => (
                  <option key={option.provider} value={option.provider}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="route-api-key">
                API key
              </label>
              <select
                id="route-api-key"
                value={form.apiKeyId}
                onChange={(event) =>
                  setForm((current) => ({ ...current, apiKeyId: event.target.value }))
                }
                disabled={keysLoading}
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none disabled:opacity-50"
              >
                <option value="">{defaultKeyOptionLabel(keyProvider)}</option>
                {keyOptions.map((key) => (
                  <option key={key.id ?? key.key_prefix} value={key.id ?? ''}>
                    {key.label ? `${key.label} · ` : ''}
                    {key.key_prefix} ({key.source})
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-3">
            <label className="text-[12px] font-medium text-gray-500" htmlFor="route-base-url">
              Base URL
            </label>
            <input
              id="route-base-url"
              type="url"
              value={form.baseUrl}
              onChange={(event) =>
                setForm((current) => ({ ...current, baseUrl: event.target.value }))
              }
              className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
              required
            />
          </div>

          <div className="mt-3">
            <label
              className="text-[12px] font-medium text-gray-500"
              htmlFor="route-provider-model-id"
            >
              Provider model ID
            </label>
            <input
              id="route-provider-model-id"
              type="text"
              value={form.providerModelId}
              onChange={(event) =>
                setForm((current) => ({ ...current, providerModelId: event.target.value }))
              }
              className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
              required
            />
          </div>

          {form.upstreamProvider === 'openrouter' && (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <div className="flex items-center gap-1.5">
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="route-openrouter-routing"
                  >
                    OpenRouter routing
                  </label>
                  {openRouterProvidersLoading && (
                    <span className="h-3 w-3 animate-spin rounded-full border border-gray-200 border-t-gray-700" />
                  )}
                </div>
                <select
                  id="route-openrouter-routing"
                  value={openRouterRoutingValue(form.openRouterProvider, form.openRouterSort)}
                  onChange={(event) => {
                    const routingValue = event.target.value;
                    const openRouterProvider = openRouterProviderFromRoutingValue(routingValue);
                    setForm((current) => ({
                      ...current,
                      openRouterProvider,
                      openRouterSort: openRouterSortFromRoutingValue(routingValue),
                      customOpenRouterProvider:
                        openRouterProvider === OPENROUTER_PROVIDER_CUSTOM
                          ? current.customOpenRouterProvider
                          : '',
                    }));
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  {editOpenRouterRoutingOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
              {form.openRouterProvider === OPENROUTER_PROVIDER_CUSTOM && (
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="route-custom-openrouter-provider"
                  >
                    Custom OpenRouter provider
                  </label>
                  <input
                    id="route-custom-openrouter-provider"
                    type="text"
                    value={form.customOpenRouterProvider}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        customOpenRouterProvider: event.target.value,
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 font-mono text-[13px] focus:border-gray-400 focus:outline-none"
                    placeholder="provider-slug"
                    required
                  />
                </div>
              )}
            </div>
          )}

          {editsLocalQuota && (
            <div className="mt-3">
              <label className="text-[12px] font-medium text-gray-500" htmlFor="route-quota-limit">
                Local daily quota
              </label>
              <input
                id="route-quota-limit"
                type="number"
                min={1}
                step={1}
                value={form.quotaLimit}
                onChange={(event) =>
                  setForm((current) => ({ ...current, quotaLimit: event.target.value }))
                }
                className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                required
              />
            </div>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onVerify}
              disabled={
                verifyingKey === routeKey(editingRoute) ||
                savingKey === routeKey(editingRoute) ||
                !editFormValid
              }
              className={
                editRouteVerified
                  ? 'rounded-md border border-emerald-600 bg-emerald-600 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40'
                  : 'rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40'
              }
            >
              {verifyingKey === routeKey(editingRoute)
                ? 'Verifying…'
                : editRouteVerified
                  ? 'Verified'
                  : 'Verify'}
            </button>
            <button
              type="submit"
              disabled={savingKey === routeKey(editingRoute) || !editFormValid}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {savingKey === routeKey(editingRoute) ? 'Applying…' : 'Apply'}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

export default ProviderRoutesTab;
