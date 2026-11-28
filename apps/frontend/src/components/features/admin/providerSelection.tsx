'use client';

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

/** One click of the registry's "Manage keys": the provider, and which click it was. */
export interface ProviderKeysRequest {
  provider: string;
  /** Counts the clicks, so the same provider asked for again is a new request. */
  serial: number;
}

interface ProviderSelection {
  /** The provider Keys opens on: the one "Manage keys" asked for, or the one Keys showed last. */
  provider: string;
  /**
   * Whether "Manage keys" asked for it. Keys then keeps it even when it has no
   * keys to manage, and says so. One Keys only showed last gives way to the
   * first provider once it is no longer listed: deleted from the registry, say.
   */
  requested: boolean;
  /** The last "Manage keys", which Keys follows while it is open; null before the first. */
  lastRequest: ProviderKeysRequest | null;
  /** "Manage keys" asks for a provider. */
  request: (provider: string) => void;
  /** Keys reports the provider it shows. */
  show: (provider: string) => void;
}

// Outside the Providers layout (a section rendered on its own) nothing is kept.
const Selection = createContext<ProviderSelection>({
  provider: '',
  requested: false,
  lastRequest: null,
  request: () => {},
  show: () => {},
});

interface Kept {
  provider: string;
  requested: boolean;
  lastRequest: ProviderKeysRequest | null;
}

/**
 * Keeps the provider Keys shows. The registry's "Manage keys" asks Keys, on
 * the same page, for a provider; and the Providers layout renders this, and a
 * layout stays mounted from one of its pages to the next, so Keys opens again
 * on the provider it showed last after a visit to Performance.
 */
export function ProviderSelectionProvider({ children }: { children: ReactNode }) {
  const [selection, setSelection] = useState<Kept>({
    provider: '',
    requested: false,
    lastRequest: null,
  });
  const request = useCallback(
    (provider: string) =>
      setSelection((current) => ({
        provider,
        requested: true,
        lastRequest: { provider, serial: (current.lastRequest?.serial ?? 0) + 1 },
      })),
    [],
  );
  // Keys reports what it shows on every change, its first render included: a
  // report of what is already kept changes nothing.
  const show = useCallback(
    (provider: string) =>
      setSelection((current) =>
        current.provider === provider && !current.requested
          ? current
          : { ...current, provider, requested: false },
      ),
    [],
  );
  const value = useMemo(() => ({ ...selection, request, show }), [selection, request, show]);
  return <Selection.Provider value={value}>{children}</Selection.Provider>;
}

export function useProviderSelection(): ProviderSelection {
  return useContext(Selection);
}
