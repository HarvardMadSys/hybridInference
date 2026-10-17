'use client';

import { useEffect, useRef } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { useAuth } from './AuthProvider';
import { useLocale } from './LocaleProvider';
import { updateLocalePreference } from '@/lib/api/user';
import { readBrowserChoice, writeBrowserChoice } from '@/lib/i18n/browserChoice';
import { accountChoice, planLocaleSync } from '@/lib/i18n/choice';

/**
 * Reconcile the browser's language choice with the signed-in account's, each
 * time the account or its stored choice changes (see `planLocaleSync`).
 *
 * Renders nothing. Mounted once, inside both the auth and the locale
 * providers.
 */
export function LocaleAccountSync() {
  const router = useRouter();
  const current = useLocale();
  const { state } = useAuth();
  const userId = state.user?.id ?? null;
  const storedValue = state.user?.locale?.value;
  const storedAt = state.user?.locale?.chosen_at;

  useEffect(() => {
    if (!userId) return;
    const account = accountChoice(
      storedValue && storedAt ? { value: storedValue, chosen_at: storedAt } : null,
    );
    const plan = planLocaleSync(readBrowserChoice(), account);

    if (plan.kind === 'adopt') {
      writeBrowserChoice({ locale: plan.locale, chosenAt: plan.chosenAt, pending: false });
      if (plan.locale !== current) router.refresh();
      return;
    }
    if (plan.kind === 'store') {
      let cancelled = false;
      updateLocalePreference(plan.locale, plan.chosenAt)
        .then((stored) => {
          const confirmed = accountChoice(stored);
          // A newer choice made while the request was out wins over this one.
          const latest = readBrowserChoice();
          if (cancelled || !confirmed || (latest && latest.chosenAt > plan.chosenAt)) return;
          writeBrowserChoice({ ...confirmed, pending: false });
          // The account kept a choice made on another browser since this one:
          // that is the most recent, so it is the one shown.
          if (confirmed.locale !== plan.locale) router.refresh();
        })
        .catch((error) => {
          console.warn('Could not store the language choice in the account:', error);
        });
      return () => {
        cancelled = true;
      };
    }
    // `current` is read, not followed: a language change is this effect's
    // output, and re-running on it would only re-plan the same state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, storedValue, storedAt, router]);

  // A refresh issued just before a navigation is dropped by it — signing in
  // adopts the account's language and then the sign-in page moves on to the
  // dashboard — and moving between pages does not re-render the root layout
  // that hands the language down. So after each navigation, a page still in a
  // language other than the browser's choice asks for the right one.
  const pathname = usePathname();
  const lastPathname = useRef(pathname);
  useEffect(() => {
    // Only after a navigation: a page load renders from the cookie itself.
    if (lastPathname.current === pathname) return;
    lastPathname.current = pathname;
    const browser = readBrowserChoice();
    if (browser && browser.locale !== current) router.refresh();
  }, [pathname, current, router]);

  return null;
}
