'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import {
  OffloadRoute,
  OpenRouterProviderOption,
  OpenRouterSortPolicy,
  ProviderApiKeyItem,
  ProviderRoute,
  ProviderRouteStrategy,
  ProviderRouteType,
  RouteDeploymentType,
  ProviderRouteOption,
  Role,
  RouteWeight,
  clearRouteWeight,
  createProviderRouteModel,
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listOffloadRoutes,
  listRouteWeights,
  listProviderKeys,
  listOpenRouterProviderOptions,
  listProviderRoutes,
  removeProviderRoute,
  restoreProviderRoute,
  setRouteWeight,
  updateProviderRoute,
  updateProviderRouteCandidate,
  updateProviderRouteState,
  updateProviderRouteCapabilities,
  resetProviderRouteCapabilities,
  updateProviderRouteStrategy,
  verifyProviderRoute,
  verifyProviderRouteModel,
  verifyProviderRouteCandidate,
} from '@/lib/api/admin';
import { ChangeList, plural, type Change } from '@/components/features/admin/confirm/ChangeList';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { writeEach } from '@/components/features/admin/confirm/writeEach';
import { BUTTON_GHOST, BUTTON_PRIMARY } from '@/components/features/admin/adminStyles';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { OffloadRoutePanel } from './OffloadRoutePanel';
import { RouteDeploymentField } from './RouteDeploymentField';
import {
  RouteCapabilityFields,
  capabilityChanges,
  capabilityDraftFrom,
  capabilityRows,
  describeCapabilities,
  emptyCapabilityDraft,
  readCapabilityDraft,
  type CapabilityDraft,
  type DeclaredChoice,
} from './RouteCapabilityFields';
import type { OffloadQueueInfo } from './OffloadRoutePanel';
import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';
import { RoutewiseDecisionsPanel } from './RoutewiseDecisionsPanel';
import { Select } from '@/components/ui/Select';

type RouteForm = {
  upstreamProvider: string;
  openRouterProvider: string;
  customOpenRouterProvider: string;
  openRouterSort: string;
  baseUrl: string;
  // Null until the admin picks one in this edit: the server then keeps the
  // route's declaration, or resets it to auto when the endpoint moves.
  deploymentType: RouteDeploymentType | null;
  apiKeyId: string;
  providerModelId: string;
  quotaLimit: string;
  concurrencyLimit: string;
};

// Starts unanswered: a choice the admin never made must not be sent as "no".

type CreateRouteForm = {
  routeType: ProviderRouteType;
  upstreamProvider: string;
  openRouterProvider: string;
  customOpenRouterProvider: string;
  openRouterSort: string;
  baseUrl: string;
  deploymentType: RouteDeploymentType;
  apiKeyId: string;
  providerModelId: string;
  quotaLimit: string;
  concurrencyLimit: string;
  weight: string;
  pricingPrompt: string;
  pricingCompletion: string;
  pricingCacheReads: string;
  pricingCacheWrites: string;
  contextLength: string;
  maxOutputLength: string;
  imageInput: DeclaredChoice;
  toolCalling: DeclaredChoice;
  otherInputModalities: string[];
};

