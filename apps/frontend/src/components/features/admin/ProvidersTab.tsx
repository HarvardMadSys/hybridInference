'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import {
  ProviderQuotaResult,
  RoutableProvider,
  getProviderQuotas,
  getRoutableProviders,
  setProviderDisabled,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { PerformanceTab } from '@/components/features/admin/PerformanceTab';
import { ProviderKeysTab } from '@/app/dashboard/admin/ProviderKeysTab';
import { ProviderOverviewTab } from '@/app/dashboard/admin/ProviderOverviewTab';

type SubTab = 'overview' | 'availability' | 'quotas' | 'keys' | 'performance';

const SUB_TABS: { key: SubTab; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'availability', label: 'Availability' },
  { key: 'quotas', label: 'Quotas' },
  { key: 'keys', label: 'Keys' },
  { key: 'performance', label: 'Performance' },
];

function pct(used: number | null, limit: number | null): number | null {
  if (used == null || limit == null || limit <= 0) return null;
  return Math.min(100, (used / limit) * 100);
}

function formatNum(v: number | null): string {
  if (v == null) return '—';
  if (Math.abs(v) < 0.01 && v !== 0) return v.toFixed(4);
  if (Math.abs(v) < 1 && v !== 0) return v.toFixed(1);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toFixed(2);
}

function unavailableMessage(provider: ProviderQuotaResult): string {
  if (provider.error === 'probe_unavailable') {
    return 'Status unavailable — no configured route to probe.';
  }
  return `Quota unavailable — ${provider.error ?? 'unknown error'}`;
}

function featherlessConcurrencyMessage(provider: ProviderQuotaResult): string | null {
  const usage = provider.usages.find((u) => u.label.toLowerCase() === 'concurrency');
  if (!usage) return null;
  if (usage.limit == null) {
    return 'Concurrency available';
  }
  return `Concurrency ${formatNum(usage.limit)} units`;
}

function featherlessMessage(provider: ProviderQuotaResult): string {
  if (provider.ok) {
    return featherlessConcurrencyMessage(provider) ?? 'Available';
  }
  if (provider.error === 'not_configured') {
    return 'Not configured.';
  }
  if (provider.error === 'plan_api_disabled') {
    return 'The current subscription plan does not have API access enabled.';
  }
  if (provider.error === 'probe_unavailable') {
    return 'Status unavailable — no configured Featherless route to probe.';
  }
  if (provider.error === 'timeout') {
    return 'Unavailable — probe timed out.';
  }
  if (provider.error === 'auth_failed') {
    return 'Unavailable — auth failed.';
  }
  return `Unavailable — ${provider.error ?? 'unknown error'}`;
}

function ProviderToggle({
  disabled,
  busy,
  onToggle,
  label,
}: {
  disabled: boolean;
  busy: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={busy}
      role="switch"
      aria-checked={!disabled}
      aria-label={`${disabled ? 'Enable' : 'Disable'} ${label}`}
      title={
        disabled ? 'Provider disabled — click to enable' : 'Provider enabled — click to disable'
      }
      className={`relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition disabled:opacity-50 ${
        disabled ? 'bg-gray-300' : 'bg-emerald-500'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
          disabled ? 'translate-x-0.5' : 'translate-x-[18px]'
        }`}
      />
    </button>
  );
}

