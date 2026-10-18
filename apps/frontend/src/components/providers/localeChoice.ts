'use client';

import { useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';

// Through the barrel, like the console's other components, so a test that
// stands in for `useAuth` there stands in for it here too.
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
import { updateLocalePreference } from '@/lib/api/user';
import { replaceBrowserChoiceIf, writeBrowserChoice } from '@/lib/i18n/browserChoice';
import { accountChoice, type LocaleChoice } from '@/lib/i18n/choice';
import type { Locale } from '@/lib/i18n/locales';

/**
 * Choose the interface language.
 *
 * The browser keeps the choice at once, owned by the signed-in account (or by
 * nobody, signed out) and not yet stored, and the route refreshes so the
 * server renders it in the new language. A signed-in reader's choice is then
 * stored in their account, which is what makes it follow them to another
 * browser.
 *
 * The answer is written back only if nothing has moved on while it was out:
 * the same account is still signed in, and the browser still holds exactly this
 * choice — not a newer one, and not another account's adopted after a switch.
 * If storing fails, the choice stays unstored and `LocaleAccountSync` retries.
 */
export function useChooseLocale(): (next: Locale) => Promise<void> {
  const router = useRouter();
  const current = useLocale();
  const { state, refreshUser } = useAuth();
  const userId = state.isAuthenticated ? (state.user?.id ?? null) : null;
  // Read after the request returns, when this render's `userId` may be stale.
  const activeUser = useRef(userId);
  activeUser.current = userId;

  return useCallback(
    async (next: Locale) => {
      const owner = userId;
      const choice: LocaleChoice = { locale: next, chosenAt: Date.now(), owner, stored: false };
      writeBrowserChoice(choice);
      if (next !== current) router.refresh();
      if (owner === null) return;
      try {
        const kept = accountChoice(await updateLocalePreference(next));
        if (!kept || activeUser.current !== owner) return;
        if (!replaceBrowserChoiceIf(choice, { ...kept, owner, stored: true })) return;
        // The account kept a more recent choice made on another browser.
        if (kept.locale !== next) router.refresh();
        await refreshUser();
      } catch (error) {
        console.warn('Could not store the language choice in the account:', error);
      }
    },
    [current, refreshUser, router, userId],
  );
}
