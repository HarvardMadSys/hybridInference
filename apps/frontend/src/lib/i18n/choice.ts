import { isLocale, matchAcceptLanguage, SOURCE_LOCALE, type Locale } from './locales';

/**
 * A reader's language choice: where it is kept, and how the browser's copy and
 * the account's copy are reconciled.
 *
 * Only a choice the reader made is ever stored. The language picked from the
 * browser's `Accept-Language` is worked out again on every request and never
 * written down, so "there is a record" means "the reader chose" — and a reader
 * who never chose keeps following their system language.
 */

/** The cookie the server reads on every request. */
export const LOCALE_COOKIE = 'hi_locale';

/** A year: the choice outlives a session, and every new choice renews it. */
const LOCALE_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

export interface LocaleChoice {
  locale: Locale;
  /** When the reader chose, in epoch milliseconds. */
  chosenAt: number;
  /**
   * True until an account has stored this choice: made while signed out, or
   * made signed in and not yet confirmed by the server. Only a pending choice
   * may be written into an account; a stored one belongs to the account that
   * stored it.
   */
  pending: boolean;
}

/** The account's copy, as `/user/me` returns it. */
export interface AccountLocaleChoice {
  value: string;
  chosen_at: string;
}

/**
 * `zh-CN|1760700000000|p` — three fields joined by `|`, none of which may hold
 * one. `p` marks a pending choice and `s` a stored one.
 */
export function encodeLocaleChoice(choice: LocaleChoice): string {
  return `${choice.locale}|${Math.trunc(choice.chosenAt)}|${choice.pending ? 'p' : 's'}`;
}

/** The choice in a cookie value, or `null` for anything this build did not write. */
export function decodeLocaleChoice(value: string | null | undefined): LocaleChoice | null {
  if (!value) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return null;
  }
  const [locale, chosenAt, state, ...rest] = decoded.split('|');
  if (rest.length > 0 || !isLocale(locale) || (state !== 'p' && state !== 's')) return null;
  const at = Number(chosenAt);
  if (!Number.isSafeInteger(at) || at <= 0) return null;
  return { locale, chosenAt: at, pending: state === 'p' };
}

/** The account's choice, or `null` when it holds none this build can show. */
export function accountChoice(
  stored: AccountLocaleChoice | null | undefined,
): Omit<LocaleChoice, 'pending'> | null {
  if (!stored || !isLocale(stored.value)) return null;
  const at = Date.parse(stored.chosen_at);
  if (!Number.isFinite(at)) return null;
  return { locale: stored.value, chosenAt: at };
}

/**
 * The language a request renders in: the reader's choice, else the best match
 * for their browser's languages, else English.
 */
export function resolveRequestLocale({
  cookie,
  acceptLanguage,
}: {
  cookie: string | null | undefined;
  acceptLanguage: string | null | undefined;
}): Locale {
  return decodeLocaleChoice(cookie)?.locale ?? matchAcceptLanguage(acceptLanguage) ?? SOURCE_LOCALE;
}

/** What to do once the signed-in account's choice is known. */
export type LocaleSyncPlan =
  | { kind: 'none' }
  /** Write the browser's pending choice into the account. */
  | { kind: 'store'; locale: Locale; chosenAt: number }
  /** Make the account's choice the browser's. */
  | { kind: 'adopt'; locale: Locale; chosenAt: number };

/**
 * Reconcile the browser's choice with the signed-in account's.
 *
 * The most recent choice wins, with one limit. A browser holds one active
 * account at a time and can switch between several; a choice the browser made
 * while account A was signed in, and A stored, must not overwrite account B's
 * when the reader switches to B. So only a *pending* choice — made signed out,
 * or not yet stored — is ever written into an account, and only when it is
 * newer than what the account has. Otherwise the account's choice, when it has
 * one, is the one shown.
 */
export function planLocaleSync(
  browser: LocaleChoice | null,
  account: Omit<LocaleChoice, 'pending'> | null,
): LocaleSyncPlan {
  if (browser?.pending && (!account || browser.chosenAt > account.chosenAt)) {
    return { kind: 'store', locale: browser.locale, chosenAt: browser.chosenAt };
  }
  if (!account) return { kind: 'none' };
  if (
    browser &&
    !browser.pending &&
    browser.locale === account.locale &&
    browser.chosenAt === account.chosenAt
  ) {
    return { kind: 'none' };
  }
  return { kind: 'adopt', locale: account.locale, chosenAt: account.chosenAt };
}

/** The `Set-Cookie`-style string `document.cookie` takes for a choice. */
export function localeCookieString(choice: LocaleChoice, secure: boolean): string {
  return [
    `${LOCALE_COOKIE}=${encodeURIComponent(encodeLocaleChoice(choice))}`,
    'Path=/',
    `Max-Age=${LOCALE_COOKIE_MAX_AGE_SECONDS}`,
    'SameSite=Lax',
    ...(secure ? ['Secure'] : []),
  ].join('; ');
}

/** Read the choice from a `document.cookie` string. */
export function choiceFromCookieHeader(cookieHeader: string): LocaleChoice | null {
  for (const part of cookieHeader.split(';')) {
    const [name, ...value] = part.trim().split('=');
    if (name === LOCALE_COOKIE) return decodeLocaleChoice(value.join('='));
  }
  return null;
}
