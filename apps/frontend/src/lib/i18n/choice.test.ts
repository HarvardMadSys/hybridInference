import { describe, expect, it } from 'vitest';

import {
  accountChoice,
  choiceFromCookieHeader,
  decodeLocaleChoice,
  encodeLocaleChoice,
  localeCookieString,
  planLocaleSync,
  resolveRequestLocale,
  sameChoice,
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
  const chosen = encodeLocaleChoice({
    locale: 'en',
    chosenAt: 1_700_000_000_000,
    owner: 'u1',
    stored: true,
  });

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
  const signedOut: LocaleChoice = {
    locale: 'zh-CN',
    chosenAt: 1_760_700_000_000,
    owner: null,
    stored: false,
  };
  const owned: LocaleChoice = { ...signedOut, owner: '01M54DWM1J5W1WR6S35HQX2ZHN' };

  it('round-trips each kind of choice, encoded or not', () => {
    expect(encodeLocaleChoice(signedOut)).toBe('zh-CN|1760700000000|a');
    expect(encodeLocaleChoice(owned)).toBe('zh-CN|1760700000000|p|01M54DWM1J5W1WR6S35HQX2ZHN');
    expect(encodeLocaleChoice({ ...owned, stored: true })).toBe(
      'zh-CN|1760700000000|s|01M54DWM1J5W1WR6S35HQX2ZHN',
    );
    for (const choice of [signedOut, owned, { ...owned, stored: true }]) {
      const value = encodeLocaleChoice(choice);
      expect(decodeLocaleChoice(value)).toEqual(choice);
      expect(decodeLocaleChoice(encodeURIComponent(value))).toEqual(choice);
    }
  });

  it('refuses a value this build did not write', () => {
    for (const value of [
      'fr|1760700000000|a',
      'zh-CN|soon|a',
      'zh-CN|-5|a',
      'zh-CN|1760700000000|x',
      'zh-CN|1760700000000|a|u1',
      'zh-CN|1760700000000|s',
      'zh-CN|1760700000000|p|bad id',
      'zh-CN|1760700000000|s|u1|extra',
      'zh-CN|1760700000000|p',
      'zh-CN',
      '%E0%A4%A',
    ]) {
      expect(decodeLocaleChoice(value), value).toBeNull();
    }
  });

  it('tells choices apart by every field', () => {
    expect(sameChoice(owned, { ...owned })).toBe(true);
    expect(sameChoice(owned, { ...owned, stored: true })).toBe(false);
    expect(sameChoice(owned, { ...owned, owner: 'u2' })).toBe(false);
    expect(sameChoice(owned, { ...owned, chosenAt: owned.chosenAt + 1 })).toBe(false);
    expect(sameChoice(owned, null)).toBe(false);
  });

  it('is site-wide, long-lived, and Secure only over HTTPS', () => {
    const plain = localeCookieString(signedOut, false);
    expect(plain).toBe(
      'hi_locale=zh-CN%7C1760700000000%7Ca; Path=/; Max-Age=31536000; SameSite=Lax',
    );
    expect(localeCookieString(signedOut, true)).toBe(`${plain}; Secure`);
    expect(choiceFromCookieHeader(`a=1; ${plain.split(';')[0]}; b=2`)).toEqual(signedOut);
    expect(choiceFromCookieHeader('a=1')).toBeNull();
  });
});

describe('syncing the browser with the signed-in account', () => {
  const T0 = 1_760_000_000_000;
  const A = 'account-a';
  const B = 'account-b';
  const accountEn = { locale: 'en' as const, chosenAt: T0 };

  it('leaves a reader who never chose on their system language', () => {
    expect(planLocaleSync(null, null, A)).toEqual({ kind: 'none' });
  });

  it("brings the account's choice to a browser that has none", () => {
    expect(planLocaleSync(null, accountEn, A)).toEqual({
      kind: 'adopt',
      locale: 'en',
      chosenAt: T0,
    });
  });

  it('stores a signed-out choice in an account that has none, or an older one', () => {
    const signedOut: LocaleChoice = {
      locale: 'zh-CN',
      chosenAt: T0 + 1,
      owner: null,
      stored: false,
    };
    expect(planLocaleSync(signedOut, null, A)).toEqual({ kind: 'store', choice: signedOut });
    expect(planLocaleSync(signedOut, accountEn, A)).toEqual({ kind: 'store', choice: signedOut });
  });

  it("retries this account's own unstored choice", () => {
    const unstored: LocaleChoice = { locale: 'zh-CN', chosenAt: T0 + 1, owner: A, stored: false };
    expect(planLocaleSync(unstored, accountEn, A)).toEqual({ kind: 'store', choice: unstored });
  });

  it("takes the account's choice over an older unstored one — chosen since, elsewhere", () => {
    const older: LocaleChoice = { locale: 'zh-CN', chosenAt: T0 - 1, owner: null, stored: false };
    expect(planLocaleSync(older, accountEn, A)).toEqual({
      kind: 'adopt',
      locale: 'en',
      chosenAt: T0,
    });
  });

  it("never writes account A's unstored choice into account B", () => {
    // A chose Chinese, storing it failed, and the reader switched to B.
    const failedInA: LocaleChoice = {
      locale: 'zh-CN',
      chosenAt: T0 + 60_000,
      owner: A,
      stored: false,
    };
    expect(planLocaleSync(failedInA, accountEn, B)).toEqual({
      kind: 'adopt',
      locale: 'en',
      chosenAt: T0,
    });
    expect(planLocaleSync(failedInA, null, B)).toEqual({ kind: 'none' });
    // Back in A, it is A's to retry.
    expect(planLocaleSync(failedInA, null, A)).toEqual({ kind: 'store', choice: failedInA });
  });

  it("never writes account A's stored choice into account B", () => {
    const storedInA: LocaleChoice = {
      locale: 'zh-CN',
      chosenAt: T0 + 60_000,
      owner: A,
      stored: true,
    };
    expect(planLocaleSync(storedInA, accountEn, B)).toEqual({
      kind: 'adopt',
      locale: 'en',
      chosenAt: T0,
    });
    expect(planLocaleSync(storedInA, null, B)).toEqual({ kind: 'none' });
  });

  it('does nothing once the two agree', () => {
    const agreed: LocaleChoice = { locale: 'en', chosenAt: T0, owner: A, stored: true };
    expect(planLocaleSync(agreed, accountEn, A)).toEqual({ kind: 'none' });
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
