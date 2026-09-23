/**
 * The host facade — everything a distribution's UI module is allowed to import.
 *
 * ## Why this file exists
 *
 * A module that could import anything would be coupled to this application's
 * internal layout: one renamed provider, one moved helper and the deployment's
 * build breaks for a reason that has nothing to do with its own design. The
 * facade is the promised surface. It is deliberately small, and it is the only
 * module path a distribution should reach into.
 *
 * ## Rules for a module
 *
 * Import from here, from `react`, from `next/*`, and from your own relative
 * paths. Do not deep-import `@/app/...`, `@/components/...`, `@/config/...` or
 * `@/lib/...`: those are internals of the shared console, they may move at any
 * time. `src/site-ui/verify-imports.js` rejects one while the module is staged,
 * and `src/site-ui/containment.js` fails any build that resolves one.
 *
 * ## What a module gets, and what it does not
 *
 * Reads, and one narrow subscription: the deployment's public configuration,
 * the resolved identity, the session *state*, the field styling the shared
 * forms apply, and its own copy.
 *
 * There is no authentication *operation* here. `useSession()` reports whether
 * someone is signed in so a header can choose between "Log in" and "Console";
 * it does not log in, log out, refresh, mint a key or touch a token. Those live
 * in the shared controllers, which are the only code that makes a request, and a
 * module cannot reach them.
 *
 * This is a boundary about responsibility, not a sandbox: a module is compiled
 * into the application and could in principle reach around the facade. The point
 * is that the promised surface states what is supported, so a module that stays
 * on it does not break when the console is refactored — and a reviewer can see
 * at a glance what a distribution may depend on.
 */

'use client';

import type { RuntimeSiteConfig } from '@/config/site-config';

// Re-exported through a rename so a reader of a stack trace, or of the module
// API, sees one name for the concept.
export { useSiteConfig, useBranding } from '@/components/providers/SiteConfigProvider';
export type { RuntimeSiteConfig } from '@/config/site-config';

// Named slices of the runtime document, so a module can type one prop without
// importing the whole configuration type.
export type RuntimeBranding = RuntimeSiteConfig['branding'];
export type RuntimeFeatures = RuntimeSiteConfig['features'];
export type RuntimeDistribution = RuntimeSiteConfig['distribution'];

/**
 * Who is signed in, if anyone.
 *
 * Deliberately a projection of the console's auth context rather than the
 * context itself. Exporting `useAuth` would hand a module `login`, `logout` and
 * `refreshUser` — three ways to start an authentication request from outside the
 * controllers that own the schema, the error handling and the redirect rules. A
 * header needs none of them; it needs to know which word to put on a link.
 */
export { useSession } from '@/components/providers/SessionState';
export type { SessionState } from '@/components/providers/SessionState';

export { useAuthAppearance } from '@/site-ui/appearance';
export type { AuthAppearance } from '@/site-ui/contract';
export { PUBLIC_ROUTES, TERMS_SECTION_ANCHOR } from '@/site-ui/contract';
export type {
  AuthFieldLayout,
  AuthFieldLayoutProps,
  AuthFrameProps,
  AuthMessages,
  LandingPageProps,
  PublicRoute,
  SiteUiClientModule,
  SiteUiModuleDescriptor,
  SiteUiServerModule,
  TermsFrameProps,
} from '@/site-ui/contract';

/**
 * Resolve one interface string from the module's own copy.
 *
 * A module ships its copy with itself — that is the point of the split — so this
 * is the host's *passthrough* translator, whose strings are the English literals
 * the console passes in. A module with its own language supplies its own
 * resolver with the same `(slot, fallback)` signature; the shape is the
 * interface, not this implementation.
 *
 * Exported rather than left out so a component can move between the console and
 * a module without its call sites being rewritten.
 */
export { useT } from '@/components/providers/useT';
export type { Translate } from '@/components/providers/useT';

/** Substitute `{name}` placeholders in a resolved string. */
export { fill } from '@/lib/utils/interpolate';
