'use client';

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

interface ProviderSelection {
  /** The provider Keys opens on: the one Overview's "Manage keys" asked for, or the one Keys showed last. */
  provider: string;
  /**
   * Whether "Manage keys" asked for it. Keys then keeps it even when it has no
   * keys to manage, and says so. One Keys only showed last gives way to the
   * first provider once it is no longer listed: deleted on Overview, say.
   */
  requested: boolean;
  /** "Manage keys" asks for a provider. */
  request: (provider: string) => void;
  /** Keys reports the provider it shows. */
  show: (provider: string) => void;
}

// Outside the Providers layout (a section rendered on its own) nothing is kept.
const Selection = createContext<ProviderSelection>({
  provider: '',
  requested: false,
  request: () => {},
  show: () => {},
});

/**
 * Keeps the provider Keys shows while the administrator moves between the
 * Providers sections. The Providers layout renders it, and a layout stays
 * mounted from one of its pages to the next, so Keys opens again on the
 * provider it showed last.
 */
export function ProviderSelectionProvider({ children }: { children: ReactNode }) {
  const [selection, setSelection] = useState({ provider: '', requested: false });
  const request = useCallback(
    (provider: string) => setSelection({ provider, requested: true }),
    [],
  );
  // Keys reports what it shows on every change, its first render included: a
  // report of what is already kept changes nothing.
  const show = useCallback(
    (provider: string) =>
      setSelection((current) =>
        current.provider === provider && !current.requested
          ? current
          : { provider, requested: false },
      ),
    [],
  );
  const value = useMemo(() => ({ ...selection, request, show }), [selection, request, show]);
  return <Selection.Provider value={value}>{children}</Selection.Provider>;
}

export function useProviderSelection(): ProviderSelection {
  return useContext(Selection);
}
