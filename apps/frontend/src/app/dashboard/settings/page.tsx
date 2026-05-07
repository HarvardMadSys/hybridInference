'use client';

import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import toast from 'react-hot-toast';
import { updatePassword, updateProfile } from '@/lib/api/user';
import { profileUpdateSchema, ProfileUpdateData } from '@/lib/schemas/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { Card } from '@/components/ui/Card';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';

const changePasswordSchema = z
  .object({
    oldPassword: z.string().min(1, 'Current password is required'),
    newPassword: z
      .string()
      .min(8, 'Password must be at least 8 characters')
      .regex(/[A-Z]/, 'Password must contain at least one uppercase letter')
      .regex(/[a-z]/, 'Password must contain at least one lowercase letter')
      .regex(/[0-9]/, 'Password must contain at least one number'),
    confirmPassword: z.string(),
  })
  .refine((data) => data.newPassword === data.confirmPassword, {
    message: 'Passwords do not match',
    path: ['confirmPassword'],
  });

type ChangePasswordFormData = z.infer<typeof changePasswordSchema>;

function SettingsContent() {
  const { state, refreshUser } = useAuth();
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileSuccess, setProfileSuccess] = useState<string | null>(null);

  const [passwordLoading, setPasswordLoading] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSuccess, setPasswordSuccess] = useState<string | null>(null);

  const passwordForm = useForm<ChangePasswordFormData>({
    resolver: zodResolver(changePasswordSchema),
  });

  const profileForm = useForm<ProfileUpdateData>({
    resolver: zodResolver(profileUpdateSchema),
    defaultValues: { userName: state.user?.user_name || '' },
  });

  useEffect(() => {
    profileForm.reset({ userName: state.user?.user_name || '' });
  }, [profileForm, state.user?.user_name]);

  const onProfileSubmit = async (data: ProfileUpdateData) => {
    setProfileLoading(true);
    setProfileError(null);
    setProfileSuccess(null);

    try {
      await updateProfile({ user_name: data.userName?.trim() });
      await refreshUser();
      setProfileSuccess('Username updated successfully.');
      toast.success('Username updated successfully!');
    } catch (err) {
      const errorMsg = getErrorMessage(err);
      setProfileError(errorMsg);
      toast.error(errorMsg);
    } finally {
      setProfileLoading(false);
    }
  };

  const onPasswordSubmit = async (data: ChangePasswordFormData) => {
    setPasswordLoading(true);
    setPasswordError(null);
    setPasswordSuccess(null);

    try {
      const result = await updatePassword(data.oldPassword, data.newPassword);
      setPasswordSuccess(result.message);
      toast.success('Password updated successfully!');
      passwordForm.reset();
    } catch (err) {
      const errorMsg = getErrorMessage(err);
      setPasswordError(errorMsg);
      toast.error(errorMsg);
    } finally {
      setPasswordLoading(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Account Settings</h1>
          <p className="mt-1 text-sm text-gray-600">Manage your account security and preferences</p>
        </div>
        <div className="flex items-center gap-3">
          <a href="/dashboard" className="text-sm font-medium text-blue-600 hover:text-blue-700">
            ← Back
          </a>
          <a
            href="https://doc.freeinference.org/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm font-medium text-gray-700 hover:text-gray-900"
          >
            Docs
          </a>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
        <Card>
          <div className="flex flex-col h-full">
            <div className="mb-2">
              <h2 className="text-lg font-semibold">Change Username</h2>
              <p className="text-sm text-gray-600">
                This name is shown on your dashboard and admin request views.
              </p>
            </div>
            <form onSubmit={profileForm.handleSubmit(onProfileSubmit)} className="space-y-4 flex-1">
              {profileError && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
                  {profileError}
                </div>
              )}
              {profileSuccess && (
                <div className="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded-md text-sm">
                  {profileSuccess}
                </div>
              )}

              <InputField
                label="Username"
                type="text"
                autoComplete="username"
                hint="Use 2-50 characters."
                error={profileForm.formState.errors.userName?.message}
                {...profileForm.register('userName')}
              />

              <div className="mt-auto flex justify-end">
                <Button type="submit" isLoading={profileLoading}>
                  Update Username
                </Button>
              </div>
            </form>
          </div>
        </Card>

        <Card>
          <div className="flex flex-col h-full">
            <div className="mb-2">
              <h2 className="text-lg font-semibold">Change Password</h2>
              <p className="text-sm text-gray-600">
                Update your password to keep your account secure.
              </p>
            </div>
            <form
              onSubmit={passwordForm.handleSubmit(onPasswordSubmit)}
              className="space-y-4 flex-1"
            >
              {passwordError && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
                  {passwordError}
                </div>
              )}
              {passwordSuccess && (
                <div className="bg-green-50 border border-green-200 text-green-700 px-4 py-3 rounded-md text-sm">
                  {passwordSuccess}
                </div>
              )}

              <InputField
                label="Current Password"
                type="password"
                autoComplete="current-password"
                error={passwordForm.formState.errors.oldPassword?.message}
                {...passwordForm.register('oldPassword')}
              />

              <InputField
                label="New Password"
                type="password"
                hint="At least 8 characters with uppercase, lowercase, and numbers"
                autoComplete="new-password"
                error={passwordForm.formState.errors.newPassword?.message}
                {...passwordForm.register('newPassword')}
              />

              <InputField
                label="Confirm New Password"
                type="password"
                autoComplete="new-password"
                error={passwordForm.formState.errors.confirmPassword?.message}
                {...passwordForm.register('confirmPassword')}
              />

              <div className="mt-auto flex justify-end">
                <Button type="submit" isLoading={passwordLoading}>
                  Update Password
                </Button>
              </div>
            </form>
          </div>
        </Card>

      </div>

      {/* Placeholder: Danger Zone */}
      <Card>
        <div className="flex items-start gap-3">
          <div className="mt-0.5 h-5 w-5 rounded-full bg-red-100 text-red-600 flex items-center justify-center">
            !
          </div>
          <div>
            <h2 className="text-lg font-medium mb-1 text-red-700">Danger Zone</h2>
            <p className="text-sm text-gray-600">
              Account deletion is not enabled yet. We will add secure flows with password
              confirmation and email verification.
            </p>
          </div>
        </div>
      </Card>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <ProtectedRoute>
      <SettingsContent />
    </ProtectedRoute>
  );
}
