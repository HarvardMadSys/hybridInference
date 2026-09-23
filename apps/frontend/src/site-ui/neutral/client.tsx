'use client';

import type { ReactNode } from 'react';

// Absolute imports, not the `@/` alias: this file is reached through the
// generated `@site-ui` stub, and an aliased import inside a module reached by
// an alias is one more resolution rule the bundler, `tsc` and Vitest would each
// have to agree on. The stylesheet is not imported here: the root layout loads
// the selected module's `styles.css` through the generated bridge.
import type {
  AuthAppearance,
  AuthFrameProps,
  LandingPageProps,
  SiteUiModuleDescriptor,
} from '../contract';
import { NEUTRAL_AUTH_APPEARANCE } from '../appearance';
import { Card } from '../../components/ui/Card';

/**
 * The UI this application ships with, written against the same interface a
 * distribution implements.
 *
 * Not a placeholder and not an error fallback: this is the real default console
 * look, and it is why the interface stays honest. If the neutral implementation
 * could not be written against the contract, the contract would be describing
 * one distribution's design instead of a seam.
 *
 * A distribution implements the same optional capabilities from its own
 * `client.tsx`; omitting a frame preserves this default presentation.
 */

export const descriptor: SiteUiModuleDescriptor = {
  siteUiApi: 1,
  id: 'neutral',
  locale: '',
};

/**
 * The account pages' default look: a centred card with the heading inside it.
 *
 * Exported under its own name rather than as `AuthFrame`, because the two are
 * answers to different questions and conflating them is what broke these pages.
 * `AuthFrame` is a claim to own the whole page — header, `<main>`, footer — and
 * `null` below is this module saying the console's container is the page. This
 * component is the body that container holds.
 */
export function NeutralAuthCard({
  title,
  subtitle,
  topbar,
  children,
}: Pick<AuthFrameProps, 'title' | 'subtitle' | 'topbar' | 'children'>) {
  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">{title}</h1>
          <p className="mt-2 text-sm text-gray-600">{subtitle}</p>
        </div>
        {children}
        {/* The shared page supplies the account cross-link; the card places it. */}
        {topbar ? (
          <p className="mt-5 text-center text-sm text-gray-600 [&_a]:font-medium [&_a]:text-blue-600 [&_a:hover]:text-blue-700">
            {topbar}
          </p>
        ) : null}
      </Card>
    </div>
  );
}

/** The console owns account chrome; the shared form uses NeutralAuthCard. */
export const AuthFrame: ((props: AuthFrameProps) => ReactNode) | null = null;

/**
 * Present so the module has the shape the interface documents, but never
 * rendered for `/`.
 *
 * The console's landing page is a component tree of its own with its own tests,
 * and moving it behind this interface would be churn with no reader. The host
 * treats `Landing === null` as "keep the console's page" — see `useLanding()`.
 */
export const Landing: ((props: LandingPageProps) => ReactNode) | null = null;

/** The console's field styling, unchanged, supplied through the seam. */
export const authAppearance: AuthAppearance = NEUTRAL_AUTH_APPEARANCE;
