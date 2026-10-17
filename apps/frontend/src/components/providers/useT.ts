'use client';

import { useMemo } from 'react';

import { useIsConsoleCopy } from '@/site-ui/appearance';
import { SITE_UI_CLIENT } from '@/site-ui/module';
import { translate, translator, type Translate } from '@/lib/i18n/translate';

/**
 * Resolve one interface string, in a component.
 *
 * Two sources, and the order matters: the compiled-in UI module's own
 * dictionary first, then the English literal the call site passes. A key the
 * module did not translate resolves to that literal, which is what keeps a
 * partially translated deployment usable.
 *
 * The module's dictionary is the *only* runtime source. Page copy used to be
 * resolved from a slot document the gateway served through `/site-config`,
 * which meant a deployment's sentences were part of the backend contract and a
 * wording change did not change the front-end build's inputs — so the version
 * lock could not tell that a running image was stale. Copy now ships with the
 * image that renders it (see `docs/developer/distribution-customization.md`).
 *
 * `SITE_UI_CLIENT.authMessages` holds only the keys the contract declares —
 * `module.tsx` drops the rest — so a module can supply wording only for those.
 * Console routes (marked by `SiteUiBoundary` when `usePublicRoute()` is `null`)
 * always keep the shared English copy (`CONSOLE_LANGUAGE = 'en'`), even when
 * they mount a shared component such as `PhoneAuthForm`.
 */
export function useT(): Translate {
  const isConsoleRoute = useIsConsoleCopy();
  const messages = isConsoleRoute ? undefined : SITE_UI_CLIENT.authMessages;
  return useMemo(() => (messages ? translator(messages) : translate), [messages]);
}

export type { Translate };