const emptyCreateForm: CreateRouteForm = {
  routeType: 'on_demand',
  upstreamProvider: '',
  openRouterProvider: '',
  customOpenRouterProvider: '',
  openRouterSort: '',
  baseUrl: '',
  deploymentType: 'auto',
  apiKeyId: '',
  providerModelId: '',
  quotaLimit: '5000',
  concurrencyLimit: '1',
  weight: '1',
  pricingPrompt: '',
  pricingCompletion: '',
  pricingCacheReads: '',
  pricingCacheWrites: '',
  ...emptyCapabilityDraft,
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
const OPENROUTER_PROVIDER_SLUG_RE = /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/;
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

// The gateway's own reason. The shared mapping reads any 409 as a signup
// conflict ("This email is already registered"), which would hide why a route
// edit, disable or delete was refused.
function routeErrorMessage(err: unknown) {
  return err instanceof APIError && err.message ? err.message : getErrorMessage(err);
}

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

function overrideNotApplied(route: ProviderRoute) {
  return route.source === 'override' && route.override_applied === false;
}

function sourceLabel(route: ProviderRoute) {
  if (overrideNotApplied(route) && !route.removed) return 'Override not applied';
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

function formatRouteQuotaValue(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '—';
  return Math.round(value).toLocaleString();
}

function routeQuotaCurrentLimit(route: ProviderRoute) {
  return route.quota_current_limit ?? route.quota_limit ?? null;
}

function routeQuotaUsedPercent(route: ProviderRoute) {
  const limit = routeQuotaCurrentLimit(route);
  if (limit == null || limit <= 0 || route.quota_remaining == null) return null;
  return Math.min(100, Math.max(0, ((limit - route.quota_remaining) / limit) * 100));
}

function routeQuotaResetLabel(route: ProviderRoute) {
  if (!route.quota_reset_at) return null;
  const resetAt = new Date(route.quota_reset_at);
  if (Number.isNaN(resetAt.getTime())) return null;
  return resetAt.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}

function RouteQuotaCell({ route }: { route: ProviderRoute }) {
  const limit = routeQuotaCurrentLimit(route);
  const usedPct = routeQuotaUsedPercent(route);
  const resetLabel = routeQuotaResetLabel(route);
  if (route.quota_remaining == null || limit == null) {
    return (
      <div>
        <div>{routeLimitLabel(route, true)}</div>
        <div className="mt-1 text-[11px] leading-4 text-gray-400">Snapshot pending</div>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <div className="tabular-nums text-gray-900">
        {formatRouteQuotaValue(route.quota_remaining)} left
      </div>
      <div className="text-[11px] leading-4 text-gray-400">
        of {formatRouteQuotaValue(limit)} today
      </div>
      {usedPct != null && (
        <div className="h-1 overflow-hidden rounded-full bg-gray-100">
          <div
            className={
              route.quota_remaining === 0
                ? 'h-full bg-red-400'
                : usedPct >= 80
                  ? 'h-full bg-amber-400'
                  : 'h-full bg-emerald-500'
            }
            style={{ width: `${usedPct}%` }}
          />
        </div>
      )}
      {resetLabel && <div className="text-[11px] leading-4 text-gray-400">Resets {resetLabel}</div>}
    </div>
  );
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
  return /^openrouter\[([A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*)\]$/.exec(kind)?.[1] ?? null;
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
      // Pins share OpenRouter's key and its route-type policy, so the first
      // OpenRouter-kind option describes the collapsed target.
      openRouterOption ??= {
        provider: 'openrouter',
        label: 'OpenRouter',
        kind: 'openrouter',
        key_provider: option.key_provider,
        default_base_url: option.default_base_url,
        route_types: option.route_types,
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
  // The gateway reports which route types the deployment allows per provider;
  // an option without the field is unrestricted.
  return option.route_types ? option.route_types.includes(routeType) : true;
}

function routePrimaryProvider(route: ProviderRoute) {
  if (route.provider === route.upstream_provider && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.provider;
}

// Mirrors the gateway's endpoint comparison closely enough to tell whether an
// edit still points at the route's current endpoint.
function canonicalEndpointUrl(value: string) {
  const trimmed = value.trim();
  try {
    const url = new URL(
      trimmed.includes('://') ? trimmed : `https://${trimmed.replace(/^\/+/, '')}`,
    );
    return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, '')}`;
  } catch {
    return trimmed.replace(/\/+$/, '');
  }
}

function routePrimaryUpstreamProvider(route: ProviderRoute) {
  if (route.upstream_provider !== 'openrouter' && route.key_provider === 'openrouter') {
    return 'openrouter';
  }
  return route.upstream_provider;
}

const ROUTE_EDIT_FIELD_LABELS: Record<string, string> = {
  upstream_provider: 'Provider',
  openrouter_provider: 'OpenRouter provider',
  openrouter_sort: 'OpenRouter sort',
  base_url: 'Base URL',
  deployment_type: 'Deployment',
  api_key_id: 'API key',
  provider_model_id: 'Provider model ID',
  quota_limit: 'Quota limit',
  concurrency_limit: 'Concurrency limit',
};

function shownRouteValue(value: unknown): string {
  return value === null || value === undefined || value === '' ? '(default)' : String(value);
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

function routeTargetLabel(
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

/**
 * The OpenRouter routing choices for the selector: OpenRouter's own routing
 * (automatic, or sorted by price, throughput or latency) apart from pinning
 * one provider. Same values; only the presentation is grouped.
 */
function groupOpenRouterRouting(options: ReadonlyArray<{ value: string; label: string }>) {
  const pinned = options.filter((option) =>
    option.value.startsWith(OPENROUTER_ROUTING_PROVIDER_PREFIX),
  );
  const routing = options.filter(
    (option) => !option.value.startsWith(OPENROUTER_ROUTING_PROVIDER_PREFIX),
  );
  return [
    ...(routing.length ? [{ label: 'Let OpenRouter choose', options: routing }] : []),
    ...(pinned.length ? [{ label: 'Pin a provider', options: pinned }] : []),
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
  // Whether OpenRouter may carry this route type at all is the provider
  // option's route_types (the deployment's policy), checked by the provider
  // selector; here only the pins already used for the type drop out, and a
  // metered type (quota, concurrency) must name a provider rather than Auto.
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

// Why a control that would drop an unsaved draft is unavailable for now.
const HELD_FOR_DRAFTS = 'Save or discard the unsaved changes first';

export function ProviderRoutesTab({ showRoutewiseSettings = false }: ProviderRoutesTabProps = {}) {
  const toast = useAccountToast();
  const [routes, setRoutes] = useState<ProviderRoute[]>([]);
  const [routeWeights, setRouteWeights] = useState<RouteWeight[]>([]);
  const [draftWeights, setDraftWeights] = useState<Record<string, string>>({});
  const [draftConcurrencyLimits, setDraftConcurrencyLimits] = useState<Record<string, string>>({});
  const [savingWeightKey, setSavingWeightKey] = useState<string | null>(null);
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
  // The route whose capabilities are being edited, and the fields as typed.
  const [capabilityRoute, setCapabilityRoute] = useState<ProviderRoute | null>(null);
  const [capabilityDraft, setCapabilityDraft] = useState<CapabilityDraft>(emptyCapabilityDraft);
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
    deploymentType: 'auto',
    apiKeyId: '',
    providerModelId: '',
    quotaLimit: '',
    concurrencyLimit: '',
  });
  const [createForm, setCreateForm] = useState<CreateRouteForm>(emptyCreateForm);
  const [keyOptions, setKeyOptions] = useState<ProviderApiKeyItem[]>([]);
  const [createKeyOptions, setCreateKeyOptions] = useState<ProviderApiKeyItem[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [createKeysLoading, setCreateKeysLoading] = useState(false);
  const confirmation = useConfirmAction();
  // Fixed weights, the routing policy and a concurrency limit are edited in
  // place; their drafts are written only after the confirmation.
  const [weightsError, setWeightsError] = useState<string | null>(null);
  // The policy chosen for the selected model; null while it is the saved one.
  const [draftStrategy, setDraftStrategy] = useState<ProviderRouteStrategy | null>(null);
  // Route key → why that row's concurrency limit could not be saved.
  const [concurrencyErrors, setConcurrencyErrors] = useState<Record<string, string>>({});
  const [routeEditError, setRouteEditError] = useState<string | null>(null);
  const [verifyingKey, setVerifyingKey] = useState<string | null>(null);
  const [verifyingCreateRoute, setVerifyingCreateRoute] = useState(false);
  const [verifiedEditSignature, setVerifiedEditSignature] = useState<string | null>(null);
  const [verifiedCreateSignature, setVerifiedCreateSignature] = useState<string | null>(null);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [togglingKey, setTogglingKey] = useState<string | null>(null);
  const [offloadRoutes, setOffloadRoutes] = useState<OffloadRoute[]>([]);
  const [offloadQueue, setOffloadQueue] = useState<OffloadQueueInfo | null>(null);
  const [offloadLoadError, setOffloadLoadError] = useState<string | null>(null);

  // Loaded on its own so a failure here costs the offload panel, not the whole
  // routes table. Refreshed after the edits that can change whether a stored
  // offload route is in force: a routing-policy switch and a weight change.
  const loadOffloadRoutes = useCallback(async () => {
    try {
      const resp = await listOffloadRoutes();
      setOffloadRoutes(resp.offload_routes);
      setOffloadQueue({ enabled: resp.queue_enabled, maxWaitSeconds: resp.max_wait_seconds });
      setOffloadLoadError(null);
    } catch (err) {
      setOffloadLoadError(routeErrorMessage(err));
    }
  }, []);

  const onOffloadChange = useCallback((modelId: string, offload: OffloadRoute | null) => {
    setOffloadRoutes((current) => {
      const others = current.filter((item) => item.model_id !== modelId);
      return offload ? [...others, offload] : others;
    });
  }, []);

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
      toast.error(`Failed to load provider routes: ${routeErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void loadRoutes();
    void loadOffloadRoutes();
  }, [loadOffloadRoutes, loadRoutes]);

  const models = useMemo(
    () => Array.from(new Set(routes.map((route) => route.model_id))).sort(),
    [routes],
  );
  const selectedRoutes = useMemo(
    () => routes.filter((route) => route.model_id === selectedModel),
    [routes, selectedModel],
  );
  const selectedQuotaRoutes = useMemo(
    () => selectedRoutes.filter((route) => route.route_type === 'quota'),
    [selectedRoutes],
  );
  const selectedOffload = useMemo(
    () => offloadRoutes.find((item) => item.model_id === selectedModel) ?? null,
    [offloadRoutes, selectedModel],
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
  const editingRuntimeRoute = editingRoute?.source === 'runtime';
  // A runtime quota route owns its local quota; a config route gets one only
  // while an override provider serves it.
  const editsLocalQuota =
    editingRoute?.route_type === 'quota' &&
    (editingRuntimeRoute ||
      form.upstreamProvider !== routePrimaryProvider(editingRoute) ||
      (form.upstreamProvider === 'openrouter' &&
        (selectedOpenRouterProvider ?? OPENROUTER_PROVIDER_AUTO) !==
          routeOpenRouterProvider(editingRoute)));
  const parsedQuotaLimit = editsLocalQuota ? Number.parseInt(form.quotaLimit, 10) : null;
  const quotaLimitValid =
    !editsLocalQuota ||
    (parsedQuotaLimit !== null && Number.isInteger(parsedQuotaLimit) && parsedQuotaLimit > 0);
  const editsConcurrencyLimit = editingRoute?.route_type === 'concurrency';
  const parsedConcurrencyLimit = editsConcurrencyLimit
    ? Number.parseInt(form.concurrencyLimit, 10)
    : null;
  const concurrencyLimitValid =
    !editsConcurrencyLimit ||
    (parsedConcurrencyLimit !== null &&
      Number.isInteger(parsedConcurrencyLimit) &&
      parsedConcurrencyLimit > 0);
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
  const createCapabilityReading = readCapabilityDraft(createForm);
  const runtimeCapabilities = createCapabilityReading.payload;
  // A new model and an added route both declare their own.
  const createCapabilitiesValid = runtimeCapabilities !== null;
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
    createPricingValid &&
    createCapabilitiesValid;
  const editFormValid =
    Boolean(editingRoute) &&
    Boolean(form.upstreamProvider) &&
    Boolean(form.baseUrl.trim()) &&
    Boolean(form.providerModelId.trim()) &&
    formOpenRouterProviderValid &&
    quotaLimitValid &&
    concurrencyLimitValid;
  const editDeploymentType: RouteDeploymentType =
    form.deploymentType ??
    (editingRoute &&
    form.upstreamProvider === routePrimaryUpstreamProvider(editingRoute) &&
    (form.upstreamProvider !== 'openrouter' ||
      selectedOpenRouterProvider ===
        resolvedOpenRouterProvider(routeOpenRouterProvider(editingRoute), '')) &&
    canonicalEndpointUrl(form.baseUrl) === canonicalEndpointUrl(editingRoute.base_url)
      ? (editingRoute.deployment_type ?? 'auto')
      : 'auto');
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
      ...(form.deploymentType ? { deployment_type: form.deploymentType } : {}),
      api_key_id: form.apiKeyId || null,
      provider_model_id: form.providerModelId.trim(),
      quota_limit: editsLocalQuota ? parsedQuotaLimit : null,
      concurrency_limit: editsConcurrencyLimit ? parsedConcurrencyLimit : null,
    };
  }, [
    editsConcurrencyLimit,
    editingRoute,
    editsLocalQuota,
    form.apiKeyId,
    form.baseUrl,
    form.deploymentType,
    form.openRouterProvider,
    form.openRouterSort,
    form.providerModelId,
    form.upstreamProvider,
    parsedConcurrencyLimit,
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
      deployment_type: createForm.deploymentType,
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
      createForm.deploymentType,
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
    capabilities: runtimeCapabilities,
    payload: createRoutePayload,
  });
  const editRouteVerified =
    Boolean(editVerificationSignature) && verifiedEditSignature === editVerificationSignature;
  const createRouteVerified = verifiedCreateSignature === createVerificationSignature;

  // The fields an Apply would change, `old → new`. A limit this edit does not
  // own is sent as null ("leave it"), so it is never listed as a change.
  const routeEditChanges: Change[] = (() => {
    if (!editingRoute || !editRoutePayload) return [];
    const current: Record<string, unknown> = {
      ...editingRoute,
      upstream_provider: routePrimaryUpstreamProvider(editingRoute),
      openrouter_provider: routeOpenRouterProvider(editingRoute) || null,
    };
    const apiKeyLabel = (id: unknown) => {
      const option = keyOptions.find((candidate) => candidate.id === id);
      return option ? (option.label ?? option.key_prefix) : id;
    };
    return Object.entries(editRoutePayload)
      .filter(([field]) => field !== 'quota_limit' || editsLocalQuota)
      .filter(([field]) => field !== 'concurrency_limit' || editsConcurrencyLimit)
      .filter(([field, value]) => shownRouteValue(current[field]) !== shownRouteValue(value))
      .map(([field, value]) => ({
        label: ROUTE_EDIT_FIELD_LABELS[field] ?? field,
        before: shownRouteValue(
          field === 'api_key_id' ? apiKeyLabel(current[field]) : current[field],
        ),
        after: shownRouteValue(field === 'api_key_id' ? apiKeyLabel(value) : value),
      }));
  })();

  useEffect(() => {
    if (!editingRoute) {
      setForm({
        upstreamProvider: '',
        openRouterProvider: '',
        customOpenRouterProvider: '',
        openRouterSort: '',
        baseUrl: '',
        deploymentType: 'auto',
        apiKeyId: '',
        providerModelId: '',
        quotaLimit: '',
        concurrencyLimit: '',
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
      deploymentType: null,
      apiKeyId: editingRoute.api_key_id ?? '',
      providerModelId: editingRoute.provider_model_id ?? '',
      quotaLimit: editingRoute.quota_limit ? String(editingRoute.quota_limit) : '',
      concurrencyLimit: editingRoute.concurrency_limit
        ? String(editingRoute.concurrency_limit)
        : '',
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
        : '',
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
    // A runtime route keeps its type, so it may only move to a provider the
    // deployment allows that type for.
    const baseOptions =
      editingRoute?.source === 'runtime'
        ? providerSelectBaseOptions.filter((option) =>
            optionSupportsRouteType(option, editingRoute.route_type as ProviderRouteType),
          )
        : providerSelectBaseOptions;
    if (!form.upstreamProvider || optionFor(baseOptions, form.upstreamProvider)) {
      return baseOptions;
    }
    return [
      ...baseOptions,
      {
        provider: form.upstreamProvider,
        label: form.upstreamProvider,
        kind: form.upstreamProvider,
        key_provider: form.upstreamProvider,
        default_base_url: form.baseUrl,
      },
    ];
  }, [editingRoute, form.baseUrl, form.upstreamProvider, providerSelectBaseOptions]);

  const loadKeys = useCallback(
    async (provider: string) => {
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
        toast.error(`Failed to load provider keys: ${routeErrorMessage(err)}`);
      } finally {
        setKeysLoading(false);
      }
    },
    [toast],
  );

  const loadCreateKeys = useCallback(
    async (provider: string) => {
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
        toast.error(`Failed to load provider keys: ${routeErrorMessage(err)}`);
      } finally {
        setCreateKeysLoading(false);
      }
    },
    [toast],
  );

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
        deploymentType: 'auto',
        apiKeyId: '',
        providerModelId: '',
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
      providerModelId: defaultProviderModelIdFor(nextProvider.provider, createTargetRoutes),
    }));
  }, [
    createFormOpen,
    createForm.upstreamProvider,
    createOpenRouterProviderOptions,
    createProviderOptions,
    createTargetRoutes,
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
      deploymentType: upstreamProvider === current.upstreamProvider ? current.deploymentType : null,
      apiKeyId: '',
      providerModelId:
        editingRoute && upstreamProvider === routePrimaryUpstreamProvider(editingRoute)
          ? (editingRoute.provider_model_id ?? current.providerModelId)
          : defaultProviderModelIdFor(upstreamProvider, selectedRoutes),
      quotaLimit: current.quotaLimit,
      concurrencyLimit: current.concurrencyLimit,
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
      // A starting point, not a copy: the admin confirms or changes each value
      // for this provider, and the route stores what was submitted.
      ...(selectedRoutes[0]?.capabilities
        ? capabilityDraftFrom(selectedRoutes[0].capabilities)
        : emptyCapabilityDraft),
    });
    setCreateKeyOptions([]);
    setVerifiedCreateSignature(null);
    setAddingRoute(true);
    setCreatingModel(false);
    setEditingRoute(null);
    setCapabilityRoute(null);
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
    setCapabilityRoute(null);
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
      providerModelId: defaultProviderModelIdFor(upstreamProvider, createTargetRoutes),
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
        : '',
    }));
  };

  const closeRouteEdit = () => {
    if (confirmation.busy) return;
    setRouteEditError(null);
    setEditingRoute(null);
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingRoute || !editRoutePayload || routeEditChanges.length === 0) return;
    if (!editFormValid) {
      setRouteEditError(
        !quotaLimitValid
          ? 'Local daily quota must be a positive whole number.'
          : !concurrencyLimitValid
            ? 'Local concurrency limit must be a positive whole number.'
            : !formOpenRouterProviderValid
              ? 'Enter a valid custom OpenRouter provider slug.'
              : 'Fill in the provider, base URL and provider model ID.',
      );
      return;
    }
    setRouteEditError(null);
    const target = editingRoute;
    const payload = editRoutePayload;
    const changes = routeEditChanges;
    confirmation.request({
      title: `Save ${plural(changes.length, 'change')} to ${target.model_id} / ${target.provider}?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={changes} />
          <p>
            The gateway checks the new target with one short upstream request, then routes new
            requests through it.
          </p>
        </>
      ),
      run: async () => {
        let updated: ProviderRoute;
        try {
          updated =
            target.source === 'runtime'
              ? await updateProviderRouteCandidate(target.model_id, target.route_id, payload)
              : await updateProviderRoute(target.model_id, target.route_id, payload);
        } catch (err) {
          throw new Error(routeErrorMessage(err));
        }
        if (updated.endpoint_id !== target.endpoint_id) refreshAfterRouteChange();
        updateRoute(updated);
        setEditingRoute(null);
        toast.success('Provider route updated');
      },
    });
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
      toast.error(`Verify failed: ${routeErrorMessage(err)}`);
    } finally {
      setVerifyingKey(null);
    }
  };

  // What a Create/Add will register, for the review before the write.
  const createReview = (): Change[] => {
    const review: Change[] = [];
    if (creatingModel) {
      review.push(
        { label: 'Model ID', after: newModelIdValue },
        { label: 'Routing policy', after: newModelStrategy },
        {
          label: 'Visibility',
          after:
            MODEL_ROLE_OPTIONS.find((option) => option.value === newModelRequiredRole)?.label ??
            newModelRequiredRole,
        },
        { label: 'Prompt $/M', after: runtimePricing.prompt },
        { label: 'Completion $/M', after: runtimePricing.completion },
      );
      if (runtimePricing.input_cache_reads) {
        review.push({ label: 'Cache read $/M', after: runtimePricing.input_cache_reads });
      }
      if (runtimePricing.input_cache_writes) {
        review.push({ label: 'Cache write $/M', after: runtimePricing.input_cache_writes });
      }
    }
    if (runtimeCapabilities) {
      // A new model and an added route both declare their own.
      review.push(...capabilityRows(runtimeCapabilities));
    }
    review.push(
      { label: 'Route type', after: createRoutePayload.route_type },
      {
        label: 'Provider',
        after: providerLabel(createProviderOptions, createRoutePayload.upstream_provider),
      },
    );
    if (createRoutePayload.upstream_provider === 'openrouter') {
      review.push({
        label: 'OpenRouter routing',
        after: createRoutePayload.openrouter_provider
          ? `Provider: ${openRouterProviderLabel(
              createOpenRouterSelectOptions,
              createRoutePayload.openrouter_provider,
            )}`
          : createRoutePayload.openrouter_sort
            ? `Sort by ${createRoutePayload.openrouter_sort}`
            : 'Auto',
      });
    }
    const apiKey = createKeyOptions.find((key) => key.id === createRoutePayload.api_key_id);
    review.push(
      { label: 'Base URL', after: createRoutePayload.base_url },
      { label: 'Deployment', after: createRoutePayload.deployment_type },
      {
        label: 'API key',
        after: apiKey
          ? `${apiKey.label ? `${apiKey.label} · ` : ''}${apiKey.key_prefix}`
          : defaultKeyOptionLabel(createKeyProvider),
      },
      { label: 'Provider model ID', after: createRoutePayload.provider_model_id },
    );
    if (createRoutePayload.quota_limit !== null) {
      review.push({ label: 'Local daily quota', after: String(createRoutePayload.quota_limit) });
    }
    if (createRoutePayload.concurrency_limit !== null) {
      review.push({
        label: 'Concurrency limit',
        after: String(createRoutePayload.concurrency_limit),
      });
    }
    if (showCreateWeight) {
      review.push({ label: 'Fixed weight', after: String(createRoutePayload.weight) });
    }
    return review;
  };

  const onCreateSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    // createFormValid already requires it; this check narrows the type.
    if (!createFormValid || !runtimeCapabilities) return;
    const model = creatingModel;
    const capabilities = runtimeCapabilities;
    // A new model and an added route both declare their own capabilities.
    const routePayload = { ...createRoutePayload, capabilities };
    const modelPayload = model
      ? {
          ...createRoutePayload,
          model_id: newModelIdValue,
          strategy: newModelStrategy,
          required_role: newModelRequiredRole,
          pricing: runtimePricing,
          capabilities,
        }
      : null;
    const targetModel = selectedModel;
    confirmation.request({
      title: model ? `Create model ${newModelIdValue}?` : `Add a provider route to ${targetModel}?`,
      confirmLabel: model ? 'Create' : 'Add',
      busyLabel: model ? 'Creating…' : 'Adding…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={createReview()} />
          <p>
            {model
              ? 'The gateway checks the route with one short upstream request, then registers the model and starts routing to it.'
              : 'The gateway checks the route with one short upstream request, then routes new requests through it.'}
          </p>
        </>
      ),
      run: async () => {
        const created = modelPayload
          ? await createProviderRouteModel(modelPayload)
          : await createProviderRouteCandidate(targetModel, routePayload);
        setRoutes((current) => [...current, created]);
        if (model) {
          setSelectedModel(created.model_id);
          resetInlineDrafts();
        }
        setAddingRoute(false);
        setCreatingModel(false);
        setVerifiedCreateSignature(null);
        toast.success(model ? 'Model created' : 'Provider route added');
      },
    });
  };

  const onCreateVerify = async () => {
    if (!createFormValid) return;
    // createFormValid already requires it; this check narrows the type.
    if (!runtimeCapabilities) return;
    setVerifyingCreateRoute(true);
    try {
      if (creatingModel) {
        await verifyProviderRouteModel({
          ...createRoutePayload,
          model_id: newModelIdValue,
          strategy: newModelStrategy,
          required_role: newModelRequiredRole,
          pricing: runtimePricing,
          capabilities: runtimeCapabilities,
        });
      } else {
        await verifyProviderRouteCandidate(selectedModel, {
          ...createRoutePayload,
          capabilities: runtimeCapabilities,
        });
      }
      setVerifiedCreateSignature(createVerificationSignature);
      toast.success(creatingModel ? 'Model route verified' : 'Provider route verified');
    } catch (err) {
      setVerifiedCreateSignature(null);
      toast.error(`Verify failed: ${routeErrorMessage(err)}`);
    } finally {
      setVerifyingCreateRoute(false);
    }
  };

  const routeTarget = (route: ProviderRoute) => `${route.model_id} / ${route.provider}`;
  const weightKeyFor = (route: ProviderRoute) =>
    routeWeightKey({ model_id: route.model_id, endpoint_id: route.endpoint_id });
  const effectiveWeightFor = (route: ProviderRoute) =>
    routeWeightByKey.get(weightKeyFor(route))?.effective_weight ?? route.effective_weight;
  const dropWeightDraft = (key: string) =>
    setDraftWeights((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  const dropConcurrencyDraft = (key: string) => {
    setDraftConcurrencyLimits((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    setConcurrencyErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  };
  // Drafts belong to the model they were made for. The model can only switch
  // once none of them is a change, so this drops nothing unsaved.
  const resetInlineDrafts = () => {
    setDraftStrategy(null);
    setDraftWeights({});
    setWeightsError(null);
    setDraftConcurrencyLimits({});
    setConcurrencyErrors({});
  };

  // Fixed weights: only real changes count, and an invalid draft is one, so
  // Save can say what to fix. A deleted route shows no weight to edit.
  const weightChanges = isRoutewise
    ? []
    : selectedRoutes.flatMap((route) => {
        if (route.removed) return [];
        const key = weightKeyFor(route);
        const draft = draftWeights[key];
        if (draft === undefined) return [];
        const before = effectiveWeightFor(route);
        const parsed = parseWeightDraft(draft);
        if (parsed !== null && parsed === before) return [];
        return [{ route, key, before, parsed }];
      });

  // A runtime OpenRouter concurrency limit, edited in its own row.
  const concurrencyDraftFor = (route: ProviderRoute) => {
    const draft = draftConcurrencyLimits[routeKey(route)];
    if (draft === undefined) return { changed: false, parsed: route.concurrency_limit };
    const trimmed = draft.trim();
    if (trimmed === '' && route.concurrency_limit == null) return { changed: false, parsed: null };
    const parsed = /^\d+$/.test(trimmed) ? Number.parseInt(trimmed, 10) : null;
    return { changed: parsed === null || parsed !== route.concurrency_limit, parsed };
  };
  const limitsChanged =
    isRoutewise &&
    selectedRoutes.some(
      (route) =>
        !route.removed && canEditConcurrencyLimit(route) && concurrencyDraftFor(route).changed,
    );

  const weightsChanged = weightChanges.length > 0;
  const strategyChanged = draftStrategy !== null && draftStrategy !== strategy;
  const inlineChanged = weightsChanged || strategyChanged || limitsChanged;
  // The offload and RouteWise panels keep drafts for the selected model too, and
  // drop them when it changes; they report whether a draft or confirmation is open.
  const [panelEditing, setPanelEditing] = useState({ offload: false, routewise: false });
  const onOffloadEditingChange = useCallback(
    (active: boolean) => setPanelEditing((prev) => ({ ...prev, offload: active })),
    [],
  );
  const onRoutewiseEditingChange = useCallback(
    (active: boolean) => setPanelEditing((prev) => ({ ...prev, routewise: active })),
    [],
  );
  const panelOpen = panelEditing.offload || panelEditing.routewise;
  // Switching model, directly or by creating one, would drop any unsaved draft.
  // The route, capability and create dialogs are modal, but their drafts belong
  // to this model too, as does any open confirmation. Unchanged fields hold
  // nothing back.
  const modelLocked =
    inlineChanged ||
    panelOpen ||
    editingRoute !== null ||
    capabilityRoute !== null ||
    createFormOpen ||
    confirmation.open;
  // So would switching the routing policy: it unmounts the panel of the current
  // policy (Queue offload for fixed, RouteWise parameters for routewise) and
  // swaps the table's weights for limits or back.
  const policyHeld = weightsChanged || limitsChanged || panelOpen;

  // Discrete row actions. Each `run` throws on failure, so the confirmation
  // stays open with the error and the same action can be retried.
  const confirmResetYaml = (route: ProviderRoute) =>
    confirmation.request({
      title: `Reset ${routeTarget(route)}?`,
      body: (
        <p>
          The changes made in the console are removed, and the route goes back to the values in the
          configuration file.
        </p>
      ),
      confirmLabel: 'Reset',
      busyLabel: 'Resetting…',
      tone: 'danger',
      run: async () => {
        const key = routeKey(route);
        setResettingKey(key);
        try {
          const updated = await deleteProviderRoute(route.model_id, route.route_id);
          updateRoute(updated);
          setEditingRoute((current) => (current && routeKey(current) === key ? null : current));
          toast.success('Reset route to config');
        } finally {
          setResettingKey(null);
        }
      },
    });

  const refreshAfterRouteChange = () => {
    void listRouteWeights()
      .then(setRouteWeights)
      .catch(() => undefined);
    void loadOffloadRoutes();
  };

  const confirmDeleteRoute = (route: ProviderRoute) => {
    if (selectedOffload?.route_id === route.route_id) {
      toast.error(
        `${route.route_id} is this model's offload route. Clear the offload route first, then delete it.`,
      );
      return;
    }
    const runtime = route.source === 'runtime';
    const lastRoute = selectedRoutes.filter((item) => !item.removed).length === 1;
    confirmation.request({
      title: `Delete ${runtime ? 'runtime' : 'config'} route ${route.route_id}?`,
      body: (
        <p>
          {runtime
            ? lastRoute
              ? `It is the last route of ${route.model_id}, so the model is deleted too. This cannot be undone.`
              : 'It stops serving now and its saved configuration is deleted from the database. This cannot be undone: add the route again to bring it back.'
            : 'It stops serving now and stays deleted after restarts; models.yaml is not changed. You can bring it back with Restore, together with its edits and weight. Reset config is different: it only discards edits.'}
        </p>
      ),
      confirmLabel: 'Delete',
      busyLabel: 'Deleting…',
      tone: 'danger',
      run: async () => {
        const key = routeKey(route);
        setDeletingKey(key);
        try {
          const updated = runtime
            ? await deleteProviderRouteCandidate(route.model_id, route.route_id)
            : await removeProviderRoute(route.model_id, route.route_id);
          replaceModelRoutes(updated.model_id ?? route.model_id, updated.routes);
          setEditingRoute((current) => (current && routeKey(current) === key ? null : current));
          // A deleted route has no weight or limit to edit; Restore starts it afresh.
          dropWeightDraft(weightKeyFor(route));
          dropConcurrencyDraft(key);
          toast.success(
            runtime ? 'Provider route deleted' : 'Config route deleted; Restore brings it back',
          );
          refreshAfterRouteChange();
        } catch (err) {
          throw new Error(routeErrorMessage(err));
        } finally {
          setDeletingKey(null);
        }
      },
    });
  };

  const confirmSetRouteDisabled = (route: ProviderRoute, disabled: boolean) =>
    confirmation.request({
      title: `${disabled ? 'Disable' : 'Enable'} ${routeTarget(route)}?`,
      body: (
        <p>
          {disabled
            ? 'The route receives no new requests. Its configuration and weight are preserved.'
            : 'The route is enabled again. A runtime weight override of 0 is cleared.'}
        </p>
      ),
      confirmLabel: disabled ? 'Disable' : 'Enable',
      busyLabel: disabled ? 'Disabling…' : 'Enabling…',
      tone: disabled ? 'danger' : 'default',
      run: async () => {
        setTogglingKey(routeKey(route));
        try {
          const updated = await updateProviderRouteState(route.model_id, route.route_id, {
            disabled,
          });
          updateRoute(updated);
          toast.success(`${disabled ? 'Disabled' : 'Enabled'} ${route.route_id}.`);
          refreshAfterRouteChange();
        } catch (err) {
          throw new Error(routeErrorMessage(err));
        } finally {
          setTogglingKey(null);
        }
      },
    });

  const confirmRestoreRoute = (route: ProviderRoute) =>
    confirmation.request({
      title: `Restore ${routeTarget(route)}?`,
      body: (
        <p>The config route is restored together with its saved edits, weight and enabled state.</p>
      ),
      confirmLabel: 'Restore',
      busyLabel: 'Restoring…',
      run: async () => {
        setDeletingKey(routeKey(route));
        try {
          const updated = await restoreProviderRoute(route.model_id, route.route_id);
          replaceModelRoutes(updated.model_id ?? route.model_id, updated.routes);
          toast.success(`Restored ${route.route_id}.`);
          refreshAfterRouteChange();
        } catch (err) {
          throw new Error(routeErrorMessage(err));
        } finally {
          setDeletingKey(null);
        }
      },
    });

  const confirmClearWeight = (route: ProviderRoute) => {
    const key = weightKeyFor(route);
    const yamlWeight = routeWeightByKey.get(key)?.yaml_weight ?? route.yaml_weight;
    confirmation.request({
      title: `Reset ${route.endpoint_id} weight?`,
      body: (
        <>
          <ChangeList
            changes={[
              {
                label: 'Weight',
                before: formatWeight(effectiveWeightFor(route)),
                after: `${formatWeight(yamlWeight)} (configured)`,
              },
            ]}
          />
          <p>The runtime override is removed and the route goes back to its configured weight.</p>
        </>
      ),
      confirmLabel: 'Reset',
      busyLabel: 'Resetting…',
      tone: 'danger',
      run: async () => {
        setSavingWeightKey(key);
        try {
          const updated = await clearRouteWeight(route.model_id, route.endpoint_id);
          replaceRouteWeight(updated);
          dropWeightDraft(key);
          toast.success(`Cleared ${route.endpoint_id} weight override.`);
          void loadOffloadRoutes();
        } finally {
          setSavingWeightKey(null);
        }
      },
    });
  };

  // A route's capabilities: the dialog's fields are a draft, Save reviews
  // `old → new`, and only Confirm writes.
  const openCapabilityEditor = (route: ProviderRoute) => {
    if (!route.capabilities) return;
    setCapabilityRoute(route);
    setCapabilityDraft(capabilityDraftFrom(route.capabilities));
    setEditingRoute(null);
    setAddingRoute(false);
    setCreatingModel(false);
  };

  const closeCapabilityEditor = () => {
    if (confirmation.busy) return;
    setCapabilityRoute(null);
  };

  const capabilityReading = readCapabilityDraft(capabilityDraft);
  const capabilityEditChanges: Change[] =
    capabilityRoute?.capabilities && capabilityReading.payload
      ? capabilityChanges(capabilityRoute.capabilities, capabilityReading.payload)
      : [];

  const onSaveCapabilities = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!capabilityRoute || !capabilityReading.payload || capabilityEditChanges.length === 0) {
      return;
    }
    const target = capabilityRoute;
    const payload = capabilityReading.payload;
    const changes = capabilityEditChanges;
    confirmation.request({
      title: `Save ${plural(changes.length, 'change')} to ${target.endpoint_id} capabilities?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={changes} />
          <p>
            Requests through this route follow the new values from the next request on, until they
            are reset to config.
          </p>
        </>
      ),
      run: async () => {
        const updated = await updateProviderRouteCapabilities(
          target.model_id,
          target.route_id,
          payload,
        ).catch((err: unknown) => {
          throw new Error(routeErrorMessage(err));
        });
        updateRoute(updated);
        setCapabilityRoute(null);
        toast.success('Route capabilities updated');
      },
    });
  };

  const confirmResetCapabilities = () => {
    if (!capabilityRoute) return;
    const target = capabilityRoute;
    const changes =
      target.capabilities && target.capabilities_baseline
        ? capabilityChanges(target.capabilities, target.capabilities_baseline)
        : [];
    confirmation.request({
      title: `Reset ${target.endpoint_id} capabilities to config?`,
      confirmLabel: 'Reset',
      busyLabel: 'Resetting…',
      tone: 'danger',
      body: (
        <>
          {changes.length > 0 ? <ChangeList changes={changes} /> : null}
          <p>
            The values set in the console are removed, and the route goes back to the capabilities
            it has from its configuration.
          </p>
        </>
      ),
      run: async () => {
        const updated = await resetProviderRouteCapabilities(
          target.model_id,
          target.route_id,
        ).catch((err: unknown) => {
          throw new Error(routeErrorMessage(err));
        });
        updateRoute(updated);
        setCapabilityRoute(null);
        toast.success('Route capabilities reset');
      },
    });
  };

  // Routing policy: chosen in place, then one reviewed change.
  const discardStrategy = () => {
    if (confirmation.busy) return;
    setDraftStrategy(null);
  };

  const saveStrategy = () => {
    const model = selectedModel;
    const before = strategy;
    const next = draftStrategy;
    if (!model || next === null || next === before || policyHeld) return;
    confirmation.request({
      key: `${model} routing policy`,
      title: `Save 1 change to ${model} routing policy?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={[{ label: 'Routing policy', before, after: next }]} />
          <p>New requests for {model} are routed by the new policy as soon as it is saved.</p>
        </>
      ),
      run: async () => {
        const updated = await updateProviderRouteStrategy(model, next);
        replaceModelRoutes(updated.model_id ?? model, updated.routes);
        setEditingRoute(null);
        // Nothing else was changed (the save waits until it is not), so the
        // controls the new policy shows start from what is saved.
        resetInlineDrafts();
        toast.success('Routing policy updated');
        void loadOffloadRoutes();
      },
    });
  };

  const discardWeights = () => {
    if (confirmation.busy) return;
    setDraftWeights({});
    setWeightsError(null);
  };

  const saveWeights = () => {
    if (weightChanges.length === 0) return;
    if (weightChanges.some((change) => change.parsed === null)) {
      setWeightsError('Weight must be a non-negative number.');
      return;
    }
    setWeightsError(null);
    const model = selectedModel;
    const pending = weightChanges.map((change) => ({ ...change, weight: change.parsed as number }));
    confirmation.request({
      key: `${model} weights`,
      title: `Save ${plural(pending.length, 'change')} to ${model} weights?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={pending.map(({ route, before, weight }) => ({
              label: route.endpoint_id,
              before: formatWeight(before),
              after: formatWeight(weight),
            }))}
          />
          <p>
            Each route&apos;s share of new requests follows its weight; a weight of 0 sends it none.
          </p>
        </>
      ),
      run: async () => {
        const failure = await writeEach(
          pending,
          ({ route }) => route.endpoint_id,
          async ({ route, key, weight }) => {
            const updated = await setRouteWeight(route.model_id, route.endpoint_id, weight);
            replaceRouteWeight(updated);
            dropWeightDraft(key);
          },
        );
        void loadOffloadRoutes();
        setWeightsError(failure);
        if (failure) return;
        toast.success(
          pending.length === 1
            ? `Updated ${pending[0].route.endpoint_id} weight.`
            : `Updated ${pending.length} weights.`,
        );
      },
    });
  };

  // Each row's limit is saved, or discarded, on its own; another row's draft is
  // never touched.
  const discardConcurrencyLimit = (route: ProviderRoute) => {
    if (confirmation.busy) return;
    dropConcurrencyDraft(routeKey(route));
  };

  const saveConcurrencyLimit = (route: ProviderRoute) => {
    const key = routeKey(route);
    const { changed, parsed } = concurrencyDraftFor(route);
    if (!changed) return;
    if (parsed === null || parsed < 1) {
      setConcurrencyErrors((current) => ({
        ...current,
        [key]: 'Concurrency limit must be a positive integer.',
      }));
      return;
    }
    setConcurrencyErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    confirmation.request({
      key: `${route.endpoint_id} concurrency limit`,
      title: `Save 1 change to ${route.endpoint_id} concurrency limit?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={[
              {
                label: 'Concurrency limit',
                before: shownRouteValue(route.concurrency_limit),
                after: String(parsed),
              },
            ]}
          />
          <p>RouteWise keeps at most this many requests open on the route at once.</p>
        </>
      ),
      run: async () => {
        const updated = await updateProviderRouteCandidate(route.model_id, route.route_id, {
          concurrency_limit: parsed,
        });
        updateRoute(updated);
        dropConcurrencyDraft(key);
        toast.success('Concurrency limit updated');
      },
    });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <label className="text-[12px] font-medium text-gray-500" htmlFor="provider-route-model">
            Selected model
          </label>
          <Select
            id="provider-route-model"
            value={selectedModel}
            onValueChange={(next) => {
              setSelectedModel(next);
              resetInlineDrafts();
              setEditingRoute(null);
              setCapabilityRoute(null);
              setAddingRoute(false);
              setCreatingModel(false);
            }}
            options={models.map((model) => ({ value: model, label: model }))}
            placeholder={models.length === 0 ? '(no models loaded)' : 'Choose a model'}
            searchable
            searchPlaceholder="Search models…"
            emptyText="No models match"
            // Unsaved weight, policy, limit, capability, offload or RouteWise drafts
            // belong to this model; save or discard them before switching.
            disabled={modelLocked}
            title={modelLocked ? HELD_FOR_DRAFTS : undefined}
            size="lg"
            className="mt-1 w-full min-w-[240px] max-w-sm"
          />
        </div>
        <div className="flex items-center gap-2">
          {loading && (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-gray-200 border-t-gray-900" />
          )}
          <label className="text-[13px] text-gray-500" htmlFor="provider-route-strategy">
            Routing policy
          </label>
          <Select<ProviderRouteStrategy>
            id="provider-route-strategy"
            value={draftStrategy ?? (strategy === 'routewise' ? 'routewise' : 'fixed')}
            // A draft only; Save changes reviews it and Confirm writes.
            onValueChange={(next) => setDraftStrategy(next === strategy ? null : next)}
            // Held while another routing draft is unsaved, since a switch would
            // drop it. A policy chosen first stays changeable; its Save waits.
            disabled={
              !selectedModel || loading || confirmation.busy || (!strategyChanged && policyHeld)
            }
            title={!strategyChanged && policyHeld ? HELD_FOR_DRAFTS : undefined}
            options={[
              { value: 'routewise', label: 'routewise' },
              { value: 'fixed', label: 'fixed' },
            ]}
            size="md"
            className={`w-32 font-medium ${strategyChanged ? '!border-amber-400' : ''}`}
          />
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
            disabled={loading || providerOptions.length === 0 || modelLocked}
            title={modelLocked ? HELD_FOR_DRAFTS : undefined}
            className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Create model
          </button>
        </div>
      </div>

      {selectedModel ? (
        <EditBar
          target={`${selectedModel} routing policy`}
          changeCount={strategyChanged ? 1 : 0}
          onCancel={discardStrategy}
          onSave={saveStrategy}
          // Several bars can be up at once; only the one being saved reads "Saving…".
          busy={confirmation.savingKey === `${selectedModel} routing policy`}
          // A weight, limit or panel draft made after the policy was chosen
          // would be dropped by the switch.
          blocked={
            strategyChanged && policyHeld ? 'Save or discard the other unsaved changes first' : null
          }
          inPlace
        />
      ) : null}

      {selectedModel && selectedRoutes.length > 0 && (!isRoutewise || selectedOffload) && (
        // RouteWise does not use offload routes, and the console refuses to switch
        // a model that has one. The panel still appears for a RouteWise model
        // when a policy is stored for it anyway (its registry `router:` changed),
        // so the leftover can be seen and cleared.
        <OffloadRoutePanel
          modelId={selectedModel}
          routes={selectedRoutes}
          offload={selectedOffload}
          queue={offloadQueue}
          editable={!isRoutewise}
          loadError={offloadLoadError}
          routeLabel={(route) =>
            `${routeTargetLabel(route, providerSelectBaseOptions, openRouterSelectOptions)} (${
              route.endpoint_id
            })`
          }
          onChange={onOffloadChange}
          onEditingChange={onOffloadEditingChange}
        />
      )}

      {showRoutewiseSettings && isRoutewise && (
        <RoutewiseSettingsPanel
          modelId={selectedModel}
          endpoints={selectedRoutes.map((route) => ({
            endpointId: route.endpoint_id,
            label: `${route.route_type} · ${routeTargetLabel(
              route,
              providerSelectBaseOptions,
              openRouterSelectOptions,
            )}`,
          }))}
          onEditingChange={onRoutewiseEditingChange}
        />
      )}

      {isRoutewise && (
        <RoutewiseDecisionsPanel modelId={selectedModel} quotaRoutes={selectedQuotaRoutes} />
      )}

      {!isRoutewise && selectedRoutes.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="text-[12px] text-gray-500">
            Fixed weights decide each route&apos;s share of {selectedModel}&apos;s requests.
          </p>
        </div>
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
        <div
          className={`overflow-hidden rounded-lg border bg-white text-[13px] ${
            weightsChanged || limitsChanged ? 'border-gray-400 shadow-sm' : 'border-gray-200'
          }`}
        >
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
              const draftWeightInvalid = parseWeightDraft(draftWeight) === null;
              const weightChanged = weightChanges.some((change) => change.key === weightKey);
              const isSavingWeight = savingWeightKey === weightKey;
              const hasWeightOverride = routeWeight?.override_weight != null;
              const routeRemoved = route.removed === true;
              // Disabled by its flag, or by a weight of 0 (how Disable worked
              // before the flag): either way routing sends it nothing.
              const routeDisabled = route.disabled === true || effectiveWeight <= 0;
              const canDisableRoute = !routeRemoved && !routeDisabled;
              // A weight of 0 from models.yaml is not something Enable can undo.
              const canEnableRoute =
                !routeRemoved && (route.disabled === true || routeWeight?.override_weight === 0);
              const isToggling = togglingKey === key;
              const editableConcurrencyLimit = canEditConcurrencyLimit(route);
              const draftConcurrencyLimit =
                draftConcurrencyLimits[key] ?? String(route.concurrency_limit ?? '');
              const concurrencyLimitChanged = concurrencyDraftFor(route).changed;
              const isOffloadRoute = selectedOffload?.route_id === route.route_id;
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
                    {routeRemoved ? (
                      <div>—</div>
                    ) : isRoutewise ? (
                      editableConcurrencyLimit ? (
                        <div>
                          <input
                            aria-label={`Concurrency limit for ${route.endpoint_id}`}
                            className={`h-8 w-20 rounded-md border px-2 text-right text-[13px] text-gray-900 ${
                              concurrencyLimitChanged ? 'border-amber-400' : 'border-gray-300'
                            }`}
                            disabled={confirmation.busy}
                            min={1}
                            step={1}
                            type="number"
                            value={draftConcurrencyLimit}
                            onChange={(event) => {
                              const value = event.target.value;
                              // Typed back to what is saved, it is no draft: a
                              // refresh of the saved limit shows through.
                              setDraftConcurrencyLimits((current) => {
                                const next = { ...current };
                                if (value === String(route.concurrency_limit ?? '')) {
                                  delete next[key];
                                } else next[key] = value;
                                return next;
                              });
                            }}
                          />
                          <EditBar
                            target={`${route.endpoint_id} concurrency limit`}
                            changeCount={concurrencyLimitChanged ? 1 : 0}
                            onCancel={() => discardConcurrencyLimit(route)}
                            onSave={() => saveConcurrencyLimit(route)}
                            busy={
                              confirmation.savingKey === `${route.endpoint_id} concurrency limit`
                            }
                            error={concurrencyErrors[key] ?? null}
                            inPlace
                          />
                        </div>
                      ) : route.route_type === 'quota' ? (
                        <RouteQuotaCell route={route} />
                      ) : (
                        <div>{routeLimitLabel(route, isRoutewise)}</div>
                      )
                    ) : (
                      <div className="space-y-2">
                        <div className="text-[11px] leading-4 text-gray-400">
                          YAML {formatWeight(yamlWeight)} · Effective{' '}
                          {formatWeight(effectiveWeight)}
                        </div>
                        <div>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <input
                              aria-label={`Runtime weight for ${route.endpoint_id}`}
                              aria-invalid={draftWeightInvalid || undefined}
                              className={`h-8 w-20 rounded-md border px-2 text-right text-[13px] text-gray-900 ${
                                draftWeightInvalid
                                  ? 'border-red-400'
                                  : weightChanged
                                    ? 'border-amber-400'
                                    : 'border-gray-300'
                              }`}
                              disabled={confirmation.busy}
                              min={0}
                              step={0.1}
                              type="number"
                              value={draftWeight}
                              onChange={(event) => {
                                const value = event.target.value;
                                // Typed back to the effective weight, it is no
                                // draft: a refresh (a Reset, say) shows through.
                                setDraftWeights((current) => {
                                  const next = { ...current };
                                  if (value === formatWeight(effectiveWeight)) {
                                    delete next[weightKey];
                                  } else next[weightKey] = value;
                                  return next;
                                });
                              }}
                            />
                            {hasWeightOverride ? (
                              // The reset replaces the weight this row's draft
                              // was typed against, so it waits for the draft.
                              <button
                                aria-label={`Clear ${route.endpoint_id} weight override`}
                                type="button"
                                disabled={isSavingWeight || weightChanged}
                                title={weightChanged ? HELD_FOR_DRAFTS : undefined}
                                onClick={() => confirmClearWeight(route)}
                                className="h-8 rounded-md border border-gray-300 px-2 text-[12px] font-medium text-gray-600 disabled:opacity-50"
                              >
                                Reset
                              </button>
                            ) : null}
                          </div>
                          {draftWeightInvalid && (
                            <p className="mt-1 text-[11px] text-red-600">
                              Enter a non-negative number.
                            </p>
                          )}
                        </div>
                        {isOffloadRoute && (
                          // The weight still decides whether routing may use the
                          // route at all (0 turns the offload off), but not how
                          // much ordinary traffic it gets: none.
                          <div className="text-[11px] leading-4 text-violet-700">
                            Offload route: gets no ordinary traffic
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <div>
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 lg:hidden">
                      Status
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {(routeRemoved || routeDisabled) && (
                        <span
                          className={
                            routeRemoved
                              ? 'inline-flex rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-700'
                              : 'inline-flex rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                          }
                          title={
                            routeRemoved
                              ? 'Deleted from this deployment: it serves nothing and stays deleted after restarts. Restore brings it back with its edits and weight.'
                              : route.disabled
                                ? 'Disabled: kept with its weight, but no request, retry, fallback or offload goes to it.'
                                : 'Its weight is 0, so routing sends it nothing.'
                          }
                        >
                          {routeRemoved ? 'Deleted' : 'Disabled'}
                        </span>
                      )}
                      <span
                        className={
                          overrideNotApplied(route)
                            ? 'inline-flex rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-medium text-red-700'
                            : route.source === 'override'
                              ? 'inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700'
                              : route.source === 'runtime'
                                ? 'inline-flex rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-700'
                                : 'inline-flex rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600'
                        }
                        title={
                          !routeRemoved && overrideNotApplied(route)
                            ? 'The saved override failed to apply at startup, so the config default shown here is serving. Edit the route or reset its config.'
                            : undefined
                        }
                      >
                        {sourceLabel(route)}
                      </span>
                      {isOffloadRoute && selectedOffload && (
                        <span
                          className={
                            selectedOffload.active
                              ? 'inline-flex rounded bg-violet-50 px-1.5 py-0.5 text-[11px] font-medium text-violet-700'
                              : 'inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700'
                          }
                          title={
                            selectedOffload.active
                              ? `Takes requests that waited ${selectedOffload.wait_seconds}s for a slot or a first token, and serves as the last resort`
                              : (selectedOffload.inactive_reason ?? undefined)
                          }
                          data-testid={`offload-badge-${route.route_id}`}
                        >
                          Offload
                        </span>
                      )}
                      {route.capabilities_overridden && route.capabilities_baseline && (
                        <span
                          className="inline-flex rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-700"
                          title={`Set in the console. From config: ${describeCapabilities(route.capabilities_baseline)}`}
                        >
                          Capabilities edited
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-start justify-end gap-1 justify-self-end">
                    {routeRemoved ? (
                      <button
                        type="button"
                        onClick={() => confirmRestoreRoute(route)}
                        disabled={confirmation.busy}
                        className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100 disabled:opacity-50"
                      >
                        {deletingKey === key ? 'Restoring…' : 'Restore'}
                      </button>
                    ) : (
                      <>
                        {route.source === 'override' && (
                          <button
                            type="button"
                            onClick={() => confirmResetYaml(route)}
                            disabled={confirmation.busy}
                            className="rounded-md px-2 py-1 text-[12px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                          >
                            {resettingKey === key ? 'Resetting...' : 'Reset config'}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => {
                            setEditingRoute(route);
                            setRouteEditError(null);
                            setAddingRoute(false);
                            setCreatingModel(false);
                            setCapabilityRoute(null);
                          }}
                          className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
                        >
                          Edit
                        </button>
                        {canDisableRoute && (
                          <button
                            type="button"
                            onClick={() => confirmSetRouteDisabled(route, true)}
                            disabled={confirmation.busy}
                            className="rounded-md px-2 py-1 text-[12px] font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
                          >
                            {isToggling ? 'Disabling...' : 'Disable'}
                          </button>
                        )}
                        {canEnableRoute && (
                          <button
                            type="button"
                            onClick={() => confirmSetRouteDisabled(route, false)}
                            disabled={confirmation.busy}
                            className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100 disabled:opacity-50"
                          >
                            {isToggling ? 'Enabling...' : 'Enable'}
                          </button>
                        )}
                        {route.capabilities && (
                          <button
                            type="button"
                            aria-label={`Edit ${route.endpoint_id} capabilities`}
                            title={describeCapabilities(route.capabilities)}
                            onClick={() => openCapabilityEditor(route)}
                            className="rounded-md px-2 py-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
                          >
                            Capabilities
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => confirmDeleteRoute(route)}
                          disabled={confirmation.busy}
                          className="rounded-md px-2 py-1 text-[12px] font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
                        >
                          {deletingKey === key ? 'Deleting…' : 'Delete'}
                        </button>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!isRoutewise && (
        <EditBar
          target={`${selectedModel} weights`}
          changeCount={weightChanges.length}
          onCancel={discardWeights}
          onSave={saveWeights}
          busy={confirmation.savingKey === `${selectedModel} weights`}
          error={weightsError}
          sticky
          inPlace
        />
      )}

      {createFormOpen && (
        <div className="fixed inset-0 z-50 overflow-y-auto bg-gray-900/30 px-4 py-8">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-provider-route-title"
            className="mx-auto w-full max-w-5xl rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
          >
            <form onSubmit={onCreateSubmit}>
              <div className="mb-4 flex items-start justify-between gap-3">
                <div>
                  <h3
                    id="create-provider-route-title"
                    className="text-[14px] font-semibold text-gray-900"
                  >
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
                  disabled={confirmation.busy}
                  className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100 disabled:opacity-40"
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
                    <Select<ProviderRouteStrategy>
                      id="new-model-strategy"
                      value={newModelStrategy}
                      onValueChange={(next) => {
                        setNewModelStrategy(next);
                        setVerifiedCreateSignature(null);
                      }}
                      options={[
                        { value: 'fixed', label: 'fixed' },
                        { value: 'routewise', label: 'routewise' },
                      ]}
                      size="lg"
                      className="mt-1 w-full"
                    />
                  </div>
                  <div>
                    <label
                      className="text-[12px] font-medium text-gray-500"
                      htmlFor="new-model-required-role"
                    >
                      Visibility
                    </label>
                    <Select<Role>
                      id="new-model-required-role"
                      value={newModelRequiredRole}
                      onValueChange={(next) => {
                        setNewModelRequiredRole(next);
                        setVerifiedCreateSignature(null);
                      }}
                      options={MODEL_ROLE_OPTIONS}
                      size="lg"
                      className="mt-1 w-full"
                    />
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

              <div className="mb-3">
                <RouteCapabilityFields
                  idPrefix="new-model"
                  draft={createForm}
                  reading={createCapabilityReading}
                  onChange={(change) => {
                    setCreateForm((current) => ({ ...current, ...change }));
                    setVerifiedCreateSignature(null);
                  }}
                  help={
                    creatingModel
                      ? "Take these from the provider's model page. Requests follow them: tool definitions are dropped without tool calling, max_tokens is capped at the max output, and images reach the model only if it accepts them."
                      : "This route's own values, started from the model's first route. Providers can serve the same model with a smaller window or without images or tools; set what this one serves."
                  }
                />
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <label className="text-[12px] font-medium text-gray-500" htmlFor="new-route-type">
                    Route type
                  </label>
                  <Select<ProviderRouteType>
                    id="new-route-type"
                    value={createForm.routeType}
                    onValueChange={onCreateRouteTypeChange}
                    options={createRouteTypeOptions}
                    size="lg"
                    className="mt-1 w-full"
                  />
                </div>
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-provider"
                  >
                    Provider
                  </label>
                  <Select
                    id="new-route-provider"
                    value={createForm.upstreamProvider}
                    onValueChange={onCreateProviderChange}
                    options={createProviderOptions.map((option) => ({
                      value: option.provider,
                      label: option.label,
                    }))}
                    placeholder={
                      createProviderOptions.length === 0
                        ? noProviderOptionLabel(createForm.routeType)
                        : 'Choose a provider'
                    }
                    searchPlaceholder="Search providers…"
                    aria-required
                    size="lg"
                    className="mt-1 w-full"
                  />
                  {createProviderOptions.length === 0 && (
                    <p className="mt-1 text-[11px] leading-5 text-gray-400">
                      {noProviderHelpText(createForm.routeType)}
                    </p>
                  )}
                </div>
                <div>
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="new-route-api-key"
                  >
                    API key
                  </label>
                  <Select
                    id="new-route-api-key"
                    value={createForm.apiKeyId}
                    onValueChange={(apiKeyId) =>
                      setCreateForm((current) => ({ ...current, apiKeyId }))
                    }
                    disabled={createKeysLoading}
                    options={[
                      { value: '', label: defaultKeyOptionLabel(createKeyProvider) },
                      ...createKeyOptions.map((key) => ({
                        value: key.id ?? '',
                        label: `${key.label ? `${key.label} · ` : ''}${key.key_prefix} (${key.source})`,
                      })),
                    ]}
                    size="lg"
                    className="mt-1 w-full"
                  />
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

              <RouteDeploymentField
                id="new-route-deployment-type"
                value={createForm.deploymentType}
                onChange={(deploymentType) =>
                  setCreateForm((current) => ({ ...current, deploymentType }))
                }
              />

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
                    <Select
                      id="new-route-openrouter-routing"
                      value={openRouterRoutingValue(
                        createForm.openRouterProvider,
                        createForm.openRouterSort,
                      )}
                      onValueChange={(routingValue) => {
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
                      options={groupOpenRouterRouting(createOpenRouterRoutingOptions)}
                      searchPlaceholder="Search OpenRouter providers…"
                      size="lg"
                      className="mt-1 w-full"
                    />
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
                          setCreateForm((current) => ({
                            ...current,
                            quotaLimit: event.target.value,
                          }))
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
                  disabled={verifyingCreateRoute || confirmation.busy || !createFormValid}
                  className={
                    createRouteVerified
                      ? 'rounded-md border border-emerald-600 bg-emerald-600 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40'
                      : 'rounded-md border border-gray-200 bg-white px-4 py-2 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40'
                  }
                >
                  {verifyingCreateRoute
                    ? 'Verifying…'
                    : createRouteVerified
                      ? 'Verified'
                      : 'Verify'}
                </button>
                <button
                  type="submit"
                  disabled={confirmation.busy || !createFormValid}
                  className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {confirmation.busy
                    ? creatingModel
                      ? 'Creating…'
                      : 'Adding…'
                    : creatingModel
                      ? 'Create'
                      : 'Add'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {capabilityRoute && (
        <div className="fixed inset-0 z-50 overflow-y-auto bg-gray-900/30 px-4 py-8">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="route-capabilities-title"
            className="mx-auto w-full max-w-3xl rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
          >
            <form onSubmit={onSaveCapabilities}>
              <div className="mb-4 flex items-start justify-between gap-3">
                <div>
                  <h3
                    id="route-capabilities-title"
                    className="text-[14px] font-semibold text-gray-900"
                  >
                    Route capabilities
                  </h3>
                  <p className="mt-1 font-mono text-[12px] text-gray-400">
                    {capabilityRoute.model_id} / {capabilityRoute.endpoint_id}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={closeCapabilityEditor}
                  disabled={confirmation.busy}
                  className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100 disabled:opacity-40"
                >
                  Close
                </button>
              </div>
              <RouteCapabilityFields
                idPrefix="route-capabilities"
                draft={capabilityDraft}
                reading={capabilityReading}
                onChange={(change) => setCapabilityDraft((current) => ({ ...current, ...change }))}
                help="Requests through this route follow these values from the next request on, and they hold across restarts. Tool definitions are dropped without tool calling, max_tokens is capped at the max output, and images reach this route only if it accepts them."
              />
              {capabilityRoute.capabilities_baseline && (
                <p className="mt-3 text-[12px] text-gray-500">
                  {capabilityRoute.capabilities_overridden ? 'Set in the console. ' : ''}
                  From config: {describeCapabilities(capabilityRoute.capabilities_baseline)}
                </p>
              )}
              <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t border-gray-100 pt-3">
                <span
                  className={`mr-auto text-[12px] ${
                    capabilityEditChanges.length > 0
                      ? 'font-medium text-amber-700'
                      : 'text-gray-500'
                  }`}
                  role="status"
                >
                  {capabilityEditChanges.length > 0
                    ? `${plural(capabilityEditChanges.length, 'unsaved change')}`
                    : capabilityReading.payload === null
                      ? 'Answer every field to save'
                      : 'No changes yet'}
                </span>
                {capabilityRoute.capabilities_overridden && (
                  <button
                    type="button"
                    onClick={confirmResetCapabilities}
                    disabled={confirmation.busy}
                    className="h-9 rounded-lg px-3 text-[13px] font-medium text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                  >
                    Reset to config
                  </button>
                )}
                <button
                  type="button"
                  onClick={closeCapabilityEditor}
                  disabled={confirmation.busy}
                  aria-label={`Cancel editing ${capabilityRoute.endpoint_id} capabilities`}
                  className={BUTTON_GHOST}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  // Disabled for a no-op and while a value is invalid; Confirm writes.
                  disabled={
                    confirmation.busy ||
                    capabilityReading.payload === null ||
                    capabilityEditChanges.length === 0
                  }
                  className={BUTTON_PRIMARY}
                >
                  {confirmation.busy ? 'Saving…' : 'Save capabilities'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {editingRoute && (
        <div className="fixed inset-0 z-50 overflow-y-auto bg-gray-900/30 px-4 py-8">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="edit-provider-route-title"
            className="mx-auto w-full max-w-5xl rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
          >
            <form onSubmit={onSubmit}>
              <div className="mb-4 flex items-start justify-between gap-3">
                <div>
                  <h3
                    id="edit-provider-route-title"
                    className="text-[14px] font-semibold text-gray-900"
                  >
                    Edit provider route
                  </h3>
                  <p className="mt-1 font-mono text-[12px] text-gray-400">
                    {editingRoute.model_id} / {editingRoute.provider} / {editingRoute.route_type}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={closeRouteEdit}
                  disabled={confirmation.busy}
                  className="rounded-md px-2 py-1 text-[12px] text-gray-500 hover:bg-gray-100 disabled:opacity-40"
                >
                  Close
                </button>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <label className="text-[12px] font-medium text-gray-500" htmlFor="route-provider">
                    {editingRuntimeRoute ? 'Provider' : 'Override provider'}
                  </label>
                  <Select
                    id="route-provider"
                    value={form.upstreamProvider}
                    onValueChange={onUpstreamProviderChange}
                    options={providerSelectOptions.map((option) => ({
                      value: option.provider,
                      label: option.label,
                    }))}
                    searchPlaceholder="Search providers…"
                    aria-required
                    size="lg"
                    className="mt-1 w-full"
                  />
                </div>
                <div>
                  <label className="text-[12px] font-medium text-gray-500" htmlFor="route-api-key">
                    API key
                  </label>
                  <Select
                    id="route-api-key"
                    value={form.apiKeyId}
                    onValueChange={(apiKeyId) => setForm((current) => ({ ...current, apiKeyId }))}
                    disabled={keysLoading}
                    options={[
                      { value: '', label: defaultKeyOptionLabel(keyProvider) },
                      ...keyOptions.map((key) => ({
                        value: key.id ?? '',
                        label: `${key.label ? `${key.label} · ` : ''}${key.key_prefix} (${key.source})`,
                      })),
                    ]}
                    size="lg"
                    className="mt-1 w-full"
                  />
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
                    setForm((current) => ({
                      ...current,
                      baseUrl: event.target.value,
                      deploymentType: null,
                    }))
                  }
                  className="mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none"
                  required
                />
              </div>

              <RouteDeploymentField
                id="route-deployment-type"
                value={editDeploymentType}
                onChange={(deploymentType) =>
                  setForm((current) => ({ ...current, deploymentType }))
                }
              />

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
                    <Select
                      id="route-openrouter-routing"
                      value={openRouterRoutingValue(form.openRouterProvider, form.openRouterSort)}
                      onValueChange={(routingValue) => {
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
                      options={groupOpenRouterRouting(editOpenRouterRoutingOptions)}
                      searchPlaceholder="Search OpenRouter providers…"
                      size="lg"
                      className="mt-1 w-full"
                    />
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

              {editsConcurrencyLimit && (
                <div className="mt-3">
                  <label
                    className="text-[12px] font-medium text-gray-500"
                    htmlFor="route-concurrency-limit"
                  >
                    Local concurrency limit
                  </label>
                  <input
                    id="route-concurrency-limit"
                    type="number"
                    min={1}
                    step={1}
                    value={form.concurrencyLimit}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        concurrencyLimit: event.target.value,
                      }))
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
                    verifyingKey === routeKey(editingRoute) || confirmation.busy || !editFormValid
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
              </div>
              <EditBar
                target={`${editingRoute.model_id} / ${editingRoute.provider}`}
                changeCount={routeEditChanges.length}
                onCancel={closeRouteEdit}
                busy={confirmation.busy}
                error={routeEditError}
                submit
              />
            </form>
          </div>
        </div>
      )}

      {/* Last, so it stacks over the route and create dialogs it confirms. */}
      {confirmation.dialog}
    </div>
  );
}