function ProviderCard({
  provider,
  busy,
  onToggleDisabled,
}: {
  provider: ProviderQuotaResult;
  busy: boolean;
  onToggleDisabled: () => void;
}) {
  const isFeatherless = provider.name === 'featherless';
  const stripeColor = provider.disabled
    ? 'bg-gray-400'
    : provider.ok
      ? 'bg-emerald-500'
      : provider.error === 'not_configured' || provider.error === 'probe_unavailable'
        ? 'bg-gray-300'
        : 'bg-red-400';

  return (
    <div
      className={`overflow-hidden rounded-xl border shadow-sm ${
        provider.disabled ? 'border-gray-200 bg-gray-50' : 'border-gray-200 bg-white'
      }`}
    >
      <div className={`h-1 ${stripeColor}`} />
      <div className={`p-4 ${provider.disabled ? 'opacity-60' : ''}`}>
        <div className="flex items-baseline justify-between gap-3">
          <div className="flex items-center gap-2">
            <h3 className="text-[15px] font-semibold text-gray-900">{provider.display_name}</h3>
            {provider.disabled && (
              <span className="rounded-full bg-gray-200 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-600">
                Disabled
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}
            >
              {provider.key_masked ?? 'Not configured'}
            </span>
            <ProviderToggle
              disabled={provider.disabled}
              busy={busy}
              onToggle={onToggleDisabled}
              label={provider.display_name}
            />
          </div>
        </div>

        {isFeatherless ? (
          <p
            className={`mt-3 text-[12px] ${
              provider.ok
                ? 'text-emerald-700'
                : provider.error === 'not_configured' || provider.error === 'probe_unavailable'
                  ? 'text-gray-400'
                  : 'text-red-600'
            }`}
          >
            {featherlessMessage(provider)}
          </p>
        ) : provider.ok ? (
          provider.usages.length === 0 ? (
            <p className="mt-3 text-[12px] text-gray-400">No usage data returned.</p>
          ) : (
            <div className="mt-3 space-y-3">
              {provider.usages.map((u, i) => {
                const p = pct(u.used, u.limit);
                return (
                  <div key={i}>
                    <div className="flex items-baseline justify-between text-[12px]">
                      <span className="text-gray-600">{u.label}</span>
                      <span className="tabular-nums text-gray-700">
                        {formatNum(u.used)}
                        {u.limit != null && ` / ${formatNum(u.limit)}`} {u.unit}
                        {p != null && <span className="ml-1 text-gray-400">({p.toFixed(0)}%)</span>}
                      </span>
                    </div>
                    {p != null && (
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-gray-100">
                        <div
                          className={`h-full ${
                            p >= 90 ? 'bg-red-400' : p >= 70 ? 'bg-amber-400' : 'bg-gray-900'
                          }`}
                          style={{ width: `${p}%` }}
                        />
                      </div>
                    )}
                    {u.reset_at && (
                      <p className="mt-1 text-[11px] text-gray-400">
                        Resets at{' '}
                        {new Date(u.reset_at).toLocaleString('en-US', {
                          year: 'numeric',
                          month: 'short',
                          day: 'numeric',
                          hour: 'numeric',
                          minute: '2-digit',
                          timeZoneName: 'short',
                        })}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )
        ) : (
          <p className="mt-3 text-[12px] text-gray-400">{unavailableMessage(provider)}</p>
        )}
      </div>
    </div>
  );
}

function QuotasSection() {
  const [providerQuotas, setProviderQuotas] = useState<ProviderQuotaResult[]>([]);
  const [providerQuotasLoading, setProviderQuotasLoading] = useState(false);
  const [togglingProvider, setTogglingProvider] = useState<string | null>(null);

  const loadProviderQuotas = useCallback(async () => {
    setProviderQuotasLoading(true);
    try {
      const d = await getProviderQuotas();
      setProviderQuotas(d.providers);
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setProviderQuotasLoading(false);
    }
  }, []);

  const handleToggle = useCallback(async (provider: ProviderQuotaResult) => {
    const next = !provider.disabled;
    setTogglingProvider(provider.name);
    try {
      await setProviderDisabled(provider.name, next);
      // Reflect the new state on every card sharing this provider label.
      setProviderQuotas((prev) =>
        prev.map((p) => (p.name === provider.name ? { ...p, disabled: next } : p)),
      );
      toast.success(
        `${provider.display_name} ${next ? 'disabled — excluded from routing' : 'enabled'}`,
      );
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setTogglingProvider(null);
    }
  }, []);

  useEffect(() => {
    loadProviderQuotas();
  }, [loadProviderQuotas]);

  return (
    <div>
      <div className="mb-3 flex items-center justify-end gap-2">
        {providerQuotasLoading && (
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        )}
        <button
          type="button"
          onClick={() => loadProviderQuotas()}
          disabled={providerQuotasLoading}
          className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
        >
          Refresh
        </button>
      </div>
      {providerQuotasLoading && providerQuotas.length === 0 ? (
        <div className="flex justify-center py-24">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : providerQuotas.length === 0 ? (
        <div className="py-24 text-center">
          <p className="text-[13px] text-gray-400">No provider data.</p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {providerQuotas.map((p, index) => (
            <ProviderCard
              key={`${p.name}-${p.key_index ?? 'single'}-${p.key_masked ?? index}`}
              provider={p}
              busy={togglingProvider === p.name}
              onToggleDisabled={() => handleToggle(p)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function AvailabilitySection() {
  const [providers, setProviders] = useState<RoutableProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await getRoutableProviders();
      setProviders(d.providers);
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const handleToggle = useCallback(async (provider: RoutableProvider) => {
    const next = !provider.disabled;
    setToggling(provider.provider);
    try {
      await setProviderDisabled(provider.provider, next);
      setProviders((prev) =>
        prev.map((p) => (p.provider === provider.provider ? { ...p, disabled: next } : p)),
      );
      toast.success(
        `${provider.provider} ${next ? 'disabled — excluded from routing' : 'enabled'}`,
      );
    } catch (e) {
      toast.error(getErrorMessage(e));
    } finally {
      setToggling(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="text-[12px] text-gray-500">
          Disabling a provider removes it from routing for every model. Requests fall back to the
          model&apos;s remaining providers; models with no other route will fail until re-enabled.
        </p>
        <div className="flex items-center gap-2">
          {loading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          <button
            type="button"
            onClick={() => load()}
            disabled={loading}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </div>
      {loading && providers.length === 0 ? (
        <div className="flex justify-center py-24">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : providers.length === 0 ? (
        <div className="py-24 text-center">
          <p className="text-[13px] text-gray-400">No routable providers.</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-gray-200 text-left text-[11px] uppercase tracking-wide text-gray-500">
                <th className="px-4 py-2 font-medium">Provider</th>
                <th className="px-4 py-2 font-medium">Models</th>
                <th className="px-4 py-2 font-medium">Endpoints</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 text-right font-medium">Enabled</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr
                  key={p.provider}
                  className={`border-b border-gray-100 last:border-0 ${
                    p.disabled ? 'bg-gray-50' : ''
                  }`}
                >
                  <td className="px-4 py-2.5 font-medium text-gray-900">{p.provider}</td>
                  <td className="px-4 py-2.5 tabular-nums text-gray-600">{p.model_count}</td>
                  <td className="px-4 py-2.5 tabular-nums text-gray-600">{p.endpoint_count}</td>
                  <td className="px-4 py-2.5">
                    {p.disabled ? (
                      <span className="rounded-full bg-gray-200 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                        Disabled
                      </span>
                    ) : (
                      <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                        Active
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex justify-end">
                      <ProviderToggle
                        disabled={p.disabled}
                        busy={toggling === p.provider}
                        onToggle={() => handleToggle(p)}
                        label={p.provider}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ProvidersTab() {
  const [active, setActive] = useState<SubTab>('overview');

  return (
    <div className="mt-6">
      <div
        role="tablist"
        aria-label="Providers sub-tabs"
        className="mb-4 flex flex-wrap items-center gap-1 border-b border-gray-200"
      >
        {SUB_TABS.map((tab) => {
          const isActive = active === tab.key;
          return (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => setActive(tab.key)}
              className={`-mb-px border-b-2 px-3.5 py-1.5 text-[13px] font-medium transition ${
                isActive
                  ? 'border-gray-900 text-gray-900'
                  : 'border-transparent text-gray-500 hover:text-gray-900'
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {active === 'overview' ? (
        <ProviderOverviewTab />
      ) : active === 'availability' ? (
        <AvailabilitySection />
      ) : active === 'quotas' ? (
        <QuotasSection />
      ) : active === 'keys' ? (
        <ProviderKeysTab />
      ) : (
        <PerformanceTab />
      )}
    </div>
  );
}
