'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
  ProviderApiKeyItem,
  ProviderRoute,
  ProviderRouteStrategy,
  ProviderRouteType,
  ProviderRouteOption,
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listProviderKeys,
  listProviderRoutes,
  updateProviderRoute,
  updateProviderRouteStrategy,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

type RouteForm = {
  upstreamProvider: string;
  baseUrl: string;
  apiKeyId: string;
  providerModelId: string;
  quotaLimit: string;
};

type CreateRouteForm = {
  routeType: ProviderRouteType;
  upstreamProvider: string;
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
  baseUrl: '',
  apiKeyId: '',
  providerModelId: '',
  quotaLimit: '5000',
  concurrencyLimit: '1',
  weight: '1',
};

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

function optionSupportsRouteType(option: ProviderRouteOption, routeType: ProviderRouteType) {
  if (option.provider === 'chutes') return routeType === 'quota';
  if (option.provider === 'featherless') return routeType === 'concurrency';
  if (['deepinfra', 'openrouter', 'parasail'].includes(option.provider)) {
    return routeType === 'on_demand';
  }
  return routeType === 'on_demand';
}

function createProviderOptionsFor(
  providerOptions: ProviderRouteOption[],
  routeType: ProviderRouteType,
  routes: ProviderRoute[] = [],
) {
  const usedProviders = new Set(
    routes.filter((route) => route.route_type === routeType).map((route) => route.provider),
  );
  return providerOptions.filter(
    (option) => optionSupportsRouteType(option, routeType) && !usedProviders.has(option.provider),
  );
}

export function ProviderRoutesTab() {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [providerOptions, setProviderOptions] = useState<ProviderRouteOption[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(false);
  const [editingRoute, setEditingRoute] = useState<ProviderRoute | null>(null);
  const [addingRoute, setAddingRoute] = useState(false);
  const [form, setForm] = useState<RouteForm>({
    upstreamProvider: '',
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
  const strategy = selectedRoutes[0]?.strategy ?? 'fixed';
  const isRoutewise = strategy === 'routewise';
  const editsLocalQuota =
    editingRoute?.route_type === 'quota' && form.upstreamProvider !== editingRoute.provider;
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
  const createFormValid =
    Boolean(selectedModel) &&
    Boolean(createForm.upstreamProvider) &&
    Boolean(createForm.baseUrl.trim()) &&
    Boolean(createForm.providerModelId.trim()) &&
    createQuotaValid &&
    createConcurrencyValid &&
    createWeightValid;

  useEffect(() => {
    if (!editingRoute) {
      setForm({
        upstreamProvider: '',
        baseUrl: '',
        apiKeyId: '',
        providerModelId: '',
        quotaLimit: '',
      });
      setKeyOptions([]);
      return;
    }
    setForm({
      upstreamProvider: editingRoute.upstream_provider,
      baseUrl: editingRoute.base_url,
      apiKeyId: editingRoute.api_key_id ?? '',
      providerModelId: editingRoute.provider_model_id ?? '',
      quotaLimit: editingRoute.quota_limit ? String(editingRoute.quota_limit) : '',
    });
  }, [editingRoute]);

  const keyProvider = useMemo(() => {
    const provider = optionFor(providerOptions, form.upstreamProvider);
    return provider?.key_provider ?? form.upstreamProvider;
  }, [form.upstreamProvider, providerOptions]);

  const createKeyProvider = useMemo(() => {
    const provider = optionFor(providerOptions, createForm.upstreamProvider);
    return provider?.key_provider ?? createForm.upstreamProvider;
  }, [createForm.upstreamProvider, providerOptions]);

  const createProviderOptions = useMemo(
    () => createProviderOptionsFor(providerOptions, createForm.routeType, selectedRoutes),
    [createForm.routeType, providerOptions, selectedRoutes],
  );

  const providerSelectOptions = useMemo(() => {
    if (!form.upstreamProvider || optionFor(providerOptions, form.upstreamProvider)) {
      return providerOptions;
    }
    return [
      ...providerOptions,
      {
        provider: form.upstreamProvider,
        label: form.upstreamProvider,
        kind: form.upstreamProvider,
        key_provider: form.upstreamProvider,
        default_base_url: form.baseUrl,
      },
    ];
  }, [form.baseUrl, form.upstreamProvider, providerOptions]);

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
    if (!addingRoute) return;
    if (createProviderOptions.length === 0) {
      setCreateForm((current) => ({
        ...current,
        upstreamProvider: '',
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
      baseUrl: nextProvider.default_base_url,
      apiKeyId: '',
    }));
  }, [addingRoute, createForm.upstreamProvider, createProviderOptions]);

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
    const selected = optionFor(providerOptions, upstreamProvider);
    setForm((current) => ({
      upstreamProvider,
      baseUrl: selected?.default_base_url || current.baseUrl,
      apiKeyId: '',
      providerModelId:
        upstreamProvider === editingRoute?.upstream_provider
          ? (editingRoute.provider_model_id ?? current.providerModelId)
          : '',
      quotaLimit: current.quotaLimit,
    }));
  };

  const openAddForm = () => {
    const firstProvider = createProviderOptionsFor(
      providerOptions,
      emptyCreateForm.routeType,
      selectedRoutes,
    )[0];
    setCreateForm({
      ...emptyCreateForm,
      upstreamProvider: firstProvider?.provider ?? '',
      baseUrl: firstProvider?.default_base_url ?? '',
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
      baseUrl: selected?.default_base_url || current.baseUrl,
      apiKeyId: '',
    }));
  };

  const onCreateRouteTypeChange = (routeType: ProviderRouteType) => {
    const nextProvider = createProviderOptionsFor(providerOptions, routeType, selectedRoutes)[0];
    setCreateForm((current) => ({
      ...current,
      routeType,
      upstreamProvider: nextProvider?.provider ?? '',
      baseUrl: nextProvider?.default_base_url ?? '',
      apiKeyId: '',
    }));
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (
      !editingRoute ||
      !form.upstreamProvider ||
      !form.baseUrl.trim() ||
      !form.providerModelId.trim() ||
      !quotaLimitValid
    ) {
      return;
    }
    const key = routeKey(editingRoute);
    setSavingKey(key);
    try {
      const updated = await updateProviderRoute(editingRoute.model_id, editingRoute.route_id, {
        upstream_provider: form.upstreamProvider,
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
          <div className="hidden grid-cols-[minmax(145px,1.05fr)_minmax(210px,1.65fr)_minmax(125px,.9fr)_minmax(64px,.45fr)_minmax(104px,.6fr)_minmax(56px,max-content)] gap-3 rounded-t-lg bg-gray-50 px-4 py-2 text-[12px] font-semibold uppercase tracking-wide text-gray-500 lg:grid">
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
              return (
                <div
                  key={key}
                  className={`grid gap-3 px-4 py-4 lg:grid-cols-[minmax(145px,1.05fr)_minmax(210px,1.65fr)_minmax(125px,.9fr)_minmax(64px,.45fr)_minmax(104px,.6fr)_minmax(56px,max-content)] ${
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
                      <span>{route.provider}</span>
                      {route.provider !== route.upstream_provider && (
                        <>
                          <span className="text-gray-300">→</span>
                          <span>{route.upstream_provider}</span>
                        </>
                      )}
                    </div>
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

          <div className="grid gap-3 sm:grid-cols-3">
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

          <div className="grid gap-3 sm:grid-cols-2">
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
