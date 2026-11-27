import { describe, expect, it } from 'vitest';
import { DEFAULT_FILTER_STATE, filterStateFromUrl, filterStateToUrl } from '../lib/filterTypes';

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
      tag: 'unattended',
      view: null,
    };
    const url = filterStateToUrl(state);
    const parsed = filterStateFromUrl(new URLSearchParams(url));
    expect(parsed).toEqual(state);
  });

  it('default state produces empty querystring', () => {
    expect(filterStateToUrl(DEFAULT_FILTER_STATE)).toBe('');
  });

  it.each(['requests', 'tokens'] as const)('round-trips sortBy=%s', (sortBy) => {
    const url = filterStateToUrl({ ...DEFAULT_FILTER_STATE, sortBy });
    expect(filterStateFromUrl(new URLSearchParams(url)).sortBy).toBe(sortBy);
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

describe('currency-bound amount filters', () => {
  it('drops legacy USD thresholds when opened in CNY', () => {
    const parsed = filterStateFromUrl(
      new URLSearchParams('min_cost_today=5&min_cost_month=20&status=active'),
      'CNY',
    );
    expect(parsed.minCostToday).toBeNull();
    expect(parsed.minCostMonth).toBeNull();
    expect(parsed.status).toBe('active');
  });

  it('round-trips a CNY threshold and refuses it in USD', () => {
    const params = new URLSearchParams(
      filterStateToUrl({ ...DEFAULT_FILTER_STATE, minCostToday: 5 }, 'CNY'),
    );
    expect(params.get('currency')).toBe('CNY');
    expect(filterStateFromUrl(params, 'CNY').minCostToday).toBe(5);
    expect(filterStateFromUrl(params, 'USD').minCostToday).toBeNull();
  });
});
