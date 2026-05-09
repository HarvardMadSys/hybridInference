'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
import { ProviderQuotaResult, getProviderQuotas } from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { PerformanceTab } from '@/components/features/admin/PerformanceTab';

type SubTab = 'quotas' | 'performance';

const SUB_TABS: { key: SubTab; label: string }[] = [
  { key: 'quotas', label: 'Quotas' },
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

function ProviderCard({ provider }: { provider: ProviderQuotaResult }) {
  const stripeColor = provider.ok
    ? 'bg-emerald-500'
    : provider.error === 'not_configured'
      ? 'bg-gray-300'
      : 'bg-red-400';

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
      <div className={`h-1 ${stripeColor}`} />
      <div className="p-4">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-[15px] font-semibold text-gray-900">{provider.display_name}</h3>
          <span
            className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}
          >
            {provider.key_masked ?? 'Not configured'}
          </span>
        </div>

        {provider.ok ? (
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
          <p className="mt-3 text-[12px] text-gray-400">
            Quota unavailable — <span className="text-gray-500">{provider.error}</span>
          </p>
        )}
      </div>
    </div>
  );
}

function QuotasSection() {
  const [providerQuotas, setProviderQuotas] = useState<ProviderQuotaResult[]>([]);
  const [providerQuotasLoading, setProviderQuotasLoading] = useState(false);

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
          {providerQuotas.map((p) => (
            <ProviderCard key={p.name} provider={p} />
          ))}
        </div>
      )}
    </div>
  );
}

export function ProvidersTab() {
  const [active, setActive] = useState<SubTab>('quotas');

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

      {active === 'quotas' ? <QuotasSection /> : <PerformanceTab />}
    </div>
  );
}
