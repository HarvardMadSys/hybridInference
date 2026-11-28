'use client';

/**
 * Admin → Providers → Overview: the provider registry, with each provider's
 * switch in and out of routing; the keys of the provider chosen there; and what
 * each key's plan has used of its limits. One page, so "Manage keys" moves down
 * to the keys rather than away, and a provider switched off in the registry
 * reads off on its quota cards too. Performance is a page of its own (see
 * `ProvidersSectionNav`).
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  ProviderQuotaResult,
  RoutableProvider,
  getProviderQuotas,
  getRoutableProviders,
  setProviderDisabled,
  setProviderQuotaKeyDisabled,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { ProviderToggle } from '@/components/features/admin/ProviderToggle';
import {
  PROVIDER_KEYS_ANCHOR,
  PROVIDER_QUOTAS_ANCHOR,
} from '@/components/features/admin/providerRoutes';
import { useProviderSelection } from '@/components/features/admin/providerSelection';
import { ProviderKeysTab } from '@/app/dashboard/admin/ProviderKeysTab';
import {
  ProviderOverviewTab,
  type ProviderRouting,
} from '@/app/dashboard/admin/ProviderOverviewTab';
import { UpstreamConcurrencyPanel } from '@/app/dashboard/admin/UpstreamConcurrencyPanel';

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

function concurrencyMessage(provider: ProviderQuotaResult): string | null {
  const usage = provider.usages.find((u) => u.label.toLowerCase() === 'concurrency');
  if (!usage) return null;
  if (usage.limit == null) {
    return 'Concurrency available';
  }
  return `Concurrency ${formatNum(usage.limit)}${usage.unit ? ` ${usage.unit}` : ''}`;
}

// A single concurrency allowance or a probe error gets a one-line status.
function reportsAvailability(provider: ProviderQuotaResult): boolean {
  return (
    (provider.usages.length === 1 && provider.usages[0].label.toLowerCase() === 'concurrency') ||
    provider.error === 'plan_api_disabled' ||
    provider.error === 'probe_unavailable'
  );
}

function availabilityMessage(provider: ProviderQuotaResult): string {
  if (provider.ok) {
    return concurrencyMessage(provider) ?? 'Available';
  }
  if (provider.error === 'not_configured') {
    return 'Not configured.';
  }
  if (provider.error === 'plan_api_disabled') {
    return 'The current subscription plan does not have API access enabled.';
  }
  if (provider.error === 'probe_unavailable') {
    return 'Status unavailable — no configured route to probe.';
  }
  if (provider.error === 'timeout') {
    return 'Unavailable — probe timed out.';
  }
  if (provider.error === 'auth_failed') {
    return 'Unavailable — auth failed.';
  }
  return `Unavailable — ${provider.error ?? 'unknown error'}`;
}

function ProviderCard({
  provider,
  busy,
  keyBusy,
  onToggleDisabled,
  onToggleKeyDisabled,
}: {
  provider: ProviderQuotaResult;
  busy: boolean;
  keyBusy: boolean;
  onToggleDisabled: () => void;
  onToggleKeyDisabled: () => void;
}) {
  const dimmed = provider.disabled || provider.key_disabled;
  const stripeColor = dimmed
    ? 'bg-gray-400'
    : provider.ok
      ? 'bg-emerald-500'
      : provider.error === 'not_configured' || provider.error === 'probe_unavailable'
        ? 'bg-gray-300'
        : 'bg-red-400';

  return (
    <div
      className={`overflow-hidden rounded-xl border shadow-sm ${
        dimmed ? 'border-gray-200 bg-gray-50' : 'border-gray-200 bg-white'
      }`}
    >
      <div className={`h-1 ${stripeColor}`} />
      <div className={`p-4 ${dimmed ? 'opacity-60' : ''}`}>
        <div className="flex items-baseline justify-between gap-3">
          <div className="flex items-center gap-2">
            <h3 className="text-[15px] font-semibold text-gray-900">{provider.display_name}</h3>
            {provider.disabled && (
              <span className="rounded-full bg-gray-200 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-600">
                Disabled
              </span>
            )}
            {provider.key_disabled && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700">
                Key disabled
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`tabular-nums text-[11px] ${provider.key_configured ? 'text-gray-500' : 'text-gray-400'}`}
            >
              {provider.key_masked ?? 'Not configured'}
            </span>
            {provider.key_ref && (
              <div className="flex items-center gap-1">
                <span className="text-[10px] uppercase tracking-wide text-gray-400">Key</span>
                <ProviderToggle
                  disabled={provider.key_disabled}
                  busy={keyBusy}
                  onToggle={onToggleKeyDisabled}
                  label={`${provider.display_name} key ${provider.key_masked ?? ''}`.trim()}
                  noun="Key"
                />
              </div>
            )}
            <div className="flex items-center gap-1">
              <span className="text-[10px] uppercase tracking-wide text-gray-400">Provider</span>
              <ProviderToggle
                disabled={provider.disabled}
                busy={busy}
                onToggle={onToggleDisabled}
                label={provider.display_name}
              />
            </div>
          </div>
        </div>

        {provider.key_disabled ? (
          <p className="mt-3 text-[12px] text-amber-700">
            Key disabled — removed from the rotation pool and not used for inference.
          </p>
        ) : reportsAvailability(provider) ? (
          <p
            className={`mt-3 text-[12px] ${
              provider.ok
                ? 'text-emerald-700'
                : provider.error === 'not_configured' || provider.error === 'probe_unavailable'
                  ? 'text-gray-400'
                  : 'text-red-600'
            }`}
          >
            {availabilityMessage(provider)}
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

interface ProviderQuotasSectionProps {
  /**
   * Whether routing leaves each provider out, as the registry on the same page
   * shows it: a provider switched there reads switched on its cards too.
   */
  routedOff?: ReadonlyMap<string, boolean>;
  /** A provider switched on a card, for the registry to show. */
  onProviderSwitched?: (provider: string, disabled: boolean) => void;
}

