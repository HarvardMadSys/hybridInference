'use client';

import { useEffect, useRef, useState } from 'react';
import Script from 'next/script';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { signupSchema, SignupFormData } from '@/lib/schemas/auth';
import { signup, SignupResponse } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { Card } from '@/components/ui/Card';

const TURNSTILE_SITE_KEY = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || '';
const TURNSTILE_CALLBACK = '__signupTurnstileCallback';

declare global {
  interface Window {
    [TURNSTILE_CALLBACK]?: (token: string) => void;
  }
}

export default function SignupPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [signupResult, setSignupResult] = useState<SignupResponse | null>(null);
  const turnstileTokenRef = useRef<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SignupFormData>({
    resolver: zodResolver(signupSchema),
  });

  useEffect(() => {
    if (!TURNSTILE_SITE_KEY) return;
    window[TURNSTILE_CALLBACK] = (token: string) => {
      turnstileTokenRef.current = token;
    };
    return () => {
      delete window[TURNSTILE_CALLBACK];
    };
  }, []);

  const onSubmit = async (data: SignupFormData) => {
    setIsLoading(true);
    setError(null);

    if (TURNSTILE_SITE_KEY && !turnstileTokenRef.current) {
      setError('Please complete the captcha.');
      setIsLoading(false);
      return;
    }

    try {
      const result = await signup({
        email: data.email,
        password: data.password,
        user_name: data.userName.trim(),
        use_case: data.useCase?.trim() || undefined,
        turnstileToken: turnstileTokenRef.current ?? undefined,
      });
      setSignupResult(result);
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setIsLoading(false);
    }
  };

  if (signupResult) {
    const isPendingApproval = signupResult.requires_approval;

    return (
      <div className="mx-auto w-full max-w-md">
        <Card className={isPendingApproval ? 'border-amber-100' : 'border-green-100'}>
          <div
            className={`mb-4 flex h-12 w-12 items-center justify-center rounded-full ${
              isPendingApproval ? 'bg-amber-100' : 'bg-green-100'
            }`}
          >
            {isPendingApproval ? (
              <svg
                className="h-6 w-6 text-amber-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
            ) : (
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
            )}
          </div>
          <h1 className="text-2xl font-bold text-gray-900">
            {isPendingApproval ? 'Registration Submitted' : 'Registration Successful!'}
          </h1>
          <p className="mt-3 text-base text-gray-600">
            {isPendingApproval
              ? 'Your registration is pending admin approval. You will receive an email once your account is approved. In the meantime, please verify your email address if you received a verification link.'
              : "We've sent a verification email to your inbox. Please check and click the link to complete verification."}
          </p>
          <div className="mt-6">
            <a
              href="/login"
              className="inline-flex items-center text-sm font-medium text-blue-600 transition-colors hover:text-blue-700"
            >
              &larr; Back to Login
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
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Sign Up</h1>
          <p className="mt-2 text-sm text-gray-600">
            Create an account to manage API keys and usage
          </p>
          <div className="mt-4 rounded border border-blue-100 bg-blue-50 px-4 py-3 text-left text-sm text-blue-900">
            Open to Harvard students — sign up with your{' '}
            <span className="font-medium">@harvard.edu</span> email for instant access. Everyone
            else: please describe your use case below — we review and approve manually.
          </div>
        </div>

        <form onSubmit={handleSubmit(onSubmit)} className="mt-8 space-y-5">
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
              {error}
            </div>
          )}

          <InputField
            label="Email"
            type="email"
            autoComplete="email"
            error={errors.email?.message}
            {...register('email')}
          />

          <InputField
            label="Username"
            type="text"
            autoComplete="username"
            hint="This name is shown in your account and admin review."
            error={errors.userName?.message}
            {...register('userName')}
          />

          <InputField
            label="Password"
            type="password"
            hint="At least 8 characters with uppercase, lowercase, and numbers."
            autoComplete="new-password"
            error={errors.password?.message}
            {...register('password')}
          />

          <InputField
            label="Confirm Password"
            type="password"
            autoComplete="new-password"
            error={errors.confirmPassword?.message}
            {...register('confirmPassword')}
          />

          <label className="block">
            <span className="text-sm font-medium text-gray-700">
              Use case <span className="font-normal text-gray-500">(optional for Harvard)</span>
            </span>
            <textarea
              rows={4}
              maxLength={2000}
              placeholder="Tell us briefly what you plan to use the service for (research project, course, app prototype, etc.)."
              className={
                errors.useCase
                  ? 'mt-1.5 w-full rounded-lg border border-red-300 bg-white px-4 py-2.5 text-sm shadow-sm transition-all duration-200 placeholder:text-gray-400 hover:border-gray-400 focus:border-red-500 focus:ring-2 focus:ring-red-500/20'
                  : 'mt-1.5 w-full rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-sm shadow-sm transition-all duration-200 placeholder:text-gray-400 hover:border-gray-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20'
              }
              {...register('useCase')}
            />
            {errors.useCase ? (
              <span className="mt-1.5 block text-xs text-red-600">{errors.useCase.message}</span>
            ) : (
              <span className="mt-1.5 block text-xs text-gray-500">
                Required for non-Harvard signups so admins can review.
              </span>
            )}
          </label>

          {TURNSTILE_SITE_KEY && (
            <>
              <Script
                src="https://challenges.cloudflare.com/turnstile/v0/api.js"
                strategy="afterInteractive"
              />
              <div
                className="cf-turnstile"
                data-sitekey={TURNSTILE_SITE_KEY}
                data-callback={TURNSTILE_CALLBACK}
              />
            </>
          )}

          <Button type="submit" className="w-full" isLoading={isLoading}>
            Sign Up
          </Button>

          <div className="text-center text-sm text-gray-600">
            Already have an account?{' '}
            <a className="font-medium text-blue-600 hover:text-blue-700" href="/login">
              Log In
            </a>
          </div>
        </form>
      </Card>
    </div>
  );
}
