'use client';

import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { useAuth } from './AuthProvider';
import { useLocale } from './LocaleProvider';
import { updateLocalePreference } from '@/lib/api/user';
import {
  readBrowserChoice,
  replaceBrowserChoiceIf,
  writeBrowserChoice,
} from '@/lib/i18n/browserChoice';
import { accountChoice, encodeLocaleChoice, planLocaleSync } from '@/lib/i18n/choice';

/**
 * Reconcile the browser's language choice with the signed-in account's (see
 * `planLocaleSync`).
 *
 * It runs again on every occasion a choice that failed to store could now
 * succeed: each profile load (`refreshUser` hands over a new user object, even
 * when nothing in it changed), the browser coming back online, and the window
 * regaining focus. Planning is idempotent — once the two agree there is
 * nothing to do — and one request per choice is in flight at a time.
 *
 * An answer is written back only if the same account is still signed in and
 * the browser still holds the choice that was sent; otherwise something newer
 * has happened and the answer is dropped.
 *
 * Renders nothing. Mounted once, inside both the auth and the locale providers.
 */
export function LocaleAccountSync() {
  const router = useRouter();
  const current = useLocale();
  const { state } = useAuth();
  const user = state.user;
  const userId = user?.id ?? null;

  // Read when an answer arrives, after this render's values may have moved on.
  const activeUser = useRef(userId);
  activeUser.current = userId;
  const shown = useRef(current);
  shown.current = current;
  const inFlight = useRef(new Set<string>());

  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const again = () => setRetry((n) => n + 1);
    window.addEventListener('online', again);
    window.addEventListener('focus', again);
    return () => {
      window.removeEventListener('online', again);
      window.removeEventListener('focus', again);
    };
  }, []);

  useEffect(() => {
    if (!user || !userId) return;
    const plan = planLocaleSync(readBrowserChoice(), accountChoice(user.locale), userId);

    if (plan.kind === 'adopt') {
      writeBrowserChoice({
        locale: plan.locale,
        chosenAt: plan.chosenAt,
        owner: userId,
        stored: true,
      });
      if (plan.locale !== shown.current) router.refresh();
      return;
    }
    if (plan.kind !== 'store') return;

    const sent = plan.choice;
    const key = `${userId}:${encodeLocaleChoice(sent)}`;
    if (inFlight.current.has(key)) return;
    inFlight.current.add(key);
    updateLocalePreference(sent.locale, sent.chosenAt)
      .then((answer) => {
        const kept = accountChoice(answer);
        if (!kept || activeUser.current !== userId) return;
        if (!replaceBrowserChoiceIf(sent, { ...kept, owner: userId, stored: true })) return;
        // The account kept a more recent choice made on another browser.
        if (kept.locale !== shown.current) router.refresh();
      })
      .catch((error) => {
        console.warn('Could not store the language choice in the account:', error);
      })
      .finally(() => {
        inFlight.current.delete(key);
      });
    // `user` is followed by identity on purpose: each profile load is a chance
    // to retry.
  }, [user, userId, retry, router]);

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
