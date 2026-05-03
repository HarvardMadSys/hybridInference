import type { FilterState, QuotaStateFilter, SortBy, UserStatus } from '../types';

export const DEFAULT_FILTER_STATE: FilterState = {
  status: null,
  search: '',
  sortBy: 'created',
  minCostToday: null,
  minCostMonth: null,
  quotaState: null,
  provider: null,
  activeWithinHours: null,
  anomaly: null,
  view: null,
};

const VALID_STATUS: ReadonlyArray<UserStatus> = [
  'pending_approval',
  'active',
  'suspended',
  'rejected',
  'deleted',
];

const VALID_SORT: ReadonlyArray<SortBy> = [
  'created',
  'cost_today',
  'cost_month',
  'cost_alltime',
  'last_login',
];

const VALID_QUOTA: ReadonlyArray<QuotaStateFilter> = ['near', 'over', 'custom', 'default'];

function parseNumber(s: string | null): number | null {
  if (s === null || s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function filterStateFromUrl(params: URLSearchParams): FilterState {
  const status = params.get('status');
  const sortBy = params.get('sort_by');
  const quotaState = params.get('quota_state');
  const anomalyRaw = params.get('anomaly');
  const anomaly = anomalyRaw === 'true' ? true : anomalyRaw === 'false' ? false : null;
  return {
    status: VALID_STATUS.includes(status as UserStatus) ? (status as UserStatus) : null,
    search: params.get('q') ?? '',
    sortBy: VALID_SORT.includes(sortBy as SortBy) ? (sortBy as SortBy) : 'created',
    minCostToday: parseNumber(params.get('min_cost_today')),
    minCostMonth: parseNumber(params.get('min_cost_month')),
    quotaState: VALID_QUOTA.includes(quotaState as QuotaStateFilter)
      ? (quotaState as QuotaStateFilter)
      : null,
    provider: params.get('provider'),
    activeWithinHours: parseNumber(params.get('active_within_hours')),
    anomaly,
    view: params.get('view'),
  };
}

export function filterStateToUrl(state: FilterState): string {
  const out = new URLSearchParams();
  if (state.status) out.set('status', state.status);
  if (state.search) out.set('q', state.search);
  if (state.sortBy !== 'created') out.set('sort_by', state.sortBy);
  if (state.minCostToday !== null) out.set('min_cost_today', String(state.minCostToday));
  if (state.minCostMonth !== null) out.set('min_cost_month', String(state.minCostMonth));
  if (state.quotaState) out.set('quota_state', state.quotaState);
  if (state.provider) out.set('provider', state.provider);
  if (state.activeWithinHours !== null) {
    out.set('active_within_hours', String(state.activeWithinHours));
  }
  if (state.anomaly !== null) out.set('anomaly', String(state.anomaly));
  if (state.view) out.set('view', state.view);
  return out.toString();
}

/**
 * Serialise FilterState to backend query params for /admin/users.
 * Always sets sort_by/limit/offset (defaults preserved). The `view` field
 * is purely client-side and is NOT sent to the backend.
 */
export function filterStateToParams(
  state: FilterState,
  pagination: { limit?: number; offset?: number } = {},
): URLSearchParams {
  const params = new URLSearchParams();
  if (state.status) params.set('status', state.status);
  if (state.search) params.set('search', state.search);
  params.set('sort_by', state.sortBy);
  if (state.minCostToday !== null) params.set('min_cost_today', String(state.minCostToday));
  if (state.minCostMonth !== null) params.set('min_cost_month', String(state.minCostMonth));
  if (state.quotaState) params.set('quota_state', state.quotaState);
  if (state.provider) params.set('provider', state.provider);
  if (state.activeWithinHours !== null) {
    params.set('active_within_hours', String(state.activeWithinHours));
  }
  if (state.anomaly !== null) params.set('anomaly', String(state.anomaly));
  params.set('limit', String(pagination.limit ?? 100));
  params.set('offset', String(pagination.offset ?? 0));
  return params;
}
