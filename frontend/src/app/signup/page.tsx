'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { signupSchema, SignupFormData } from '@/lib/schemas/auth';
import { signup } from '@/lib/api/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { Card } from '@/components/ui/Card';

export default function SignupPage() {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<SignupFormData>({
    resolver: zodResolver(signupSchema),
  });

  const onSubmit = async (data: SignupFormData) => {
    setIsLoading(true);
    setError(null);

    try {
      await signup({
        email: data.email,
        password: data.password,
        user_name: data.userName,
      });
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
          <h1 className="text-2xl font-bold text-gray-900">Registration Successful!</h1>
          <p className="mt-3 text-base text-gray-600">
            We&apos;ve sent a verification email to your inbox. Please check and click the link to
            complete verification.
          </p>
          <div className="mt-6">
            <a
              href="/login"
              className="inline-flex items-center text-sm font-medium text-blue-600 transition-colors hover:text-blue-700"
            >
              ← Back to Login
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
