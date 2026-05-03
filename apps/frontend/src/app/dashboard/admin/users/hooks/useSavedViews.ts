import { useCallback, useEffect, useState } from 'react';
import type { SavedView } from '../types';

export const STORAGE_KEY = 'admin.users.savedViews';

export function loadCustomViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
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

export function saveCustomView(view: SavedView): void {
  if (view.builtin) {
    throw new Error('Cannot save a built-in view');
  }
  const existing = loadCustomViews();
  const next = [...existing.filter((v) => v.id !== view.id), view];
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
}

export function deleteCustomView(id: string): void {
  const existing = loadCustomViews();
  const next = existing.filter((v) => v.id !== id);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
}

export function useSavedViews() {
  const [views, setViews] = useState<SavedView[]>([]);

  useEffect(() => {
    setViews(loadCustomViews());
  }, []);

  const save = useCallback((view: SavedView) => {
    saveCustomView(view);
    setViews(loadCustomViews());
  }, []);

  const remove = useCallback((id: string) => {
    deleteCustomView(id);
    setViews(loadCustomViews());
  }, []);

  return { views, save, remove };
}
