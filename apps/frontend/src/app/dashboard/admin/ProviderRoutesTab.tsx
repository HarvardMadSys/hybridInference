'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
  ProviderApiKeyItem,
  ProviderRoute,
  ProviderRouteStrategy,
  ProviderRouteOption,
  deleteProviderRoute,
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
  return route.source === 'override' ? 'Override active' : 'Config default';
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

export function ProviderRoutesTab() {
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [providerOptions, setProviderOptions] = useState<ProviderRouteOption[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(false);
  const [editingRoute, setEditingRoute] = useState<ProviderRoute | null>(null);
  const [form, setForm] = useState<RouteForm>({
    upstreamProvider: '',
    baseUrl: '',
    apiKeyId: '',
    providerModelId: '',
    quotaLimit: '',
  });
  const [keyOptions, setKeyOptions] = useState<ProviderApiKeyItem[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [restoringKey, setRestoringKey] = useState<string | null>(null);
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

  useEffect(() => {
    if (!editingRoute || !keyProvider) return;
    void loadKeys(keyProvider);
  }, [editingRoute, keyProvider, loadKeys]);

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
        <div className="rounded-lg border border-gray-200 bg-white text-[13px]">
          <div className="hidden grid-cols-[minmax(200px,1.1fr)_minmax(300px,1.7fr)_minmax(150px,.8fr)_minmax(105px,.55fr)_minmax(110px,.55fr)_auto] gap-4 rounded-t-lg bg-gray-50 px-4 py-2 text-[12px] font-semibold uppercase tracking-wide text-gray-500 lg:grid">
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
                  className={`grid gap-4 px-4 py-4 lg:grid-cols-[minmax(200px,1.1fr)_minmax(300px,1.7fr)_minmax(150px,.8fr)_minmax(105px,.55fr)_minmax(110px,.55fr)_auto] ${
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
                          : 'inline-flex rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                      }
                    >
                      {sourceLabel(route)}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-start justify-end gap-1">
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
                    <button
                      type="button"
                      onClick={() => setEditingRoute(route)}
                      className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
                    >
                      Edit
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
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
