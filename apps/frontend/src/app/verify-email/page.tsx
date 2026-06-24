'use client';

import { Suspense, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import toast from 'react-hot-toast';
import { resendVerification, verifyEmail } from '@/lib/api/auth';
import { getErrorMessage, APIError } from '@/lib/utils/errors';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';

export const dynamic = 'force-dynamic';

function VerifyEmailContent(): JSX.Element {
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
      toast.success('Verification email sent. Please check your inbox.');
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
      setMessage('Missing verification token');
      return;
    }

    verifyEmail(token)
      .then((response) => {
        setStatus('success');
        setMessage(response.message || 'Email verified successfully!');
      })
      .catch((err) => {
        // Check if token was already used (user already verified)
        if (err instanceof APIError && err.code === 'TOKEN_ALREADY_USED') {
          setStatus('already_verified');
          setMessage('Your email has already been verified. You can login now.');
        } else {
          setStatus('error');
          setMessage(getErrorMessage(err));
        }
      });
  }, [searchParams]);

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">
            {status === 'loading' && 'Verifying Email'}
            {status === 'success' && 'Email Verified'}
            {status === 'already_verified' && 'Already Verified'}
            {status === 'error' && 'Verification Failed'}
          </h1>
          <p className="mt-2 text-sm text-gray-600">
            {status === 'loading' && 'Please wait while we verify your email address.'}
            {status === 'success' && 'Your account is now active. You can log in to continue.'}
            {status === 'already_verified' &&
              'Your email is already verified. You can log in to continue.'}
            {status === 'error' && 'We were unable to verify your email.'}
          </p>
        </div>

        <div className="mt-8 space-y-6">
          {status === 'loading' && (
            <div className="flex justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
            </div>
          )}

          {status === 'error' && (
            <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-red-700">
              {message}
            </div>
          )}

          {(status === 'success' || status === 'already_verified') && (
            <div className="rounded border border-green-200 bg-green-50 px-4 py-3 text-green-700">
              {message}
            </div>
          )}

          <div className="flex flex-col items-center gap-4">
            {(status === 'success' || status === 'already_verified') && (
              <Link href="/login" className="w-full">
                <Button className="w-full">Go to Login</Button>
              </Link>
            )}

            {status === 'error' &&
              (resendDone ? (
                <div className="w-full rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">
                  <p>
                    A new verification email is on its way to {resendEmail.trim()}. Please check
                    your inbox (and spam folder), then follow the link to finish verifying.
                  </p>
                  <button
                    type="button"
                    onClick={() => setResendDone(false)}
                    className="mt-2 text-xs font-medium text-green-800 underline hover:text-green-900"
                  >
                    Entered the wrong email? Try another one
                  </button>
                </div>
              ) : (
                <form onSubmit={handleResend} className="w-full space-y-3">
                  <p className="text-sm text-gray-600">
                    Need a new link? Enter your email and we&apos;ll send a fresh verification
                    email.
                  </p>
                  <InputField
                    label="Email"
                    type="email"
                    autoComplete="email"
                    value={resendEmail}
                    onChange={(e) => setResendEmail(e.target.value)}
                    required
                  />
                  <Button type="submit" className="w-full" isLoading={isResending}>
                    Resend verification email
                  </Button>
                </form>
              ))}

            {status === 'error' && (
              <div className="flex items-center gap-4">
                <Link
                  href="/signup"
                  className="text-sm font-medium text-blue-600 hover:text-blue-700"
                >
                  Sign Up Again
                </Link>
                <Link
                  href="/login"
                  className="text-sm font-medium text-blue-600 hover:text-blue-700"
                >
                  Back to Login
                </Link>
              </div>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}

export default function VerifyEmailPage(): JSX.Element {
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-md">
          <Card>
            <div className="text-center">
              <h1 className="text-3xl font-bold tracking-tight text-gray-900">Verifying Email</h1>
              <p className="mt-2 text-sm text-gray-600">
                Please wait while we verify your email address.
              </p>
            </div>
            <div className="mt-8 flex justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600" />
            </div>
          </Card>
        </div>
      }
    >
      <VerifyEmailContent />
    </Suspense>
  );
}
