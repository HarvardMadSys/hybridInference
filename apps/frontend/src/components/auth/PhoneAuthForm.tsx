'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  bindPhone,
  sendPhoneCode,
  signupPhone,
  type PhoneCodePurpose,
  type SignupResponse,
} from '@/lib/api/auth';
import {
  buildCombinedUseCase,
  createAuthSchemas,
  type PhoneSignupFormData,
} from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthField, AuthNotice } from './AuthForm';
import { TurnstileChallenge } from './TurnstileChallenge';

type Props =
  | { purpose: 'signup'; onSuccess: (result: SignupResponse) => void }
  | { purpose: 'login'; onLogin: (phone: string, code: string) => Promise<void> }
  | { purpose: 'bind'; onSuccess: () => Promise<void> };

export function PhoneAuthForm(props: Props) {
  const purpose: PhoneCodePurpose = props.purpose;
  const t = useT();
  const skin = useAuthAppearance();
  const { branding } = useSiteConfig();
  const schemas = useMemo(() => createAuthSchemas(t), [t]);
  const schema = useMemo(
    () =>
      purpose === 'signup'
        ? schemas.createPhoneSignupSchema(branding.siteHost)
        : schemas.phoneCredentialsSchema,
    [schemas, purpose, branding.siteHost],
  );
  const {
    register,
    handleSubmit,
    trigger,
    getValues,
    setValue,
    formState: { errors },
  } = useForm<PhoneSignupFormData>({
    resolver: zodResolver(schema),
  });
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const submittingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deadline, setDeadline] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const token = useRef<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);

  useEffect(() => {
    if (!deadline) return;
    const tick = () => setSeconds(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  const sendCode = async () => {
    if (sendingRef.current || submittingRef.current || Date.now() < deadline) return;
    sendingRef.current = true;
    let requested = false;
    try {
      if (!(await trigger('phone'))) return;
      setError(null);
      setNotice(null);
      if (branding.turnstileSiteKey && !token.current) {
        setError(t('auth.signup.captcha_required', 'Please complete the captcha.'));
        return;
      }
      setSending(true);
      requested = true;
      const result = await sendPhoneCode({
        phone: getValues('phone').trim(),
        purpose,
        turnstileToken: token.current ?? undefined,
      });
      const retryAfter = Number.isFinite(result.retry_after) ? Math.max(1, result.retry_after) : 60;
      setSeconds(retryAfter);
      setDeadline(Date.now() + retryAfter * 1000);
      setNotice(
        result.message === 'If this phone number is eligible, a verification code has been sent.'
          ? t(
              'auth.phone.code_sent',
              'If this phone number is eligible, a verification code has been sent.',
            )
          : result.message,
      );
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      sendingRef.current = false;
      if (requested) {
        token.current = null;
        setCaptchaReset((value) => value + 1);
      }
      setSending(false);
    }
  };

  const onSubmit = async (data: PhoneSignupFormData) => {
    if (sendingRef.current || submittingRef.current) return;
    submittingRef.current = true;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      if (props.purpose === 'signup') {
        const useCase = buildCombinedUseCase(data.useCase, data.discoverySource, branding.siteHost);
        const result = await signupPhone({
          phone: data.phone,
          code: data.code,
          user_name: data.userName.trim(),
          use_case: useCase || undefined,
          accepted_tos: true,
        });
        const message =
          result.message ===
          'Account created successfully. Your registration is pending admin approval.'
            ? t(
                'auth.phone.signup_pending',
                'Account created successfully. Your registration is pending admin approval.',
              )
            : result.message === 'Account created successfully. You can now log in.'
              ? t('auth.phone.signup_success', 'Account created successfully. You can now log in.')
              : result.message;
        props.onSuccess({ ...result, message });
      } else if (props.purpose === 'login') {
        await props.onLogin(data.phone, data.code);
      } else {
        const result = await bindPhone({ phone: data.phone, code: data.code });
        await props.onSuccess();
        setNotice(
          result.message === 'Phone number bound successfully.'
            ? t('auth.phone.bind_success', 'Phone number bound successfully.')
            : result.message,
        );
        setValue('code', '');
      }
    } catch (err) {
      const suspensionMessage =
        err instanceof APIError && err.code === 'ACCOUNT_SUSPENDED'
          ? (err.details?.suspension_message as string | undefined)
          : undefined;
      setError(suspensionMessage || getErrorMessage(err));
    } finally {
      submittingRef.current = false;
      setLoading(false);
    }
  };

  const phoneField = register('phone', {
    onChange: () => {
      setValue('code', '');
      setNotice(null);
    },
  });
  return (
    <form onSubmit={handleSubmit(onSubmit)} className={skin.form} data-auth="form">
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      {notice && <AuthNotice tone="ok">{notice}</AuthNotice>}
      <AuthField
        id={`${purpose}-phone`}
        label={t('auth.phone.phone_label', 'Mobile number')}
        hint={t('auth.phone.phone_hint', 'Include the country code, such as +12025550123.')}
        error={errors.phone?.message}
      >
        <input
          id={`${purpose}-phone`}
          className={errors.phone ? skin.inputError : skin.input}
          data-auth="control"
          type="tel"
          autoComplete="tel"
          maxLength={16}
          disabled={sending || loading}
          {...phoneField}
        />
      </AuthField>
      <AuthField
        id={`${purpose}-code`}
        label={t('auth.phone.code_label', 'Verification code')}
        error={errors.code?.message}
        action={
          <button
            type="button"
            className={skin.linkButton}
            disabled={sending || loading || seconds > 0}
            aria-busy={sending}
            onClick={sendCode}
          >
            {sending
              ? t('auth.phone.sending', 'Sending…')
              : seconds > 0
                ? fill(t('auth.phone.retry_in', 'Send again in {seconds}s'), {
                    seconds: String(seconds),
                  })
                : t('auth.phone.send_code', 'Send verification code')}
          </button>
        }
      >
        <input
          id={`${purpose}-code`}
          className={errors.code ? skin.inputError : skin.input}
          data-auth="control"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          disabled={loading}
          {...register('code')}
        />
      </AuthField>
      {purpose === 'signup' && (
        <>
          <AuthField
            id="phone-userName"
            label={t('auth.signup.username_label', 'Username')}
            hint={t(
              'auth.signup.username_hint',
              'This name is shown in your account and admin review.',
            )}
            error={errors.userName?.message}
          >
            <input
              id="phone-userName"
              className={errors.userName ? skin.inputError : skin.input}
              data-auth="control"
              autoComplete="username"
              disabled={loading}
              {...register('userName')}
            />
          </AuthField>
          <AuthField
            id="phone-discoverySource"
            label={t('auth.signup.discovery_label', 'How did you find us?')}
            hint={t('auth.signup.discovery_hint', 'Optional, but helpful for improving outreach.')}
            error={errors.discoverySource?.message}
          >
            <textarea
              id="phone-discoverySource"
              rows={2}
              maxLength={500}
              className={`${errors.discoverySource ? skin.inputError : skin.input} auth-textarea`}
              data-auth="control"
              disabled={loading}
              {...register('discoverySource')}
            />
          </AuthField>
          <AuthField
            id="phone-useCase"
            label={t('auth.signup.use_case_label', 'Use case')}
            hint={t('auth.signup.use_case_hint', 'Helps admins review signups faster.')}
            error={errors.useCase?.message}
          >
            <textarea
              id="phone-useCase"
              rows={4}
              maxLength={2000}
              className={`${errors.useCase ? skin.inputError : skin.input} auth-textarea`}
              data-auth="control"
              disabled={loading}
              {...register('useCase')}
            />
          </AuthField>
        </>
      )}
      {branding.turnstileSiteKey && (
        <TurnstileChallenge
          siteKey={branding.turnstileSiteKey}
          onToken={(value) => {
            token.current = value;
          }}
          resetKey={captchaReset}
        />
      )}
      <button
        type="submit"
        className={skin.submit}
        data-auth="submit"
        disabled={loading || sending}
        aria-busy={loading}
      >
        {loading
          ? t('auth.phone.submitting', 'Verifying…')
          : purpose === 'signup'
            ? t('auth.signup.submit', 'Sign Up')
            : purpose === 'login'
              ? t('auth.login.submit', 'Log In')
              : t('auth.phone.bind_submit', 'Bind mobile number')}
      </button>
    </form>
  );
}
