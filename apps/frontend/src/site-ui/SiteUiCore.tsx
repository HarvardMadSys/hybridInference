'use client';

import {
  AuthAppearanceProvider,
  AuthFieldLayoutProvider,
  DefaultAuthFieldLayout,
  NEUTRAL_AUTH_APPEARANCE,
} from '@/site-ui/appearance';
import { SITE_UI_CLIENT } from '@/site-ui/module';

/**
 * The compiled-in module's styling and field layout, available to the shared
 * components that render inside it.
 *
 * Separate from `SiteUiBoundary` on purpose, and the separation keeps the
 * import graph acyclic: the boundary decides *which route* a module frames and
 * therefore needs the route table, while the appearance the shared forms read
 * must not, or a shared form would end up importing the routing layer that
 * renders it.
 *
 * Wording is not installed here. Every page translates through `useT`, which
 * reads the module's dictionary as `module.tsx` left it — cut down to the keys
 * the contract declares — so there is one translator and one dictionary, and
 * no second path by which an undeclared key could reach a page.
 */

/**
 * Installs the active module's auth appearance and field layout for the shared
 * account pages.
 *
 * `children` is whatever the route boundary decided to render; this component
 * makes no routing decision of its own, so the two concerns stay separable in a
 * test.
 */
export function SiteUiProvider({ children }: { children: React.ReactNode }) {
  const appearance = SITE_UI_CLIENT.authAppearance ?? NEUTRAL_AUTH_APPEARANCE;
  const fieldLayout = SITE_UI_CLIENT.fieldLayout ?? DefaultAuthFieldLayout;

  return (
    <AuthAppearanceProvider value={appearance}>
      <AuthFieldLayoutProvider value={fieldLayout}>{children}</AuthFieldLayoutProvider>
    </AuthAppearanceProvider>
  );
}
