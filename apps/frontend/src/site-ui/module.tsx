'use client';

import type { AuthAppearance, AuthFieldLayout, AuthMessages, SiteUiClientModule } from './contract';
import * as activeModule from '@site-ui/client';

/**
 * Normalize the compiled-in module once. The descriptor is required; Landing,
 * AuthFrame, TermsFrame, fieldLayout, authAppearance and authMessages are
 * optional. Named exports take precedence over properties on an optional
 * default object.
 */

interface ModuleLike {
  descriptor?: SiteUiClientModule['descriptor'];
  Landing?: SiteUiClientModule['Landing'] | null;
  AuthFrame?: SiteUiClientModule['AuthFrame'];
  TermsFrame?: SiteUiClientModule['TermsFrame'];
  fieldLayout?: AuthFieldLayout;
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
  // Missing page-owning components preserve the host's chrome.
  AuthFrame: pickNullable('AuthFrame') ?? null,
  TermsFrame: pickNullable('TermsFrame') ?? null,
  fieldLayout: pick('fieldLayout'),
  authAppearance: pick('authAppearance'),
  authMessages: pick('authMessages'),
};
