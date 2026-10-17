import { describe, expect, it } from 'vitest';

import {
  accountChoice,
  choiceFromCookieHeader,
  decodeLocaleChoice,
  encodeLocaleChoice,
  localeCookieString,
  planLocaleSync,
  resolveRequestLocale,
  type LocaleChoice,
} from './choice';
import { localeForTag, matchAcceptLanguage } from './locales';

/**
 * First visit follows the system language; after that, the reader's most
 * recent choice — across browsers through the account, and without one
 * account's choice overwriting another's on a shared browser.
 */

describe('the browser language', () => {
  it('reads every Chinese tag as the one Chinese, simplified or traditional', () => {
    for (const tag of ['zh', 'zh-CN', 'zh-Hans', 'zh-SG', 'zh-TW', 'zh-HK', 'ZH-tw']) {
      expect(localeForTag(tag)).toBe('zh-CN');
    }
    expect(localeForTag('en-GB')).toBe('en');
    expect(localeForTag('fr-FR')).toBeNull();
  });

  it('takes the highest-quality supported language, in header order on a tie', () => {
    expect(matchAcceptLanguage('zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7')).toBe('zh-CN');
    expect(matchAcceptLanguage('en-US,en;q=0.9,zh-CN;q=0.8')).toBe('en');
    expect(matchAcceptLanguage('en;q=0.5, zh-CN;q=0.9')).toBe('zh-CN');
    expect(matchAcceptLanguage('fr-FR,fr;q=0.9,zh;q=0.4')).toBe('zh-CN');
  });

  it('skips refused, wildcard and unsupported languages', () => {
    expect(matchAcceptLanguage('zh;q=0, en;q=0.1')).toBe('en');
    expect(matchAcceptLanguage('*')).toBeNull();
    expect(matchAcceptLanguage('fr, de;q=0.5')).toBeNull();
    expect(matchAcceptLanguage('')).toBeNull();
    expect(matchAcceptLanguage(null)).toBeNull();
  });
});

describe('the request language', () => {
  const chosen = encodeLocaleChoice({ locale: 'en', chosenAt: 1_700_000_000_000, pending: false });

  it("follows the reader's choice over their browser", () => {
    expect(resolveRequestLocale({ cookie: chosen, acceptLanguage: 'zh-CN' })).toBe('en');
  });

  it('follows the browser when the reader never chose', () => {
    expect(resolveRequestLocale({ cookie: undefined, acceptLanguage: 'zh-CN,zh;q=0.9' })).toBe(
      'zh-CN',
    );
  });

  it('falls back to English for anything else', () => {
    expect(resolveRequestLocale({ cookie: 'garbage', acceptLanguage: 'fr' })).toBe('en');
    expect(resolveRequestLocale({ cookie: null, acceptLanguage: null })).toBe('en');
  });
});

describe('the choice cookie', () => {
  const choice: LocaleChoice = { locale: 'zh-CN', chosenAt: 1_760_700_000_000, pending: true };

  it('round-trips, encoded or not', () => {
    const value = encodeLocaleChoice(choice);
    expect(value).toBe('zh-CN|1760700000000|p');
    expect(decodeLocaleChoice(value)).toEqual(choice);
    expect(decodeLocaleChoice(encodeURIComponent(value))).toEqual(choice);
  });

  it('refuses a value this build did not write', () => {
    for (const value of [
      'fr|1760700000000|s',
      'zh-CN|soon|s',
      'zh-CN|-5|s',
      'zh-CN|1760700000000|x',
      'zh-CN|1760700000000|s|extra',
      'zh-CN',
      '%E0%A4%A',
    ]) {
      expect(decodeLocaleChoice(value)).toBeNull();
    }
  });

  it('is site-wide, long-lived, and Secure only over HTTPS', () => {
    const plain = localeCookieString(choice, false);
    expect(plain).toBe(
      'hi_locale=zh-CN%7C1760700000000%7Cp; Path=/; Max-Age=31536000; SameSite=Lax',
    );
    expect(localeCookieString(choice, true)).toBe(`${plain}; Secure`);
    expect(choiceFromCookieHeader(`a=1; ${plain.split(';')[0]}; b=2`)).toEqual(choice);
    expect(choiceFromCookieHeader('a=1')).toBeNull();
  });
});

describe('syncing the browser with the signed-in account', () => {
  const T0 = 1_760_000_000_000;
  const account = { locale: 'en' as const, chosenAt: T0 };

  it('leaves a reader who never chose on their system language', () => {
    expect(planLocaleSync(null, null)).toEqual({ kind: 'none' });
  });

  it("brings the account's choice to a browser that has none", () => {
    expect(planLocaleSync(null, account)).toEqual({ kind: 'adopt', locale: 'en', chosenAt: T0 });
  });

  it('stores a choice made signed out in an account that has none', () => {
    const browser: LocaleChoice = { locale: 'zh-CN', chosenAt: T0, pending: true };
    expect(planLocaleSync(browser, null)).toEqual({ kind: 'store', locale: 'zh-CN', chosenAt: T0 });
  });

  it("stores a pending choice newer than the account's", () => {
    const browser: LocaleChoice = { locale: 'zh-CN', chosenAt: T0 + 1, pending: true };
    expect(planLocaleSync(browser, account)).toEqual({
      kind: 'store',
      locale: 'zh-CN',
      chosenAt: T0 + 1,
    });
  });

  it("takes the account's choice over an older pending one — chosen since, elsewhere", () => {
    const browser: LocaleChoice = { locale: 'zh-CN', chosenAt: T0 - 1, pending: true };
    expect(planLocaleSync(browser, account)).toEqual({ kind: 'adopt', locale: 'en', chosenAt: T0 });
  });

  it("never writes another account's stored choice into this one", () => {
    // Account A chose Chinese after B chose English; switching to B shows B's.
    const fromA: LocaleChoice = { locale: 'zh-CN', chosenAt: T0 + 60_000, pending: false };
    expect(planLocaleSync(fromA, account)).toEqual({ kind: 'adopt', locale: 'en', chosenAt: T0 });
    // And an account that never chose is not given A's choice.
    expect(planLocaleSync(fromA, null)).toEqual({ kind: 'none' });
  });

  it('does nothing once the two agree', () => {
    const browser: LocaleChoice = { locale: 'en', chosenAt: T0, pending: false };
    expect(planLocaleSync(browser, account)).toEqual({ kind: 'none' });
  });

  it('ignores a stored choice this build cannot show', () => {
    expect(accountChoice({ value: 'fr', chosen_at: '2026-10-17T00:00:00Z' })).toBeNull();
    expect(accountChoice({ value: 'en', chosen_at: 'yesterday' })).toBeNull();
    expect(accountChoice({ value: 'zh-CN', chosen_at: '2026-10-17T00:00:00Z' })).toEqual({
      locale: 'zh-CN',
      chosenAt: Date.parse('2026-10-17T00:00:00Z'),
    });
  });
});
