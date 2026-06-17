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
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listProviderKeys,
  listOpenRouterProviderOptions,
  listProviderRoutes,
  updateProviderRoute,
  updateProviderRouteStrategy,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

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
};

const OPENROUTER_PROVIDER_AUTO = '';
const OPENROUTER_PROVIDER_CUSTOM = '__custom__';
const OPENROUTER_AUTO_OPTION: OpenRouterProviderOption = {
  provider: OPENROUTER_PROVIDER_AUTO,
  label: 'Auto',
};
const OPENROUTER_CUSTOM_OPTION: OpenRouterProviderOption = {
  provider: OPENROUTER_PROVIDER_CUSTOM,
  label: 'Custom...',
};
const OPENROUTER_PROVIDER_SLUG_RE = /^[A-Za-z0-9_.-]+$/;
const OPENROUTER_SORT_OPTIONS: Array<{ value: '' | OpenRouterSortPolicy; label: string }> = [
  { value: '', label: 'Auto' },
  { value: 'price', label: 'Sort by price' },
  { value: 'throughput', label: 'Sort by throughput' },
  { value: 'latency', label: 'Sort by latency' },
];

function routeKey(route: Pick<ProviderRoute, 'model_id' | 'route_id'>) {
  return `${route.model_id}\u0000${route.route_id}`;
}

