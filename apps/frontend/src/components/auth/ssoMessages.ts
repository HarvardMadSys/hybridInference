import type { Translate } from '@/components/providers/useT';
import type { SsoErrorCode } from '@/lib/api/sso';
import { fill } from '@/lib/utils/interpolate';

/**
 * The sentence for one `sso_error` code.
 *
 * The page a round trip returns to is told only the code; what the provider
 * itself said stays in the gateway's log. Each sentence is whole, so that a
 * language with another word order translates it as one.
 */
export function ssoErrorMessage(t: Translate, code: SsoErrorCode): string {
  switch (code) {
    case 'unavailable':
      return t(
        'auth.sso.error.unavailable',
        'Single sign-on is unavailable right now. Use another sign-in method.',
      );
    case 'rate_limited':
      return t('auth.sso.error.rate_limited', 'Too many sign-in attempts. Please try again later.');
    case 'cancelled':
      return t('auth.sso.error.cancelled', 'Single sign-on was cancelled.');
    case 'expired':
      return t('auth.sso.error.expired', 'The sign-in took too long. Start again.');
    case 'email_untrusted':
      return t(
        'auth.sso.error.email_untrusted',
        'We could not confirm the email of this organization account. Contact your administrator.',
      );
    case 'email_in_use':
      return t(
        'auth.sso.error.email_in_use',
        'The email of this organization account already belongs to another account here. Sign in to that account to connect it.',
      );
    case 'no_account':
      return t(
        'auth.sso.error.no_account',
        'This organization account has no account here yet. Contact your administrator.',
      );
    case 'identity_conflict':
      return t(
        'auth.sso.error.identity_conflict',
        'This account is already connected to a different identity from the same organization. Contact your administrator.',
      );
    case 'identity_in_use':
      return t(
        'auth.sso.error.identity_in_use',
        'This organization account is already connected to another account.',
      );
    case 'pending':
      return t('auth.sso.error.pending', 'Your registration is pending admin approval.');
    case 'suspended':
      return t('auth.sso.error.suspended', 'This account is suspended.');
    case 'inactive':
      return t('auth.sso.error.inactive', 'This account is not active. Please contact support.');
    case 'browser_full':
      return t(
        'auth.sso.error.browser_full',
        'This browser is already signed in to the maximum number of accounts. Sign one of them out first.',
      );
    case 'failed':
      return t('auth.sso.error.failed', 'Single sign-on failed. Try again later.');
  }
}

/**
 * Why a request that starts a round trip was refused, in the same words the
 * round trip itself fails with: the provider out of reach (503), or too many
 * attempts (429). `null` for anything else, which keeps its own message.
 */
export function ssoStartFailure(t: Translate, status: number | undefined): string | null {
  if (status === 503) return ssoErrorMessage(t, 'unavailable');
  if (status === 429) return ssoErrorMessage(t, 'rate_limited');
  return null;
}

/**
 * What linking does to an account that becomes a member of an organization
 * with it, said before the account is linked: on the step that asks, and in
 * the form that starts a link.
 */
export function ssoOnlyAfterLink(t: Translate, name: string): string {
  return fill(
    t(
      'auth.sso.link_sso_only',
      'After you connect it, this account signs in through {name} only. Its password is removed.',
    ),
    { name },
  );
}
