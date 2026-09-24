'use client';

import { useMemo } from 'react';

import { SITE_UI_CLIENT } from '@/site-ui/module';
import { translator, type Translate } from '@/lib/i18n/translate';

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
 * It cannot invent a slot or reach a console page's copy, and it cannot change
 * a validation rule, because the rules pass their own message through this
 * function rather than being read from it. A declared key set to `''` resolves
 * to the empty string: that is a module removing a sentence on purpose, not a
 * missing translation.
 */
export function useT(): Translate {
  const messages = SITE_UI_CLIENT.authMessages;
  return useMemo(() => translator(messages), [messages]);
}

export type { Translate };
