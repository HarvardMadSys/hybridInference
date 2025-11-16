'use client';

import { Suspense, useState, useEffect } from 'react';
import { useSearchParams } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { resetPassword } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { Card } from '@/components/ui/Card';

export const dynamic = 'force-dynamic';

const resetPasswordSchema = z
  .object({
    password: z
      .string()
      .min(8, 'Password must be at least 8 characters')
      .regex(/[A-Z]/, 'Password must contain at least one uppercase letter')
      .regex(/[a-z]/, 'Password must contain at least one lowercase letter')
      .regex(/[0-9]/, 'Password must contain at least one number'),
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: 'Passwords do not match',
    path: ['confirmPassword'],
  });

type ResetPasswordFormData = z.infer<typeof resetPasswordSchema>;

function ResetPasswordContent() {
  const searchParams = useSearchParams();
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    const tokenParam = searchParams.get('token');
    setToken(tokenParam);
    if (!tokenParam) {
      setError('Invalid or missing reset token');
    }
  }, [searchParams]);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ResetPasswordFormData>({
    resolver: zodResolver(resetPasswordSchema),
  });

  const onSubmit = async (data: ResetPasswordFormData) => {
    if (!token) {
      setError('Invalid or missing reset token');
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      await resetPassword(token, data.password);
      setSuccess(true);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsLoading(false);
    }
  };

  if (success) {
    return (
      <div className="mx-auto w-full max-w-md">
        <Card className="border-green-100">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-green-100">
            <svg
              className="h-6 w-6 text-green-600"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M5 13l4 4L19 7"
              />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-gray-900">Password Reset Successful!</h1>
          <p className="mt-3 text-base text-gray-600">
            Your password has been reset successfully. You can now log in with your new password.
          </p>
          <div className="mt-6">
            <a
              href="/login"
              className="inline-flex w-full items-center justify-center rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              Go to Login
            </a>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Reset Password</h1>
          <p className="mt-2 text-sm text-gray-600">Enter your new password below</p>
        </div>

        <form onSubmit={handleSubmit(onSubmit)} className="mt-8 space-y-5">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
              {error}
            </div>
          )}

          <InputField
            label="New Password"
            type="password"
            hint="At least 8 characters with uppercase, lowercase, and numbers"
            autoComplete="new-password"
            error={errors.password?.message}
            {...register('password')}
          />

          <InputField
            label="Confirm New Password"
            type="password"
            autoComplete="new-password"
            error={errors.confirmPassword?.message}
            {...register('confirmPassword')}
          />

          <Button type="submit" className="w-full" isLoading={isLoading} disabled={!token}>
            Reset Password
          </Button>

          <div className="text-center text-sm text-gray-600">
            Remember your password?{' '}
            <a className="font-medium text-blue-600 hover:text-blue-700" href="/login">
              Log In
            </a>
          </div>
        </form>
      </Card>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto w-full max-w-md">
          <Card>
            <div className="text-center">
              <h1 className="text-3xl font-bold tracking-tight text-gray-900">Reset Password</h1>
              <p className="mt-2 text-sm text-gray-600">
                Loading reset form. Please wait a moment.
              </p>
            </div>
          </Card>
        </div>
      }
    >
      <ResetPasswordContent />
    </Suspense>
  );
}
