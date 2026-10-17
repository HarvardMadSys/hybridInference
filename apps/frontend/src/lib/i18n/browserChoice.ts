import {
  choiceFromCookieHeader,
  localeCookieString,
  sameChoice,
  type LocaleChoice,
} from './choice';

/** The choice this browser holds, read from `document.cookie`. */
export function readBrowserChoice(): LocaleChoice | null {
  return choiceFromCookieHeader(document.cookie);
}

/** Keep a choice in this browser, for the server to read on the next request. */
export function writeBrowserChoice(choice: LocaleChoice): void {
  document.cookie = localeCookieString(choice, window.location.protocol === 'https:');
}

/**
 * Replace the browser's choice only if it is still `expected`, and say whether
 * it was.
 *
 * For writing back the answer to a request: by the time it arrives the reader
 * may have chosen again, or the browser may have switched to another account
 * and taken that account's choice, and the answer must not undo either.
 */
export function replaceBrowserChoiceIf(expected: LocaleChoice, next: LocaleChoice): boolean {
  if (!sameChoice(readBrowserChoice(), expected)) return false;
  writeBrowserChoice(next);
  return true;
}
