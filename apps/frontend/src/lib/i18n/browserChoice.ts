import { choiceFromCookieHeader, localeCookieString, type LocaleChoice } from './choice';

/** The choice this browser holds, read from `document.cookie`. */
export function readBrowserChoice(): LocaleChoice | null {
  return choiceFromCookieHeader(document.cookie);
}

/** Keep a choice in this browser, for the server to read on the next request. */
export function writeBrowserChoice(choice: LocaleChoice): void {
  document.cookie = localeCookieString(choice, window.location.protocol === 'https:');
}
