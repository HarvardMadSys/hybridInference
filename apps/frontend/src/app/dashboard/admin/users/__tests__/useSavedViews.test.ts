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
