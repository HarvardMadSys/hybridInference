import { useMoney } from '@/lib/hooks/useMoney';
import type { Currency } from '@/lib/utils/money';
import { useCallback, useEffect, useState } from 'react';
import type { SavedView } from '../types';

export const STORAGE_KEY = 'admin.users.savedViews';
const storageKey = (currency: Currency) =>
  currency === 'USD' ? STORAGE_KEY : `${STORAGE_KEY}.${currency}`;

export function loadCustomViews(currency: Currency = 'USD'): SavedView[] {
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

export function saveCustomView(view: SavedView, currency: Currency = 'USD'): void {
  if (view.builtin) {
    throw new Error('Cannot save a built-in view');
  }
  const existing = loadCustomViews(currency);
  const next = [...existing.filter((v) => v.id !== view.id), view];
  localStorage.setItem(storageKey(currency), JSON.stringify(next));
}

export function deleteCustomView(id: string, currency: Currency = 'USD'): void {
  const existing = loadCustomViews(currency);
  const next = existing.filter((v) => v.id !== id);
  localStorage.setItem(storageKey(currency), JSON.stringify(next));
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
