'use client';

import { useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { bindEmail } from '@/lib/api/auth';
import { createAuthSchemas, type EmailBindFormData } from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useT } from '@/components/providers/useT';
import { Button } from '@/components/ui/Button';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';
import { AuthField, AuthNotice } from './AuthForm';

export function EmailBindForm({
  onSuccess,
  onCancel,
}: {
  onSuccess: () => Promise<void>;
  onCancel?: () => void;
}) {
  const t = useT();
  const skin = useAuthAppearance();
  const schemas = useMemo(() => createAuthSchemas(t), [t]);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<EmailBindFormData>({
    resolver: zodResolver(schemas.emailBindSchema),
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const onSubmit = async (data: EmailBindFormData) => {
    if (loading) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await bindEmail({
        email: data.email.trim(),
      });
      await onSuccess();
      setNotice(
        result.email_verified
          ? 'Email address updated successfully.'
          : fill(
              'Verification email sent to {email}. Click the link in your inbox to finish verifying.',
              { email: result.email },
            ),
      );
    } catch (err) {
      const suspensionMessage =
        err instanceof APIError && err.code === 'ACCOUNT_SUSPENDED'
          ? (err.details?.suspension_message as string | undefined)
          : undefined;
      setError(suspensionMessage || getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  const isNeutral = skin === NEUTRAL_AUTH_APPEARANCE;
  const submitButton = (
    <button
      type="submit"
      className={isNeutral ? skin.submit.replace(/\bw-full\b/, '') : skin.submit}
      data-auth="submit"
      disabled={loading}
      aria-busy={loading}
    >
      {loading ? 'Sending…' : 'Send verification email'}
    </button>
  );

  return (
    <form
      onSubmit={handleSubmit(onSubmit)}
      className={isNeutral ? 'space-y-4' : skin.form}
      data-auth="form"
    >
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      {notice && <AuthNotice tone="ok">{notice}</AuthNotice>}
      <AuthField
        id="bind-email"
        label={t('auth.signup.email_label', 'Email')}
        hint="We will send a verification link to this address."
        error={errors.email?.message}
      >
        <input
          id="bind-email"
          className={errors.email ? skin.inputError : skin.input}
          data-auth="control"
          type="email"
          autoComplete="email"
          disabled={loading}
          {...register('email')}
        />
      </AuthField>
      {isNeutral ? (
        <div className="flex justify-end gap-2">
          {onCancel && (
            <Button type="button" variant="secondary" disabled={loading} onClick={onCancel}>
              Cancel
            </Button>
          )}
          {submitButton}
        </div>
      ) : (
        submitButton
      )}
    </form>
  );
}
