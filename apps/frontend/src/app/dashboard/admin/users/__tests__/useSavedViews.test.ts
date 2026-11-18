import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  loadCustomViews,
  saveCustomView,
  deleteCustomView,
  STORAGE_KEY,
} from '../hooks/useSavedViews';
import { DEFAULT_FILTER_STATE } from '../lib/filterTypes';

const memStore: Record<string, string> = {};
beforeEach(() => {
  for (const k of Object.keys(memStore)) delete memStore[k];
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => memStore[k] ?? null,
    setItem: (k: string, v: string) => {
      memStore[k] = v;
    },
    removeItem: (k: string) => {
      delete memStore[k];
    },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('useSavedViews helpers', () => {
  it('loads empty when nothing stored', () => {
    expect(loadCustomViews()).toEqual([]);
  });

  it('loads empty when storage corrupt (no crash)', () => {
    memStore[STORAGE_KEY] = 'not json';
    expect(loadCustomViews()).toEqual([]);
  });

  it('saves and reloads a view', () => {
    saveCustomView({
      id: 'mine',
      name: 'Mine',
      builtin: false,
      filterState: { ...DEFAULT_FILTER_STATE, search: 'a' },
    });
    const loaded = loadCustomViews();
    expect(loaded).toHaveLength(1);
    expect(loaded[0].id).toBe('mine');
  });

  it('refuses to save a view with builtin=true', () => {
    expect(() =>
      saveCustomView({
        id: 'pending',
        name: 'Pending',
        builtin: true,
        filterState: DEFAULT_FILTER_STATE,
      }),
    ).toThrow();
  });

  it('deleteCustomView removes the entry', () => {
    saveCustomView({
      id: 'mine',
      name: 'Mine',
      builtin: false,
      filterState: DEFAULT_FILTER_STATE,
    });
    deleteCustomView('mine');
    expect(loadCustomViews()).toEqual([]);
  });
});

it('keeps a spend threshold to the currency it was saved under', () => {
  const view = {
    id: 'spend',
    name: 'Spend',
    builtin: false,
    filterState: { ...DEFAULT_FILTER_STATE, minCostToday: 5 },
  };
  saveCustomView(view);
  saveCustomView({ ...view, filterState: { ...view.filterState, minCostToday: 30 } }, 'CNY');
  expect(loadCustomViews()[0].filterState.minCostToday).toBe(5);
  expect(loadCustomViews('CNY')).toHaveLength(1);
  expect(loadCustomViews('CNY')[0].filterState.minCostToday).toBe(30);
  expect(loadCustomViews('CNY')[0].thresholdsLeftOut).toBeUndefined();
  deleteCustomView('spend', 'CNY');
  expect(loadCustomViews()).toHaveLength(1);
});

describe('views saved before the ledger moved to another currency', () => {
  const usdViews = [
    {
      id: 'pending-pro',
      name: 'Pending pro',
      builtin: false,
      filterState: { ...DEFAULT_FILTER_STATE, status: 'pending_approval' as const, search: 'pro' },
    },
    {
      id: 'big-spenders',
      name: 'Big spenders',
      builtin: false,
      filterState: {
        ...DEFAULT_FILTER_STATE,
        provider: 'openai',
        minCostToday: 5,
        minCostMonth: 50,
      },
    },
  ];
  beforeEach(() => {
    memStore[STORAGE_KEY] = JSON.stringify(usdViews);
  });

  it('are shown, a view without thresholds exactly as saved', () => {
    const [plain, spend] = loadCustomViews('CNY');
    expect(plain).toEqual(usdViews[0]);
    expect(spend.id).toBe('big-spenders');
  });

  it('leave out their thresholds, never read as amounts in the new currency', () => {
    const spend = loadCustomViews('CNY')[1];
    expect(spend.filterState).toEqual({
      ...usdViews[1].filterState,
      minCostToday: null,
      minCostMonth: null,
    });
    expect(spend.thresholdsLeftOut).toBe('USD');
    // What is stored is untouched.
    expect(JSON.parse(memStore[STORAGE_KEY])).toEqual(usdViews);
  });

  it('can be deleted under the new currency', () => {
    deleteCustomView('big-spenders', 'CNY');
    expect(loadCustomViews('CNY').map((v) => v.id)).toEqual(['pending-pro']);
    expect(JSON.parse(memStore[STORAGE_KEY])).toEqual([usdViews[0]]);
  });

  it('are not copied when a view is saved under the new currency', () => {
    saveCustomView(
      {
        id: 'cny-spend',
        name: 'CNY spend',
        builtin: false,
        filterState: { ...DEFAULT_FILTER_STATE, minCostToday: 30 },
      },
      'CNY',
    );
    expect(JSON.parse(memStore[`${STORAGE_KEY}.CNY`]).map((v: { id: string }) => v.id)).toEqual([
      'cny-spend',
    ]);
    expect(loadCustomViews('CNY').map((v) => v.id)).toEqual([
      'cny-spend',
      'pending-pro',
      'big-spenders',
    ]);
  });

  it('give way to a view of the same id saved under the new currency', () => {
    saveCustomView(
      { ...usdViews[1], filterState: { ...usdViews[1].filterState, minCostToday: 30 } },
      'CNY',
    );
    const views = loadCustomViews('CNY');
    expect(views.filter((v) => v.id === 'big-spenders')).toHaveLength(1);
    expect(views.find((v) => v.id === 'big-spenders')?.filterState.minCostToday).toBe(30);
  });

  it('do not store the shown-only marker when saved again', () => {
    saveCustomView(loadCustomViews('CNY')[1], 'CNY');
    expect(JSON.parse(memStore[`${STORAGE_KEY}.CNY`])[0]).not.toHaveProperty('thresholdsLeftOut');
  });
});