/** Quotas: what each key's plan has used of its limits, with a switch for the key and its provider. */
export function ProviderQuotasSection({
  routedOff,
  onProviderSwitched,
}: ProviderQuotasSectionProps = {}) {
  const toast = useAccountToast();
  // Deployment docs may describe an operator's service, not backend extensions.
  const quotaDocsUrl =
    'https://github.com/HarvardMadSys/hybridInference/blob/dev/docs/developer/backend-extensions.md#quota-reporting';
  const [providerQuotas, setProviderQuotas] = useState<ProviderQuotaResult[]>([]);
  const [providerQuotasLoading, setProviderQuotasLoading] = useState(false);
  const writes = useConfirmAction();

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
  }, [toast]);

  const { perform } = writes;

  // Both switches act on the click: switching back undoes either one.
  const handleToggle = useCallback(
    async (provider: ProviderQuotaResult) => {
      const next = !provider.disabled;
      const failure = await perform({
        run: async () => {
          await setProviderDisabled(provider.name, next);
          // Reflect the new state on every card sharing this provider label.
          setProviderQuotas((prev) =>
            prev.map((p) => (p.name === provider.name ? { ...p, disabled: next } : p)),
          );
          onProviderSwitched?.(provider.name, next);
          toast.success(
            `${provider.display_name} ${next ? 'disabled — excluded from routing' : 'enabled'}`,
          );
        },
      });
      if (failure) toast.error(failure);
    },
    [onProviderSwitched, perform, toast],
  );

  const handleToggleKey = useCallback(
    async (provider: ProviderQuotaResult) => {
      const keyRef = provider.key_ref;
      if (!keyRef) return;
      const next = !provider.key_disabled;
      const failure = await perform({
        run: async () => {
          const res = await setProviderQuotaKeyDisabled(provider.name, keyRef, next);
          if (!next && res.pools_updated === 0) {
            toast.error('Key enabled but not attached to any live pool — it will not be used.');
          } else {
            toast.success(`Key ${next ? 'disabled' : 'enabled'}`);
          }
          // Reload rather than patch in place: disabling moves the key out of the
          // probed set and onto a disabled card (and enabling does the reverse).
          await loadProviderQuotas();
        },
      });
      if (failure) toast.error(failure);
    },
    [loadProviderQuotas, perform, toast],
  );

  useEffect(() => {
    loadProviderQuotas();
  }, [loadProviderQuotas]);

  // A provider switched in the registry since these cards loaded reads as switched.
  const shown = useMemo(
    () =>
      providerQuotas.map((p) => {
        const off = routedOff?.get(p.name);
        return off === undefined || off === p.disabled ? p : { ...p, disabled: off };
      }),
    [providerQuotas, routedOff],
  );

  return (
    <div>
      <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="provider-quotas-heading" className="text-[14px] font-semibold text-gray-900">
            Quotas
          </h2>
          <p className="mt-1 text-[12px] text-gray-500">
            What each key&apos;s plan has used of its limits, where a quota extension reports it.
          </p>
        </div>
        <div className="flex items-center gap-2">
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
      </div>
      {providerQuotasLoading && shown.length === 0 ? (
        <div className="flex justify-center py-24">
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
        </div>
      ) : shown.length === 0 ? (
        <div className="py-24 text-center">
          <p className="text-[13px] text-gray-500">No quota data to display.</p>
          <p className="mt-2 text-[12px] text-gray-500">
            Quota reporting requires a configured backend extension and data source.
          </p>
          <a
            href={quotaDocsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-2 inline-block text-[12px] text-gray-600 underline hover:text-gray-900"
          >
            Quota reporting documentation
          </a>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {shown.map((p, index) => (
            <ProviderCard
              key={`${p.name}-${p.key_index ?? 'single'}-${p.key_ref ?? p.key_masked ?? index}`}
              provider={p}
              busy={writes.busy}
              keyBusy={writes.busy}
              onToggleDisabled={() => void handleToggle(p)}
              onToggleKeyDisabled={() => void handleToggleKey(p)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Which providers routing may use: the live routing table's providers, each
 * with the switch that takes it out of routing for every model. The registry
 * shows it beside each provider; a switch on a quota card is reported here.
 */
export function useProviderRouting(): ProviderRouting & {
  /** Each provider's switch, by provider name, for the quota cards. */
  routedOff: ReadonlyMap<string, boolean>;
  /** A provider switched elsewhere on the page. */
  switched: (provider: string, disabled: boolean) => void;
} {
  const toast = useAccountToast();
  const [providers, setProviders] = useState<RoutableProvider[] | null>(null);
  const writes = useConfirmAction();
  const { perform } = writes;

  const load = useCallback(async () => {
    try {
      const d = await getRoutableProviders();
      setProviders(d.providers);
    } catch (e) {
      toast.error(getErrorMessage(e));
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const switched = useCallback((provider: string, disabled: boolean) => {
    setProviders(
      (prev) => prev?.map((p) => (p.provider === provider ? { ...p, disabled } : p)) ?? prev,
    );
  }, []);

  // The switch acts on the click: switching it back undoes it.
  const toggle = useCallback(
    async (provider: RoutableProvider) => {
      const next = !provider.disabled;
      const failure = await perform({
        run: async () => {
          await setProviderDisabled(provider.provider, next);
          switched(provider.provider, next);
          toast.success(
            `${provider.provider} ${next ? 'disabled — excluded from routing' : 'enabled'}`,
          );
        },
      });
      if (failure) toast.error(failure);
    },
    [perform, switched, toast],
  );

  const routedOff = useMemo(
    () => new Map((providers ?? []).map((p) => [p.provider, p.disabled])),
    [providers],
  );
  const onToggle = useCallback((provider: RoutableProvider) => void toggle(provider), [toggle]);
  const reload = useCallback(() => void load(), [load]);

  return { providers, busy: writes.busy, onToggle, reload, routedOff, switched };
}

/** Scrolls one of the page's sections into view. */
function revealSection(id: string) {
  document.getElementById(id)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
}

/** The provider registry. "Manage keys" shows that provider's keys, further down the page. */
export function ProviderOverviewSection({
  routing,
  refreshKey,
  onRegistryChange,
}: {
  routing?: ProviderRouting;
  refreshKey?: number;
  onRegistryChange?: () => void;
}) {
  const { request } = useProviderSelection();
  return (
    <ProviderOverviewTab
      routing={routing}
      refreshKey={refreshKey}
      onRegistryChange={onRegistryChange}
      onManageKeys={(provider) => {
        request(provider);
        revealSection(PROVIDER_KEYS_ANCHOR);
      }}
    />
  );
}

/** Keys: a provider's keys, and the upstream concurrency of every key the gateway calls. */
export function ProviderKeysSection({
  refreshKey,
  onKeysChange,
}: {
  refreshKey?: number;
  onKeysChange?: () => void;
} = {}) {
  const { provider, requested, lastRequest, show } = useProviderSelection();
  // Read once, as Keys opens: its own report of what it shows must not change
  // how it opened. A "Manage keys" made after that is passed on as a request.
  const [opening] = useState(() => ({
    provider,
    requested,
    serial: lastRequest?.serial ?? 0,
  }));
  const request = lastRequest && lastRequest.serial > opening.serial ? lastRequest : undefined;
  return (
    <div className="space-y-6">
      <ProviderKeysTab
        initialProvider={opening.provider}
        keepUnlistedProvider={opening.requested}
        request={request}
        refreshKey={refreshKey}
        onProviderChange={show}
        onKeysChange={onKeysChange}
      />
      {/* Not scoped to the selected provider: the limiter buckets by key, so
          this lists every key the gateway is currently calling. */}
      <UpstreamConcurrencyPanel />
    </div>
  );
}

/** Admin → Providers → Overview: the registry and its routing switches, then Keys, then Quotas. */
export function ProvidersOverviewPage() {
  const routing = useProviderRouting();
  // The sections share one page, so each reads again what another changed:
  // the registry counts keys, and Keys lists the registry's providers. A
  // provider deleted from the registry leaves routing too.
  const [registryRefresh, setRegistryRefresh] = useState(0);
  const [keysRefresh, setKeysRefresh] = useState(0);
  const { reload: reloadRouting } = routing;
  const keysChanged = useCallback(() => setRegistryRefresh((n) => n + 1), []);
  const registryChanged = useCallback(() => {
    setKeysRefresh((n) => n + 1);
    reloadRouting();
  }, [reloadRouting]);
  return (
    <div className="space-y-10">
      <ProviderOverviewSection
        routing={routing}
        refreshKey={registryRefresh}
        onRegistryChange={registryChanged}
      />
      <section
        id={PROVIDER_KEYS_ANCHOR}
        aria-labelledby="provider-keys-heading"
        className="scroll-mt-4 border-t border-gray-200 pt-8"
      >
        <div className="mb-4">
          <h2 id="provider-keys-heading" className="text-[14px] font-semibold text-gray-900">
            Keys
          </h2>
          <p className="mt-1 text-[12px] text-gray-500">
            Each provider&apos;s API keys, and the upstream concurrency of every key the gateway
            calls.
          </p>
        </div>
        <ProviderKeysSection refreshKey={keysRefresh} onKeysChange={keysChanged} />
      </section>
      <section
        id={PROVIDER_QUOTAS_ANCHOR}
        aria-labelledby="provider-quotas-heading"
        className="scroll-mt-4 border-t border-gray-200 pt-8"
      >
        <ProviderQuotasSection
          routedOff={routing.routedOff}
          onProviderSwitched={routing.switched}
        />
      </section>
    </div>
  );
}
