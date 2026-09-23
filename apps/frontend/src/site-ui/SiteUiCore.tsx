'use client';

import { createContext, useContext } from 'react';

import { useT } from '@/components/providers/useT';
import {
  AuthAppearanceProvider,
  AuthFieldLayoutProvider,
  DefaultAuthFieldLayout,
  NEUTRAL_AUTH_APPEARANCE,
} from '@/site-ui/appearance';
import type { AuthAppearance } from '@/site-ui/contract';
import { SITE_UI_CLIENT } from '@/site-ui/module';

/**
 * The compiled-in module, its styling and its account wording, available to the
 * shared components that render inside it.
 *
 * Separate from `SiteUiBoundary` on purpose, and the separation keeps the
 * import graph acyclic: the boundary decides *which route* a module frames and
 * therefore needs the route table, while the appearance the shared forms read
 * must not, or a shared form would end up importing the routing layer that
 * renders it.
 */

interface SiteUiContextValue {
  appearance: AuthAppearance;
  /** Account-page wording: module dictionary, then content document, then English. */
  t: (key: string, fallback: string) => string;
}

const SiteUiContext = createContext<SiteUiContextValue | null>(null);

export function useSiteUi(): SiteUiContextValue {
  const value = useContext(SiteUiContext);
  if (!value) {
    // Total rather than throwing: `AuthField` and friends are also rendered by
    // error pages and by unit tests that have no boundary above them, and a
    // form that cannot say its label is worse than one using the neutral look.
    return NEUTRAL_SITE_UI;
  }
  return value;
}

const NEUTRAL_SITE_UI: SiteUiContextValue = {
  appearance: NEUTRAL_AUTH_APPEARANCE,
  t: (_key, fallback) => fallback,
};

/**
 * Installs the active module's auth appearance, its field layout and its
 * wording for the shared account pages.
 *
 * `children` is whatever the route boundary decided to render; this component
 * makes no routing decision of its own, so the two concerns stay separable in a
 * test.
 */
export function SiteUiProvider({ children }: { children: React.ReactNode }) {
  const translate = useT();
  const appearance = SITE_UI_CLIENT.authAppearance ?? NEUTRAL_AUTH_APPEARANCE;
  const fieldLayout = SITE_UI_CLIENT.fieldLayout ?? DefaultAuthFieldLayout;
  const messages = SITE_UI_CLIENT.authMessages;

  /**
   * One translator for the account pages, three sources in order: the module's
   * own dictionary, then the deployment's `/site-config` content document, then
   * the neutral English the shared page passed in.
   *
   * The last step is what keeps a partially translated deployment usable, and a
   * key set to `''` resolving to empty is a module suppressing a sentence on
   * purpose rather than a missing translation.
   */
  const value: SiteUiContextValue = {
    appearance,
    t: (key, fallback) =>
      (messages ? messages[key as keyof typeof messages] : undefined) ?? translate(key, fallback),
  };

  return (
    <SiteUiContext.Provider value={value}>
      <AuthAppearanceProvider value={appearance}>
        <AuthFieldLayoutProvider value={fieldLayout}>{children}</AuthFieldLayoutProvider>
      </AuthAppearanceProvider>
    </SiteUiContext.Provider>
  );
}
