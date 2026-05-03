import { describe, expect, it } from 'vitest';
import {
  DEFAULT_FILTER_STATE,
  filterStateFromUrl,
  filterStateToParams,
  filterStateToUrl,
} from '../lib/filterTypes';

describe('filterStateFromUrl / filterStateToUrl', () => {
  it('round-trips a populated state', () => {
    const state = {
      ...DEFAULT_FILTER_STATE,
      status: 'active' as const,
      search: 'alice',
      sortBy: 'cost_today' as const,
      minCostToday: 10,
      quotaState: 'near' as const,
      provider: 'anthropic',
      anomaly: true,
      view: null,
    };
    const url = filterStateToUrl(state);
    const parsed = filterStateFromUrl(new URLSearchParams(url));
    expect(parsed).toEqual(state);
  });

  it('default state produces empty querystring', () => {
    expect(filterStateToUrl(DEFAULT_FILTER_STATE)).toBe('');
  });

  it('ignores unknown query params (forward compat)', () => {
    const params = new URLSearchParams('?status=active&future_param=42');
    const parsed = filterStateFromUrl(params);
    expect(parsed.status).toBe('active');
    expect(parsed).not.toHaveProperty('future_param');
  });

  it('rejects invalid status silently', () => {
    const params = new URLSearchParams('?status=banana');
    const parsed = filterStateFromUrl(params);
    expect(parsed.status).toBeNull();
  });
});

describe('filterStateToParams', () => {
  it('omits null/empty values', () => {
    const params = filterStateToParams(DEFAULT_FILTER_STATE);
    expect(params.toString()).toBe('sort_by=created&limit=100&offset=0');
  });

  it('serialises all filters', () => {
    const params = filterStateToParams({
      ...DEFAULT_FILTER_STATE,
      status: 'active',
      minCostToday: 5,
      provider: 'anthropic',
      activeWithinHours: 24,
      quotaState: 'near',
      anomaly: true,
    });
    const obj = Object.fromEntries(params.entries());
    expect(obj).toMatchObject({
      status: 'active',
      min_cost_today: '5',
      provider: 'anthropic',
      active_within_hours: '24',
      quota_state: 'near',
      anomaly: 'true',
    });
  });
});
