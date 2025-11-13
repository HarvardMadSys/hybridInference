'use client';

import { useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { verifyEmail } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import Link from 'next/link';
import { Card } from '@/components/ui/Card';

export default function VerifyEmailPage(): JSX.Element {
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading');
  const [message, setMessage] = useState('');

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
        setStatus('error');
        setMessage(getErrorMessage(err));
      });
  }, [searchParams]);

  return (
    <div className="mx-auto max-w-md">
      <Card>
        {status === 'loading' && (
          <>
            <h1 className="text-2xl font-semibold">Verifying...</h1>
            <div className="mt-4 flex justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
            </div>
          </>
        )}

        {status === 'success' && (
          <>
            <h1 className="text-2xl font-semibold text-green-600">Verification Successful!</h1>
            <p className="mt-4 text-sm text-gray-600">{message}</p>
            <div className="mt-6">
              <Link
                href="/login"
                className="inline-block rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
              >
                Go to Login
              </Link>
            </div>
          </>
        )}

        {status === 'error' && (
          <>
            <h1 className="text-2xl font-semibold text-red-600">Verification Failed</h1>
            <p className="mt-4 text-sm text-gray-600">{message}</p>
            <div className="mt-6 space-x-4">
              <Link
                href="/signup"
                className="text-sm font-medium text-blue-600 hover:text-blue-700"
              >
                Sign Up Again
              </Link>
              <Link href="/login" className="text-sm font-medium text-blue-600 hover:text-blue-700">
                Back to Login
              </Link>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