function formatWeight(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

function keyLabel(route: ProviderRoute) {
  if (route.api_key.source === 'default') return `Default ${route.api_key.provider} pool`;
  const label = route.api_key.label ? `${route.api_key.label} · ` : '';
  return `${label}${route.api_key.key_prefix ?? route.api_key.source}`;
}

function sourceLabel(route: ProviderRoute) {
  if (route.source === 'override') return 'Override active';
  if (route.source === 'runtime') return 'Runtime added';
  return 'Config default';
}

function routeLimitLabel(route: ProviderRoute, isRoutewise: boolean) {
  if (route.quota_limit) return route.quota_limit.toLocaleString();
  if (!isRoutewise) {
    return `${formatWeight(route.effective_weight)} / ${formatWeight(route.yaml_weight)}`;
  }
  return '—';
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
  if (option.provider === 'openrouter') return routeType === 'on_demand';
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

function openRouterProviderLabel(
  options: OpenRouterProviderOption[],
  provider: string,
) {
  return options.find((option) => option.provider === provider)?.label ?? provider;
}

function ensureOpenRouterProviderOption(
  options: OpenRouterProviderOption[],
  provider: string,
) {
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

function resolvedOpenRouterProvider(selectedProvider: string, customProvider: string) {
  if (selectedProvider === OPENROUTER_PROVIDER_CUSTOM) {
    const cleaned = customProvider.trim();
    return cleaned || null;
  }
  return selectedProvider || null;
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
  if (routeType !== 'on_demand') return [];
  const usedPins = new Set(
    routes
      .filter(
        (route) =>
          route.route_type === routeType && routePrimaryUpstreamProvider(route) === 'openrouter',
      )
      .map(routeOpenRouterProvider),
  );
  return allOptions.filter((option) => !usedPins.has(option.provider));
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
      (option.provider === 'openrouter'
        ? routeType === 'on_demand'
        : !usedProviders.has(option.provider)),
  );
}

export function ProviderRoutesTab() {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [providerOptions, setProviderOptions] = useState<ProviderRouteOption[]>([]);
  const [openRouterProviderOptions, setOpenRouterProviderOptions] = useState<
    OpenRouterProviderOption[]
  >([]);
  const [openRouterProvidersLoading, setOpenRouterProvidersLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(false);
  const [editingRoute, setEditingRoute] = useState<ProviderRoute | null>(null);
  const [addingRoute, setAddingRoute] = useState(false);
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
  const [restoringKey, setRestoringKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [savingStrategy, setSavingStrategy] = useState(false);

  const loadRoutes = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await listProviderRoutes();
      setRoutes(resp.routes);
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
  const providerSelectBaseOptions = useMemo(
    () => primaryProviderOptions(providerOptions),
    [providerOptions],
  );
  const openRouterSelectOptions = useMemo(
    () => openRouterProviderOptionsFor(providerOptions, openRouterProviderOptions),
    [openRouterProviderOptions, providerOptions],
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
    addingRoute && createForm.upstreamProvider === 'openrouter'
      ? createForm.providerModelId.trim()
      : editingRoute && form.upstreamProvider === 'openrouter'
        ? form.providerModelId.trim()
        : '';
  const strategy = selectedRoutes[0]?.strategy ?? 'fixed';
  const isRoutewise = strategy === 'routewise';
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
  const createWeightValid = Number.isFinite(parsedCreateWeight) && parsedCreateWeight > 0;
  const formOpenRouterProviderValid = customOpenRouterProviderValid(
    form.openRouterProvider,
    form.customOpenRouterProvider,
  );
  const createOpenRouterProviderValid = customOpenRouterProviderValid(
    createForm.openRouterProvider,
    createForm.customOpenRouterProvider,
  );
  const createFormValid =
    Boolean(selectedModel) &&
    Boolean(createForm.upstreamProvider) &&
    Boolean(createForm.baseUrl.trim()) &&
    Boolean(createForm.providerModelId.trim()) &&
    createOpenRouterProviderValid &&
    createQuotaValid &&
    createConcurrencyValid &&
    createWeightValid;

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
        selectedRoutes,
      ),
    [createForm.routeType, openRouterSelectOptions, selectedRoutes],
  );
  const createOpenRouterSelectOptions = useMemo(
    () => withCustomOpenRouterOption(createOpenRouterProviderOptions),
    [createOpenRouterProviderOptions],
  );

  const createProviderOptions = useMemo(
    () =>
      createProviderOptionsFor(
        providerSelectBaseOptions,
        createForm.routeType,
        selectedRoutes,
      ),
    [createForm.routeType, providerSelectBaseOptions, selectedRoutes],
  );

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
    if (!addingRoute || !createKeyProvider) return;
    void loadCreateKeys(createKeyProvider);
  }, [addingRoute, createKeyProvider, loadCreateKeys]);

  useEffect(() => {
    if (!activeOpenRouterProviderModelId.includes('/')) return undefined;
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      setOpenRouterProvidersLoading(true);
      void listOpenRouterProviderOptions(activeOpenRouterProviderModelId)
        .then((resp) => {
          if (cancelled) return;
          setOpenRouterProviderOptions(resp.providers);
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
    if (!addingRoute) return;
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
    addingRoute,
    createForm.upstreamProvider,
    createOpenRouterProviderOptions,
    createProviderOptions,
  ]);

  const updateRoute = useCallback((updated: ProviderRoute) => {
    setRoutes((current) =>
      current.map((route) => (routeKey(route) === routeKey(updated) ? updated : route)),
    );
  }, []);

  const replaceRoute = useCallback((updated: ProviderRoute) => {
    updateRoute(updated);
    setEditingRoute(updated);
  }, [updateRoute]);

  const replaceModelRoutes = useCallback((modelId: string, updated: ProviderRoute[]) => {
    setRoutes((current) => [
      ...current.filter((route) => route.model_id !== modelId),
      ...updated,
    ]);
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
          : defaultProviderModelIdFor(upstreamProvider, selectedRoutes) ||
            current.providerModelId,
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
    setAddingRoute(true);
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
        defaultProviderModelIdFor(upstreamProvider, selectedRoutes) ||
        current.providerModelId,
    }));
  };

  const onCreateRouteTypeChange = (routeType: ProviderRouteType) => {
    const nextOpenRouterOptions = openRouterProviderOptionsForCreate(
      openRouterSelectOptions,
      routeType,
      selectedRoutes,
    );
    const nextProvider = createProviderOptionsFor(
      providerSelectBaseOptions,
      routeType,
      selectedRoutes,
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
        ? defaultProviderModelIdFor(nextProvider.provider, selectedRoutes)
        : current.providerModelId,
    }));
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (
      !editingRoute ||
      !form.upstreamProvider ||
      !form.baseUrl.trim() ||
      !form.providerModelId.trim() ||
      !formOpenRouterProviderValid ||
      !quotaLimitValid
    ) {
      return;
    }
    const key = routeKey(editingRoute);
    setSavingKey(key);
    try {
      const updated = await updateProviderRoute(editingRoute.model_id, editingRoute.route_id, {
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
      });
      replaceRoute(updated);
      toast.success('Provider route updated');
    } catch (err) {
      toast.error(`Update failed: ${getErrorMessage(err)}`);
    } finally {
      setSavingKey(null);
    }
  };

  const onCreateSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!createFormValid) return;
    setCreatingRoute(true);
    try {
      const created = await createProviderRouteCandidate(selectedModel, {
        route_type: createForm.routeType,
        upstream_provider: createForm.upstreamProvider,
        openrouter_provider:
          createForm.upstreamProvider === 'openrouter'
            ? selectedCreateOpenRouterProvider
            : null,
        openrouter_sort: openRouterSortForPayload(
          createForm.upstreamProvider,
          createForm.openRouterProvider,
          createForm.openRouterSort,
        ),
        base_url: createForm.baseUrl.trim(),
        api_key_id: createForm.apiKeyId || null,
        provider_model_id: createForm.providerModelId.trim(),
        quota_limit:
          createForm.routeType === 'quota' ? parsedCreateQuotaLimit : null,
        concurrency_limit:
          createForm.routeType === 'concurrency' ? parsedCreateConcurrencyLimit : null,
        weight: parsedCreateWeight,
      });
      setRoutes((current) => [...current, created]);
      setAddingRoute(false);
      toast.success('Provider route added');
    } catch (err) {
      toast.error(`Add failed: ${getErrorMessage(err)}`);
    } finally {
      setCreatingRoute(false);
    }
  };

  const onRestoreYaml = async (route: ProviderRoute) => {
    const key = routeKey(route);
    setRestoringKey(key);
    try {
      const updated = await deleteProviderRoute(route.model_id, route.route_id);
      updateRoute(updated);
      setEditingRoute((current) =>
        current && routeKey(current) === key ? null : current,
      );
      toast.success('Restored config route');
    } catch (err) {
      toast.error(`Restore failed: ${getErrorMessage(err)}`);
    } finally {
      setRestoringKey(null);
    }
  };

  const onDeleteRuntimeRoute = async (route: ProviderRoute) => {
    const key = routeKey(route);
    setDeletingKey(key);
    try {
      const updated = await deleteProviderRouteCandidate(route.model_id, route.route_id);
      replaceModelRoutes(updated.model_id ?? route.model_id, updated.routes);
      setEditingRoute((current) =>
        current && routeKey(current) === key ? null : current,
      );
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
            onChange={(event) =>
              void onStrategyChange(event.target.value as ProviderRouteStrategy)
            }
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
            onClick={() => loadRoutes()}
            disabled={loading}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </div>

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
          <div className="hidden grid-cols-[minmax(0,1.1fr)_minmax(0,1.8fr)_minmax(0,.95fr)_minmax(56px,.45fr)_minmax(86px,.65fr)_minmax(96px,.65fr)] gap-3 rounded-t-lg bg-gray-50 px-4 py-2 text-[12px] font-semibold uppercase tracking-wide text-gray-500 lg:grid">
            <div>Candidate</div>
            <div>Target</div>
            <div>API key</div>
            <div>{isRoutewise ? 'Quota' : 'Weight'}</div>
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
              return (
                <div
                  key={key}
                  className={`grid gap-3 px-4 py-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1.8fr)_minmax(0,.95fr)_minmax(56px,.45fr)_minmax(86px,.65fr)_minmax(96px,.65fr)] ${
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
                        {openRouterProviderLabel(
                          openRouterSelectOptions,
                          targetOpenRouterProvider,
                        )}
                      </div>
                    )}
                    {targetUpstreamProvider === 'openrouter' && !targetOpenRouterProvider && (
                      <div className="mt-1 text-[11px] leading-5 text-gray-500">
                        OpenRouter policy:{' '}
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
                      {isRoutewise ? 'Quota' : 'Weight'}
                    </div>
                    <div>{routeLimitLabel(route, isRoutewise)}</div>
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
                        onClick={() => void onRestoreYaml(route)}
                        disabled={restoringKey === key}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                      >
                        {restoringKey === key ? 'Restoring…' : 'Restore config'}
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
                        onClick={() => setEditingRoute(route)}
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

      {addingRoute && (
        <form onSubmit={onCreateSubmit} className="rounded-lg border border-gray-200 bg-white p-4">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h3 className="text-[14px] font-semibold text-gray-900">Add provider route</h3>
              <p className="mt-1 font-mono text-[12px] text-gray-400">{selectedModel}</p>
            </div>
            <button
              type="button"
              onClick={() => setAddingRoute(false)}
              className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100"
            >
              Close
            </button>
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
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
                <option value="on_demand">on_demand</option>
                <option value="quota">quota</option>
                <option value="concurrency">concurrency</option>
              </select>
            </div>
            <div>
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="new-route-provider"
              >
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
                  <option value="">No providers available</option>
                )}
                {createProviderOptions.map((option) => (
                  <option key={option.provider} value={option.provider}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="new-route-api-key"
              >
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
                <option value="">Default {createKeyProvider} pool</option>
                {createKeyOptions.map((key) => (
                  <option key={key.id ?? key.key_prefix} value={key.id ?? ''}>
                    {key.label ? `${key.label} · ` : ''}
                    {key.key_prefix} ({key.source})
                  </option>
                ))}
              </select>
            </div>
            {createForm.upstreamProvider === 'openrouter' && (
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="new-route-openrouter-sort"
                >
                  OpenRouter policy
                </label>
                <select
                  id="new-route-openrouter-sort"
                  value={createForm.openRouterSort}
                  onChange={(event) =>
                    setCreateForm((current) => ({
                      ...current,
                      openRouterSort: event.target.value,
                    }))
                  }
                  disabled={createForm.openRouterProvider !== OPENROUTER_PROVIDER_AUTO}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none disabled:opacity-50"
                >
                  {OPENROUTER_SORT_OPTIONS.map((option) => (
                    <option key={option.value || 'default'} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
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
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="new-route-model-id"
              >
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
                    htmlFor="new-route-openrouter-provider"
                  >
                    OpenRouter provider
                  </label>
                  {openRouterProvidersLoading && (
                    <span className="h-3 w-3 animate-spin rounded-full border border-gray-200 border-t-gray-700" />
                  )}
                </div>
                <select
                  id="new-route-openrouter-provider"
                  value={createForm.openRouterProvider}
                  onChange={(event) => {
                    const openRouterProvider = event.target.value;
                    setCreateForm((current) => ({
                      ...current,
                      openRouterProvider,
                      openRouterSort:
                        openRouterProvider === OPENROUTER_PROVIDER_AUTO
                          ? current.openRouterSort
                          : '',
                    }));
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  {createOpenRouterSelectOptions.map((option) => (
                    <option key={option.provider || 'auto'} value={option.provider}>
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
            <div>
              <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-weight">
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
          </div>

          <div className="mt-4 flex justify-end">
            <button
              type="submit"
              disabled={creatingRoute || !createFormValid}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {creatingRoute ? 'Verifying…' : 'Verify & Add'}
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
                <option value="">Default {keyProvider} pool</option>
                {keyOptions.map((key) => (
                  <option key={key.id ?? key.key_prefix} value={key.id ?? ''}>
                    {key.label ? `${key.label} · ` : ''}
                    {key.key_prefix} ({key.source})
                  </option>
                ))}
              </select>
            </div>
            {form.upstreamProvider === 'openrouter' && (
              <div>
                <label
                  className="text-[12px] font-medium text-gray-500"
                  htmlFor="route-openrouter-sort"
                >
                  OpenRouter policy
                </label>
                <select
                  id="route-openrouter-sort"
                  value={form.openRouterSort}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      openRouterSort: event.target.value,
                    }))
                  }
                  disabled={form.openRouterProvider !== OPENROUTER_PROVIDER_AUTO}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none disabled:opacity-50"
                >
                  {OPENROUTER_SORT_OPTIONS.map((option) => (
                    <option key={option.value || 'default'} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
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
                    htmlFor="route-openrouter-provider"
                  >
                    OpenRouter provider
                  </label>
                  {openRouterProvidersLoading && (
                    <span className="h-3 w-3 animate-spin rounded-full border border-gray-200 border-t-gray-700" />
                  )}
                </div>
                <select
                  id="route-openrouter-provider"
                  value={form.openRouterProvider}
                  onChange={(event) => {
                    const openRouterProvider = event.target.value;
                    setForm((current) => ({
                      ...current,
                      openRouterProvider,
                      openRouterSort:
                        openRouterProvider === OPENROUTER_PROVIDER_AUTO
                          ? current.openRouterSort
                          : '',
                    }));
                  }}
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                >
                  {editOpenRouterSelectOptions.map((option) => (
                    <option key={option.provider || 'auto'} value={option.provider}>
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
              <label
                className="text-[12px] font-medium text-gray-500"
                htmlFor="route-quota-limit"
              >
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

          <div className="mt-4 flex justify-end">
            <button
              type="submit"
              disabled={
                savingKey === routeKey(editingRoute) ||
                !form.upstreamProvider ||
                !form.baseUrl.trim() ||
                !form.providerModelId.trim() ||
                !formOpenRouterProviderValid ||
                !quotaLimitValid
              }
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {savingKey === routeKey(editingRoute) ? 'Verifying…' : 'Verify & Apply'}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

export default ProviderRoutesTab;
