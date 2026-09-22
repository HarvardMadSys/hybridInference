'use client';

import type { AuthAppearance, AuthMessages, SiteUiClientModule } from './contract';
import * as activeModule from '@site-ui/client';

/**
 * Normalize whatever the compiled-in module exports into one shape.
 *
 * The interface documents three required named exports on `client.tsx`
 * (`descriptor`) and five optional ones
 * (`AuthFrame`, `TermsFrame`, `authAppearance`, `authMessages`) — the optional
 * four being the ones whose absence is a statement rather than a gap. A module
 * may also
 * default-export a single object carrying them. Both are legitimate — the
 * property that matters is that the module provides the pieces, not which
 * syntax carried them — so the host resolves both here, once, instead of
 * teaching every consumer to cope with two shapes.
 *
 * Done at module scope rather than in a hook: the bundler chose this module
 * when it followed the `@site-ui` alias, so the answer cannot change between
 * renders and there is nothing to re-evaluate.
 */

interface ModuleLike {
  descriptor?: SiteUiClientModule['descriptor'];
  Landing?: SiteUiClientModule['Landing'] | null;
  AuthFrame?: SiteUiClientModule['AuthFrame'];
  TermsFrame?: SiteUiClientModule['TermsFrame'];
  PublicFrame?: SiteUiClientModule['PublicFrame'];
  authAppearance?: AuthAppearance;
  authMessages?: AuthMessages;
  default?: ModuleLike;
}

const raw = activeModule as unknown as ModuleLike;

// A default export wins only for the keys it actually provides, so a module may
// put some pieces on the namespace and the rest in a default object without one
// silently masking the other.
const fallback = raw.default ?? {};

function pick<K extends keyof ModuleLike>(key: K): ModuleLike[K] {
  const named = raw[key];
  if (named !== undefined && named !== null) return named;
  return fallback[key];
}

/**
 * Like `pick`, but keeps `null` as an answer.
 *
 * The distinction this preserves is the whole reason `Landing` is optional: a
 * module that provides no landing page is saying "the host's page is correct",
 * while a module that provides one is replacing it. Collapsing the two here
 * would make an absent `Landing` render nothing at all.
 */
function pickNullable<K extends keyof ModuleLike>(key: K): ModuleLike[K] {
  if (key in raw) return raw[key];
  if (fallback && key in fallback) return fallback[key];
  return undefined;
}

export const SITE_UI_DESCRIPTOR = pick('descriptor');

export const SITE_UI_CLIENT: SiteUiClientModule = {
  descriptor: pick('descriptor') ?? { siteUiApi: 1, id: 'unknown', locale: '' },
  // `null` is a meaningful answer, not a missing one: it means "the host's own
  // page is the right page for this route".
  Landing: pickNullable('Landing') ?? null,
  // `null`, not a passthrough, and for exactly the reason `TermsFrame` below is:
  // the boundary asks whether an account frame *exists* to decide who draws the
  // page. A module that declares nothing is saying "the console container is the
  // page", and it must not be answered with a component that draws nothing while
  // claiming the header — that is how five default account routes lost their
  // chrome.
  AuthFrame: pickNullable('AuthFrame') ?? null,
  // `null`, not a passthrough, and the difference is the whole contract: the
  // route boundary asks whether a terms frame *exists* to decide who draws the
  // legal page's header. A passthrough here would answer yes to that question
  // while drawing nothing, which is how `/terms` once rendered with no chrome
  // at all — the console stepped aside for a frame that was a no-op.
  TermsFrame: pickNullable('TermsFrame') ?? null,
  // Optional like the page-owning frames, and for the same kind of reason:
  // the host never renders it, so requiring it asked every module for a
  // component nothing would call. A module that wants one exports one.
  PublicFrame: pickNullable('PublicFrame') ?? null,
  authAppearance: pick('authAppearance'),
  authMessages: pick('authMessages'),
};
