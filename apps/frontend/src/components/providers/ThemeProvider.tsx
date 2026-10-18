'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { THEME_STORAGE_KEY, SYSTEM_THEME_QUERY, type Theme, type ResolvedTheme } from './theme';

export type { Theme, ResolvedTheme } from './theme';

function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark' || value === 'system';
}

function storedTheme(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : 'system';
  } catch {
    return 'system';
  }
}

function systemTheme(): ResolvedTheme {
  return typeof window.matchMedia === 'function' && window.matchMedia(SYSTEM_THEME_QUERY).matches
    ? 'dark'
    : 'light';
}

function applyTheme(theme: Theme): ResolvedTheme {
  const resolved = theme === 'system' ? systemTheme() : theme;
  document.documentElement.dataset.consoleTheme = resolved;
  return resolved;
}

interface ThemeContextValue {
  theme: Theme;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'system',
  resolvedTheme: 'light',
  setTheme: () => undefined,
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // Keep the server and hydration render identical; the head script already
  // applies the saved appearance while React restores the preference controls.
  const [theme, updateTheme] = useState<Theme>('system');
  const currentTheme = useRef<Theme>('system');
  const [resolvedTheme, updateResolvedTheme] = useState<ResolvedTheme>('light');

  useEffect(() => {
    const sync = () => {
      const current = storedTheme();
      currentTheme.current = current;
      updateTheme(current);
      updateResolvedTheme(applyTheme(current));
    };
    sync();
    const query = window.matchMedia?.(SYSTEM_THEME_QUERY);
    const onSystemChange = () => {
      // Follow the current choice even when browser storage is unavailable.
      const current = currentTheme.current;
      if (current === 'system') updateResolvedTheme(applyTheme(current));
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_STORAGE_KEY || event.key === null) sync();
    };
    query?.addEventListener('change', onSystemChange);
    window.addEventListener('storage', onStorage);
    return () => {
      query?.removeEventListener('change', onSystemChange);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setTheme = useCallback((next: Theme) => {
    if (!isTheme(next)) return;
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Private browsing can disable storage; the choice still applies now.
    }
    currentTheme.current = next;
    updateTheme(next);
    updateResolvedTheme(applyTheme(next));
  }, []);

  const value = useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
