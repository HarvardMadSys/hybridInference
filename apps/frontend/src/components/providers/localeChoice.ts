'use client';

import { useCallback } from 'react';
import { useRouter } from 'next/navigation';

// Through the barrel, like the console's other components, so a test that
// stands in for `useAuth` there stands in for it here too.
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
import { updateLocalePreference } from '@/lib/api/user';
import { writeBrowserChoice } from '@/lib/i18n/browserChoice';
import { accountChoice } from '@/lib/i18n/choice';
import type { Locale } from '@/lib/i18n/locales';

/**
 * Choose the interface language.
 *
 * The browser keeps the choice at once — as *pending*, so that if storing it
 * in the account fails, the next sync still carries it there — and the route
 * refreshes so the server renders it in the new language. A signed-in reader's
 * choice is then stored in their account, which is what makes it follow them
 * to another browser.
 */
export function useChooseLocale(): (next: Locale) => Promise<void> {
  const router = useRouter();
  const current = useLocale();
  const { state, refreshUser } = useAuth();
  const signedIn = state.isAuthenticated;

  return useCallback(
    async (next: Locale) => {
      writeBrowserChoice({ locale: next, chosenAt: Date.now(), pending: true });
      if (next !== current) router.refresh();
      if (!signedIn) return;
      try {
        const stored = accountChoice(await updateLocalePreference(next));
        if (stored) writeBrowserChoice({ ...stored, pending: false });
        await refreshUser();
      } catch (error) {
        // The browser still shows the choice, and it stays pending: the next
        // sign-in or profile load stores it.
        console.warn('Could not store the language choice in the account:', error);
      }
    },
    [current, refreshUser, router, signedIn],
  );
}
