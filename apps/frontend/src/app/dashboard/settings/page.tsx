'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import toast from 'react-hot-toast';
import { updatePassword, updateProfile } from '@/lib/api/user';
import { profileUpdateSchema, ProfileUpdateData } from '@/lib/schemas/auth';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { cropImageToDataUrl, UserAvatar } from '@/components/ui/UserAvatar';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { useAuth } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { EmailBindForm } from '@/components/auth/EmailBindForm';
import { PhoneAuthForm } from '@/components/auth/PhoneAuthForm';

function createPasswordSchema(requireOldPassword: boolean) {
  return z
    .object({
      oldPassword: requireOldPassword
        ? z.string().min(1, 'Current password is required')
        : z.string().optional(),
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
}

type ChangePasswordFormData = z.infer<ReturnType<typeof createPasswordSchema>>;

function SettingsSection({
  title,
  description,
  tone = 'default',
  children,
}: {
  title: React.ReactNode;
  description: React.ReactNode;
  tone?: 'default' | 'danger';
  children: React.ReactNode;
}) {
  return (
    <section
      className={
        tone === 'danger'
          ? 'rounded-xl border border-red-200/80 bg-white p-6 shadow-sm'
          : 'rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200'
      }
    >
      <div className="grid grid-cols-1 gap-6 md:grid-cols-3 md:gap-8">
        <div className="md:col-span-1">
          <h2
            className={
              tone === 'danger'
                ? 'text-base font-semibold text-red-700'
                : 'text-base font-semibold text-gray-900'
            }
          >
            {title}
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-gray-600">{description}</p>
        </div>
        <div className="md:col-span-2">{children}</div>
      </div>
    </section>
  );
}

function SettingsContent() {
  const { state, refreshUser } = useAuth();
  const { branding, features } = useSiteConfig();
  const t = useT();
  const hasPassword = state.user?.has_password !== false;
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileSuccess, setProfileSuccess] = useState<string | null>(null);
  const [avatarDraft, setAvatarDraft] = useState<string | null>(state.user?.avatar_url ?? null);
  const [avatarSaving, setAvatarSaving] = useState(false);
  const [editingEmail, setEditingEmail] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [passwordLoading, setPasswordLoading] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSuccess, setPasswordSuccess] = useState<string | null>(null);

  const passwordSchema = useMemo(() => createPasswordSchema(hasPassword), [hasPassword]);
  const passwordForm = useForm<ChangePasswordFormData>({
    resolver: zodResolver(passwordSchema),
  });

  const profileForm = useForm<ProfileUpdateData>({
    resolver: zodResolver(profileUpdateSchema),
    defaultValues: { userName: state.user?.user_name || '' },
  });

  useEffect(() => {
    profileForm.reset({ userName: state.user?.user_name || '' });
  }, [profileForm, state.user?.user_name]);

  useEffect(() => {
    setAvatarDraft(state.user?.avatar_url ?? null);
  }, [state.user?.avatar_url]);

  const handleAvatarChange = async (nextAvatar: string | null) => {
    setAvatarDraft(nextAvatar);
    setAvatarSaving(true);
    setProfileError(null);
    setProfileSuccess(null);
    try {
      await updateProfile({ avatar_url: nextAvatar ?? '' });
      await refreshUser();
      setProfileSuccess('Portrait updated successfully.');
      toast.success('Portrait updated!');
    } catch (err) {
      const errorMsg = getErrorMessage(err);
      setProfileError(errorMsg);
      toast.error(errorMsg);
    } finally {
      setAvatarSaving(false);
    }
  };

  const handleFileSelect = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    try {
      const dataUrl = await cropImageToDataUrl(file, 128);
      await handleAvatarChange(dataUrl);
    } catch (err) {
      const errorMsg = getErrorMessage(err);
      setProfileError(errorMsg);
      toast.error(errorMsg);
    }
  };

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
      const result = await updatePassword(data.oldPassword || '', data.newPassword);
      await refreshUser();
      setPasswordSuccess(result.message);
      toast.success(hasPassword ? 'Password updated successfully!' : 'Password set successfully!');
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
    <div className="mx-auto w-full max-w-4xl space-y-6">
      <div className="flex items-start justify-between border-b border-gray-200 pb-5">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900">Account Settings</h1>
          <p className="mt-1 text-sm text-gray-600">
            Manage your profile, sign-in methods, and account security
          </p>
        </div>
        <div className="flex items-center gap-3">
          <a
            href="/dashboard"
            className="inline-flex items-center rounded-md bg-white px-3.5 py-2 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
          >
            ← Back
          </a>
          {branding.docsUrl && (
            <a
              href={branding.docsUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center rounded-md bg-white px-3.5 py-2 text-sm font-medium text-gray-700 shadow-sm ring-1 ring-inset ring-gray-300 hover:bg-gray-50"
            >
              Docs
            </a>
          )}
        </div>
      </div>

      <SettingsSection
        title="Change Username"
        description="This name is shown on your dashboard and admin request views."
      >
        <form onSubmit={profileForm.handleSubmit(onProfileSubmit)} className="space-y-5">
          {profileError && (
            <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {profileError}
            </div>
          )}
          {profileSuccess && (
            <div className="rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">
              {profileSuccess}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-4 border-b border-gray-100 pb-5">
            <div className="flex items-center gap-4">
              <UserAvatar
                user={{
                  ...state.user,
                  user_name: profileForm.watch('userName') || state.user?.user_name,
                }}
                avatarUrl={avatarDraft}
                size="lg"
              />
              <div>
                <div className="text-sm font-medium text-gray-900">Portrait</div>
                <p className="text-xs text-gray-500">
                  PNG, JPEG, or WebP up to 8 MB. Defaults to your initials.
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={handleFileSelect}
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={avatarSaving}
                onClick={() => fileInputRef.current?.click()}
              >
                Upload Photo
              </Button>
              {avatarDraft && (
                <Button
                  type="button"
                  variant="subtle"
                  size="sm"
                  disabled={avatarSaving}
                  onClick={() => handleAvatarChange(null)}
                >
                  Remove
                </Button>
              )}
            </div>
          </div>

          <InputField
            label="Username"
            type="text"
            autoComplete="username"
            hint="Use 2-50 characters."
            error={profileForm.formState.errors.userName?.message}
            {...profileForm.register('userName')}
          />

          <div className="flex justify-end">
            <Button type="submit" isLoading={profileLoading}>
              Update Username
            </Button>
          </div>
        </form>
      </SettingsSection>

      {!state.user?.login_name && (
        <SettingsSection
          title="Email Address"
          description={
            state.user?.email
              ? 'Your verified email address for signing in and receiving account notifications.'
              : 'Bind an email address to receive notifications and sign in with email.'
          }
        >
          {state.user?.email ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3 shadow-sm">
                <div className="flex min-w-0 items-center gap-3">
                  <span
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-600"
                    aria-hidden="true"
                  >
                    <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                      <path d="M3 4a2 2 0 00-2 2v1.161l8.441 4.221a1.25 1.25 0 001.118 0L19 7.162V6a2 2 0 00-2-2H3z" />
                      <path d="M19 8.839l-7.77 3.885a2.75 2.75 0 01-2.46 0L1 8.839V14a2 2 0 002 2h14a2 2 0 002-2V8.839z" />
                    </svg>
                  </span>
                  <div className="flex min-w-0 flex-wrap items-center gap-2.5">
                    <span className="truncate text-sm font-medium text-gray-900">
                      {state.user.email}
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
                      <svg
                        className="h-3 w-3 text-emerald-600"
                        viewBox="0 0 20 20"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          fillRule="evenodd"
                          d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                          clipRule="evenodd"
                        />
                      </svg>
                      Verified
                    </span>
                  </div>
                </div>
                <Button
                  type="button"
                  variant={editingEmail ? 'subtle' : 'secondary'}
                  size="sm"
                  onClick={() => setEditingEmail((prev) => !prev)}
                >
                  {editingEmail ? 'Cancel' : 'Change Email'}
                </Button>
              </div>

              {editingEmail && (
                <div className="border-t border-gray-100 pt-4">
                  <EmailBindForm onSuccess={refreshUser} isChanging />
                </div>
              )}
            </div>
          ) : (
            <EmailBindForm onSuccess={refreshUser} />
          )}
        </SettingsSection>
      )}

      {features.phoneAuth && (
        <SettingsSection
          title={t('auth.phone.settings_title', 'Mobile Number')}
          description={t(
            'auth.phone.bind_hint',
            'Bind a mobile number to log in to this account with a verification code.',
          )}
        >
          {state.user?.phone_verified ? (
            <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-gray-50/70 px-4 py-3">
              <span className="text-sm font-medium text-gray-900">
                {fill(t('auth.phone.bound_number', 'Verified mobile number: {phone}'), {
                  phone: state.user.phone ?? '',
                })}
              </span>
              <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
                Verified
              </span>
            </div>
          ) : (
            <PhoneAuthForm purpose="bind" onSuccess={refreshUser} />
          )}
        </SettingsSection>
      )}

      {(hasPassword || features.phoneAuth) && (
        <SettingsSection
          title={hasPassword ? 'Change Password' : 'Set Password'}
          description={
            hasPassword
              ? 'Update your password to keep your account secure.'
              : 'Set a password so you can also sign in with your email address.'
          }
        >
          <form onSubmit={passwordForm.handleSubmit(onPasswordSubmit)} className="space-y-4">
            {passwordError && (
              <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                {passwordError}
              </div>
            )}
            {passwordSuccess && (
              <div className="rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700">
                {passwordSuccess}
              </div>
            )}

            {hasPassword && (
              <InputField
                label="Current Password"
                type="password"
                autoComplete="current-password"
                error={passwordForm.formState.errors.oldPassword?.message}
                {...passwordForm.register('oldPassword')}
              />
            )}

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
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
            </div>

            <div className="flex justify-end">
              <Button type="submit" isLoading={passwordLoading}>
                {hasPassword ? 'Update Password' : 'Set Password'}
              </Button>
            </div>
          </form>
        </SettingsSection>
      )}

      <SettingsSection
        title="Danger Zone"
        description="Irreversible actions that permanently affect your account."
        tone="danger"
      >
        <div className="rounded-lg border border-red-100 bg-red-50/50 px-4 py-3.5 text-sm text-gray-700">
          Account deletion is not enabled yet. We will add secure flows with password confirmation
          and email verification.
        </div>
      </SettingsSection>
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
