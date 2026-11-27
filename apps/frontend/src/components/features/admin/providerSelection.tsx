'use client';

import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

interface ProviderSelection {
  /** The provider Keys shows: the one Overview's "Manage keys" named, or the one last chosen on Keys. */
  provider: string;
  setProvider: (provider: string) => void;
}

// Outside the Providers layout (a section rendered on its own) nothing is kept.
const Selection = createContext<ProviderSelection>({ provider: '', setProvider: () => {} });

/**
 * Keeps the provider Keys shows while the administrator moves between the
 * Providers sections. The Providers layout renders it, and a layout stays
 * mounted from one of its pages to the next, so Keys opens again on the
 * provider it showed last.
 */
export function ProviderSelectionProvider({ children }: { children: ReactNode }) {
  const [provider, setProvider] = useState('');
  const value = useMemo(() => ({ provider, setProvider }), [provider]);
  return <Selection.Provider value={value}>{children}</Selection.Provider>;
}

export function useProviderSelection(): ProviderSelection {
  return useContext(Selection);
}
