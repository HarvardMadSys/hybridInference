'use client';

import { Suspense, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import toast from 'react-hot-toast';
import { resendVerification, verifyEmail } from '@/lib/api/auth';
import { getErrorMessage, APIError } from '@/lib/utils/errors';
import { useT } from '@/components/providers/useT';
import { AuthField, AuthLoading, AuthNotice } from '@/components/auth/AuthForm';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';
import { fill } from '@/lib/utils/interpolate';

export const dynamic = 'force-dynamic';

function VerifyEmailContent(): JSX.Element {
  const t = useT();
  const skin = useAuthAppearance();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<'loading' | 'success' | 'error' | 'already_verified'>(
    'loading',
  );
  const [message, setMessage] = useState('');
  // A bad/expired verification link carries no email, so collect one here to
  // request a fresh verification email instead of dead-ending the user.
  const [resendEmail, setResendEmail] = useState('');
  const [isResending, setIsResending] = useState(false);
  const [resendDone, setResendDone] = useState(false);

  const handleResend = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (!resendEmail.trim()) return;
    setIsResending(true);
    try {
      await resendVerification(resendEmail.trim());
      setResendDone(true);
      toast.success(
        t('auth.verify.resent_toast', 'Verification email sent. Please check your inbox.'),
      );
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setIsResending(false);
    }
  };

  useEffect(() => {
    const token = searchParams.get('token');

    if (!token) {
      setStatus('error');
      setMessage(t('auth.verify.missing_token', 'Missing verification token'));
      return;
    }

    verifyEmail(token)
      .then((response) => {
        setStatus('success');
        setMessage(
          response.message || t('auth.verify.success_fallback', 'Email verified successfully!'),
        );
      })
      .catch((err) => {
        // Check if token was already used (user already verified)
        if (err instanceof APIError && err.code === 'TOKEN_ALREADY_USED') {
          setStatus('already_verified');
          setMessage(
            t(
              'auth.verify.already_message',
              'Your email has already been verified. You can login now.',
            ),
          );
        } else {
          setStatus('error');
          setMessage(getErrorMessage(err));
        }
      });
  }, [searchParams, t]);

  const titles: Record<typeof status, string> = {
    loading: t('auth.verify.loading_title', 'Verifying Email'),
    success: t('auth.verify.success_title', 'Email Verified'),
    already_verified: t('auth.verify.already_title', 'Already Verified'),
    error: t('auth.verify.error_title', 'Verification Failed'),
  };
  const bodies: Record<typeof status, string> = {
    loading: t('auth.verify.loading_body', 'Please wait while we verify your email address.'),
    success: t(
      'auth.verify.success_body',
      'Your account is now active. You can log in to continue.',
    ),
    already_verified: t(
      'auth.verify.already_body',
      'Your email is already verified. You can log in to continue.',
    ),
    error: t('auth.verify.error_body', 'We were unable to verify your email.'),
  };
  const verified = status === 'success' || status === 'already_verified';

  return (
    <AuthPageFrame
      page="verify-email"
      kicker={t('auth.verify.kicker', 'EMAIL VERIFICATION')}
      title={titles[status]}
      subtitle={bodies[status]}
      legal={
        <>
          {t('auth.legal.see', 'See')}{' '}
          <Link href="/terms">{t('chrome.footer.terms', 'Terms')}</Link>
        </>
      }
    >
      <div className={skin.form} data-auth="form">
        {status === 'loading' && <AuthLoading />}

        {status === 'error' && message ? <AuthNotice tone="error">{message}</AuthNotice> : null}

        {verified && message ? <AuthNotice tone="ok">{message}</AuthNotice> : null}

        {verified && (
          <Link href="/login" className={skin.submit} data-auth="submit" prefetch={false}>
            {t('auth.verify.go_to_login', 'Go to Login')}
          </Link>
        )}

        {status === 'error' &&
          (resendDone ? (
            <AuthNotice tone="ok">
              <p>
                {fill(
                  t(
                    'auth.verify.resent_notice',
                    'A new verification email is on its way to {email}. Please check your inbox (and spam folder), then follow the link to finish verifying.',
                  ),
                  { email: resendEmail.trim() },
                )}
              </p>
              <button
                type="button"
                onClick={() => setResendDone(false)}
                className={skin.linkButton}
              >
                {t('auth.verify.try_another', 'Entered the wrong email? Try another one')}
              </button>
            </AuthNotice>
          ) : (
            <form onSubmit={handleResend} className={skin.form} data-auth="form">
              <p className="text-sm">
                {t(
                  'auth.verify.resend_prompt',
                  "Need a new link? Enter your email and we'll send a fresh verification email.",
                )}
              </p>
              <AuthField id="resend-email" label={t('auth.verify.email_label', 'Email')}>
                <input
                  id="resend-email"
                  className={skin.input}
                  data-auth="control"
                  type="email"
                  autoComplete="email"
                  value={resendEmail}
                  onChange={(e) => setResendEmail(e.target.value)}
                  required
                />
              </AuthField>
              <button
                type="submit"
                className={skin.submit}
                data-auth="submit"
                disabled={isResending}
                aria-busy={isResending}
              >
                {t('auth.verify.resend_button', 'Resend verification email')}
              </button>
            </form>
          ))}

        {status === 'error' && (
          <div className="flex items-center justify-center gap-4" data-auth="secondary-actions">
            <Link href="/signup" className={skin.linkButton} prefetch={false}>
              {t('auth.verify.signup_again', 'Sign Up Again')}
            </Link>
            <Link href="/login" className={skin.linkButton} prefetch={false}>
              {t('auth.verify.back_to_login', 'Back to Login')}
            </Link>
          </div>
        )}
      </div>
    </AuthPageFrame>
  );
}

export default function VerifyEmailPage(): JSX.Element {
  return (
    <Suspense fallback={<AuthLoading />}>
      <VerifyEmailContent />
    </Suspense>
  );
}
