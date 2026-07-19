'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import toast from 'react-hot-toast';
import { loginSchema, LoginFormData } from '@/lib/schemas/auth';
import { useAuth } from '@/components/providers';
import { resendVerification } from '@/lib/api/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { Card } from '@/components/ui/Card';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

export default function LoginPage() {
  const router = useRouter();
  const { login, state } = useAuth();
  const { features } = useSiteConfig();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Email of an unverified account that just failed to log in. When set, we
  // surface a "resend verification email" action so the user isn't dead-ended.
  const [unverifiedEmail, setUnverifiedEmail] = useState<string | null>(null);
  const [isResending, setIsResending] = useState(false);
  const [resendDone, setResendDone] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
  });

  useEffect(() => {
    if (!state.loading && state.isAuthenticated) {
      router.replace('/dashboard');
    }
  }, [state.loading, state.isAuthenticated, router]);

  const onSubmit = async (data: LoginFormData) => {
    setIsLoading(true);
    setError(null);
    setUnverifiedEmail(null);
    setResendDone(false);

    try {
      await login(data.email, data.password);
      toast.success('Login successful!');
      router.push('/dashboard');
    } catch (err) {
      const errorMsg = getErrorMessage(err);
      setError(errorMsg);
      toast.error(errorMsg);
      if (err instanceof APIError && err.code === 'EMAIL_NOT_VERIFIED') {
        setUnverifiedEmail(data.email);
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleResend = async () => {
    if (!unverifiedEmail) return;
    setIsResending(true);
    try {
      await resendVerification(unverifiedEmail);
      setResendDone(true);
      toast.success('Verification email sent. Please check your inbox.');
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setIsResending(false);
    }
  };

  if (state.loading || state.isAuthenticated) {
    return (
      <div className="flex w-full items-center justify-center">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Log In</h1>
          <p className="mt-2 text-sm text-gray-600">Welcome back, please log in with your email</p>
        </div>

        <form onSubmit={handleSubmit(onSubmit)} className="mt-8 space-y-5">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
              {error}
            </div>
          )}

          {unverifiedEmail &&
            (resendDone ? (
              <div className="rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">
                A new verification email is on its way to {unverifiedEmail}. Please check your inbox
                (and spam folder).
              </div>
            ) : (
              <div className="rounded border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
                <p>Didn&apos;t get the verification email?</p>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="mt-2"
                  isLoading={isResending}
                  onClick={handleResend}
                >
                  Resend verification email
                </Button>
              </div>
            ))}

          <InputField
            label="Email"
            type="email"
            autoComplete="email"
            error={errors.email?.message}
            {...register('email')}
          />

          <InputField
            label="Password"
            type="password"
            autoComplete="current-password"
            error={errors.password?.message}
            {...register('password')}
          />

          <div className="flex items-center justify-end">
            <a
              href="/forgot-password"
              className="text-sm font-medium text-blue-600 hover:text-blue-700"
            >
              Forgot password?
            </a>
          </div>

          <Button type="submit" className="w-full" isLoading={isLoading}>
            Log In
          </Button>

          {features.publicSignup && (
            <div className="text-center text-sm text-gray-600">
              Don&apos;t have an account?{' '}
              <a className="font-medium text-blue-600 hover:text-blue-700" href="/signup">
                Sign Up
              </a>
            </div>
          )}
        </form>
      </Card>
    </div>
  );
}
