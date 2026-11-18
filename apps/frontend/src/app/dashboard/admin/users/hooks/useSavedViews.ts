import { useMoney } from '@/lib/hooks/useMoney';
import type { Currency } from '@/lib/utils/money';
import { useCallback, useEffect, useState } from 'react';
import type { SavedView } from '../types';

export const STORAGE_KEY = 'admin.users.savedViews';
const CURRENCIES: readonly Currency[] = ['USD', 'CNY'];
const storageKey = (currency: Currency) =>
  currency === 'USD' ? STORAGE_KEY : `${STORAGE_KEY}.${currency}`;

/** The views stored under *currency*, as saved. */
function storedViews(currency: Currency): SavedView[] {
  try {
    const raw = localStorage.getItem(storageKey(currency));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (v): v is SavedView =>
        v && typeof v.id === 'string' && typeof v.name === 'string' && v.builtin === false,
    );
  } catch {
    return [];
  }
}

const hasThresholds = ({ filterState }: SavedView) =>
  filterState.minCostToday != null || filterState.minCostMonth != null;

/**
 * A view saved under another currency, as it applies under this one. Its spend
 * thresholds are amounts in the currency it was saved under, and the console
 * never converts between currencies, so they are left out; every other filter
 * means the same under either.
 */
function carriedOver(view: SavedView, savedUnder: Currency): SavedView {
  if (!hasThresholds(view)) return view;
  return {
    ...view,
    filterState: { ...view.filterState, minCostToday: null, minCostMonth: null },
    thresholdsLeftOut: savedUnder,
  };
}

/**
 * The custom views shown on a ledger kept in *currency*: those saved under it,
 * then those saved under another currency -- before the ledger moved to this
 * one -- without their spend thresholds. A view saved under this currency
 * takes the place of one with the same id saved under another.
 */
export function loadCustomViews(currency: Currency = 'USD'): SavedView[] {
  const views = storedViews(currency);
  const ids = new Set(views.map((v) => v.id));
  for (const other of CURRENCIES) {
    if (other === currency) continue;
    for (const view of storedViews(other)) {
      if (ids.has(view.id)) continue;
      ids.add(view.id);
      views.push(carriedOver(view, other));
    }
  }
  return views;
}

export function saveCustomView(view: SavedView, currency: Currency = 'USD'): void {
  if (view.builtin) {
    throw new Error('Cannot save a built-in view');
  }
  const { thresholdsLeftOut: _shownOnly, ...saved } = view;
  const existing = storedViews(currency);
  const next = [...existing.filter((v) => v.id !== saved.id), saved];
  localStorage.setItem(storageKey(currency), JSON.stringify(next));
}

/** Delete the view *id* shown under *currency*, wherever it was saved. */
export function deleteCustomView(id: string, currency: Currency = 'USD'): void {
  const savedUnder = [currency, ...CURRENCIES.filter((c) => c !== currency)].find((c) =>
    storedViews(c).some((v) => v.id === id),
  );
  if (!savedUnder) return;
  const next = storedViews(savedUnder).filter((v) => v.id !== id);
  localStorage.setItem(storageKey(savedUnder), JSON.stringify(next));
}

export function useSavedViews() {
  const { currency } = useMoney();
  const [views, setViews] = useState<SavedView[]>([]);

  useEffect(() => {
    setViews(loadCustomViews(currency));
  }, [currency]);

  const save = useCallback(
    (view: SavedView) => {
      saveCustomView(view, currency);
      setViews(loadCustomViews(currency));
    },
    [currency],
  );

  const remove = useCallback(
    (id: string) => {
      deleteCustomView(id, currency);
      setViews(loadCustomViews(currency));
    },
    [currency],
  );

  return { views, save, remove };
}
