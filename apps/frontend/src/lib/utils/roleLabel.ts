import type { Translate } from '@/lib/i18n/translate';

/**
 * An account's plan, as the reader sees it; an unknown role shows as stored.
 * The English is the stored value itself, as it was shown before translation.
 */
export function roleLabel(role: string | undefined, t: Translate): string {
  switch (role) {
    case 'free':
      return t('chrome.role.free', 'free');
    case 'pro':
      return t('chrome.role.pro', 'pro');
    case 'internal':
      return t('chrome.role.internal', 'internal');
    case 'admin':
      return t('chrome.role.admin', 'admin');
    default:
      return role ?? '';
  }
}
