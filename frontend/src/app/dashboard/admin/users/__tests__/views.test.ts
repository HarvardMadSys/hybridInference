import { describe, expect, it } from 'vitest';
import { BUILTIN_VIEWS, getViewById } from '../lib/views';

describe('built-in views', () => {
  it('exports the spec set', () => {
    const ids = BUILTIN_VIEWS.map((v) => v.id);
    expect(ids).toEqual([
      'pending',
      'top-spenders-today',
      'anomalies',
      'near-quota',
      'recently-active',
    ]);
  });

  it('all built-ins have builtin=true', () => {
    expect(BUILTIN_VIEWS.every((v) => v.builtin)).toBe(true);
  });

  it('pending view sets status filter', () => {
    const view = getViewById('pending');
    expect(view?.filterState.status).toBe('pending_approval');
  });

  it('top-spenders sorts by cost_today', () => {
    const view = getViewById('top-spenders-today');
    expect(view?.filterState.sortBy).toBe('cost_today');
  });

  it('returns undefined for unknown id', () => {
    expect(getViewById('nope')).toBeUndefined();
  });
});
