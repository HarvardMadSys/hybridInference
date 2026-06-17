'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import {
  ProviderApiKeyItem,
  ProviderRoute,
  ProviderRouteOption,
  listProviderKeys,
  listProviderRoutes,
  updateProviderRoute,
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
  return route.source === 'override' ? 'Override active' : 'YAML';
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

  const replaceRoute = useCallback((updated: ProviderRoute) => {
    setRoutes((current) =>
      current.map((route) => (routeKey(route) === routeKey(updated) ? updated : route)),
    );
    setEditingRoute(updated);
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
          <span className="rounded-full bg-gray-100 px-2.5 py-1 text-[12px] font-medium text-gray-700">
            {strategy}
          </span>
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
        <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50 text-left text-[12px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Candidate</th>
                <th className="px-3 py-2">Route provider</th>
                <th className="px-3 py-2">Override provider</th>
                <th className="px-3 py-2">API key</th>
                <th className="px-3 py-2">Provider model ID</th>
                <th className="px-3 py-2">Daily quota</th>
                {!isRoutewise && <th className="px-3 py-2">YAML</th>}
                {!isRoutewise && <th className="px-3 py-2">Effective</th>}
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {selectedRoutes.map((route) => {
                const key = routeKey(route);
                const isEditing = editingRoute ? routeKey(editingRoute) === key : false;
                return (
                  <tr key={key} className={isEditing ? 'bg-gray-50' : 'bg-white'}>
                    <td className="px-3 py-2 text-gray-900">
                      <div className="font-medium">{route.route_type}</div>
                      <div className="mt-1 max-w-[280px] truncate font-mono text-[11px] text-gray-400">
                        {route.endpoint_id}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-gray-700">
                      <div className="font-medium">{route.provider}</div>
                    </td>
                    <td className="px-3 py-2 text-gray-700">
                      <div className="font-medium">{route.upstream_provider}</div>
                      <div className="mt-1 max-w-[260px] truncate text-[11px] text-gray-400">
                        {route.base_url}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-gray-600">{keyLabel(route)}</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-gray-600">
                      {route.provider_model_id ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-600">
                      {route.quota_limit ? route.quota_limit.toLocaleString() : '—'}
                    </td>
                    {!isRoutewise && (
                      <td className="px-3 py-2 text-gray-600">
                        {formatWeight(route.yaml_weight)}
                      </td>
                    )}
                    {!isRoutewise && (
                      <td className="px-3 py-2 text-gray-900">
                        {formatWeight(route.effective_weight)}
                      </td>
                    )}
                    <td className="px-3 py-2">
                      <span
                        className={
                          route.source === 'override'
                            ? 'rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700'
                            : 'rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                        }
                      >
                        {sourceLabel(route)}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => setEditingRoute(route)}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
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
                {providerOptions.map((option) => (
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
