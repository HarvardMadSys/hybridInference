'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import clsx from 'clsx';
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
  normalizePhoneWithCountry,
  type PhoneSignupFormData,
} from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';
import { AuthField, AuthNotice } from './AuthForm';
import { TurnstileChallenge } from './TurnstileChallenge';

export interface PhoneCountryOption {
  code: string;
  iso: string;
  flag: string;
  name: string;
  placeholder: string;
}

export const PHONE_COUNTRY_CODES: readonly PhoneCountryOption[] = [
  {
    code: '+1',
    iso: 'US',
    flag: '🇺🇸',
    name: 'United States / Canada',
    placeholder: '415 555 2671',
  },
  { code: '+86', iso: 'CN', flag: '🇨🇳', name: 'China Mainland', placeholder: '138 0013 8000' },
  { code: '+44', iso: 'GB', flag: '🇬🇧', name: 'United Kingdom', placeholder: '7911 123456' },
  { code: '+852', iso: 'HK', flag: '🇭🇰', name: 'Hong Kong', placeholder: '9123 4567' },
  { code: '+853', iso: 'MO', flag: '🇲🇴', name: 'Macau', placeholder: '6612 3456' },
  { code: '+886', iso: 'TW', flag: '🇹🇼', name: 'Taiwan', placeholder: '912 345 678' },
  { code: '+81', iso: 'JP', flag: '🇯🇵', name: 'Japan', placeholder: '90 1234 5678' },
  { code: '+82', iso: 'KR', flag: '🇰🇷', name: 'South Korea', placeholder: '10 1234 5678' },
  { code: '+65', iso: 'SG', flag: '🇸🇬', name: 'Singapore', placeholder: '8123 4567' },
  { code: '+61', iso: 'AU', flag: '🇦🇺', name: 'Australia', placeholder: '412 345 678' },
  { code: '+49', iso: 'DE', flag: '🇩🇪', name: 'Germany', placeholder: '1512 3456789' },
  { code: '+33', iso: 'FR', flag: '🇫🇷', name: 'France', placeholder: '6 12 34 56 78' },
  { code: '+91', iso: 'IN', flag: '🇮🇳', name: 'India', placeholder: '98765 43210' },
] as const;

type Props =
  | { purpose: 'signup'; onSuccess: (result: SignupResponse) => void }
  | { purpose: 'login'; onLogin: (phone: string, code: string) => Promise<void> }
  | { purpose: 'bind'; onSuccess: () => Promise<void> };

export function PhoneAuthForm(props: Props) {
  const purpose: PhoneCodePurpose = props.purpose;
  const t = useT();
  const skin = useAuthAppearance();
  const { branding, distribution } = useSiteConfig();
  const [countryCode, setCountryCode] = useState<string>(() =>
    distribution.id === 'tencent' ? '+86' : '+1',
  );
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  const selectedCountry = useMemo(
    () => PHONE_COUNTRY_CODES.find((item) => item.code === countryCode) ?? PHONE_COUNTRY_CODES[0],
    [countryCode],
  );

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

  const isNeutral = skin === NEUTRAL_AUTH_APPEARANCE;
  const isNeutralBind = purpose === 'bind' && isNeutral;

  useEffect(() => {
    if (!deadline) return;
    const tick = () => setSeconds(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  useEffect(() => {
    if (!dropdownOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setDropdownOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [dropdownOpen]);

  const resolvePhone = (raw: string) =>
    isNeutralBind ? normalizePhoneWithCountry(raw, countryCode) : raw.trim();

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
        phone: resolvePhone(getValues('phone')),
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
    const canonicalPhone = resolvePhone(data.phone);
    try {
      if (props.purpose === 'signup') {
        const useCase = buildCombinedUseCase(data.useCase, data.discoverySource, branding.siteHost);
        const result = await signupPhone({
          phone: canonicalPhone,
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
        await props.onLogin(canonicalPhone, data.code);
      } else {
        const result = await bindPhone({ phone: canonicalPhone, code: data.code });
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
    onChange: (event) => {
      const val = String(event.target.value || '').trim();
      if (val.startsWith('+')) {
        const matched = [...PHONE_COUNTRY_CODES]
          .sort((a, b) => b.code.length - a.code.length)
          .find((item) => val.startsWith(item.code));
        if (matched && matched.code !== countryCode) {
          setCountryCode(matched.code);
        }
      }
      setValue('code', '');
      setNotice(null);
    },
  });

  const formClassName = isNeutralBind ? 'space-y-4' : skin.form;
  const sendCodeLabel = sending
    ? t('auth.phone.sending', 'Sending…')
    : seconds > 0
      ? fill(t('auth.phone.retry_in', 'Send again in {seconds}s'), {
          seconds: String(seconds),
        })
      : t('auth.phone.send_code', 'Send verification code');
  const submitButton = (
    <button
      type="submit"
      className={isNeutralBind ? skin.submit.replace(/\bw-full\b/, '') : skin.submit}
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
  );

  return (
    <form onSubmit={handleSubmit(onSubmit)} className={formClassName} data-auth="form">
      {error && <AuthNotice tone="error">{error}</AuthNotice>}
      {notice && <AuthNotice tone="ok">{notice}</AuthNotice>}
      <AuthField
        id={`${purpose}-phone`}
        label={t('auth.phone.phone_label', 'Mobile number')}
        hint={
          isNeutralBind
            ? t(
                'auth.phone.phone_hint',
                'Select your country code or enter a full number with +country code.',
              )
            : t('auth.phone.phone_hint', 'Include the country code, such as +12025550123.')
        }
        error={errors.phone?.message}
      >
        {isNeutralBind ? (
          <div ref={dropdownRef} className="relative mt-1.5">
            <div
              className={clsx(
                'flex w-full items-stretch rounded-lg border bg-white shadow-sm transition-all duration-200',
                errors.phone
                  ? 'border-red-300 hover:border-red-400 focus-within:border-red-500 focus-within:ring-2 focus-within:ring-red-500/20'
                  : 'border-gray-300 hover:border-gray-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-500/20',
              )}
            >
              <button
                type="button"
                aria-label="Country code"
                aria-haspopup="listbox"
                aria-expanded={dropdownOpen}
                disabled={sending || loading}
                onClick={() => setDropdownOpen((open) => !open)}
                className="inline-flex shrink-0 items-center gap-2 rounded-l-lg border-r border-gray-200 bg-gray-50/70 px-3.5 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-100/80 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                <span className="text-base leading-none" aria-hidden="true">
                  {selectedCountry.flag}
                </span>
                <span className="font-mono text-xs font-semibold text-gray-800">
                  {selectedCountry.code}
                </span>
                <svg
                  className={clsx(
                    'h-3.5 w-3.5 text-gray-400 transition-transform duration-150',
                    dropdownOpen && 'rotate-180 text-gray-600',
                  )}
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z"
                    clipRule="evenodd"
                  />
                </svg>
              </button>
              <input
                id={`${purpose}-phone`}
                className="min-w-0 flex-1 rounded-r-lg border-0 bg-transparent px-3.5 py-2.5 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 disabled:cursor-not-allowed disabled:opacity-50"
                data-auth="control"
                type="tel"
                autoComplete="tel"
                placeholder={selectedCountry.placeholder}
                maxLength={16}
                disabled={sending || loading}
                {...phoneField}
              />
            </div>

            {dropdownOpen && (
              <ul
                role="listbox"
                aria-label="Country code options"
                className="absolute left-0 top-full z-30 mt-1.5 max-h-64 w-72 overflow-y-auto rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg ring-1 ring-black/5 focus:outline-none"
              >
                {PHONE_COUNTRY_CODES.map((item) => {
                  const isSelected = item.code === countryCode;
                  return (
                    <li key={item.code} role="option" aria-selected={isSelected}>
                      <button
                        type="button"
                        onClick={() => {
                          setCountryCode(item.code);
                          setDropdownOpen(false);
                          setValue('code', '');
                          setNotice(null);
                        }}
                        className={clsx(
                          'flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors',
                          isSelected
                            ? 'bg-gray-100 font-semibold text-gray-900'
                            : 'text-gray-700 hover:bg-gray-50 hover:text-gray-900',
                        )}
                      >
                        <span className="flex min-w-0 items-center gap-2.5">
                          <span className="text-base leading-none" aria-hidden="true">
                            {item.flag}
                          </span>
                          <span className="truncate">{item.name}</span>
                        </span>
                        <span className="flex shrink-0 items-center gap-2">
                          <span className="font-mono text-xs text-gray-500">{item.code}</span>
                          {isSelected ? (
                            <svg
                              className="h-4 w-4 text-gray-900"
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
                          ) : (
                            <span className="h-4 w-4" aria-hidden="true" />
                          )}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ) : (
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
        )}
      </AuthField>
      <AuthField
        id={`${purpose}-code`}
        label={t('auth.phone.code_label', 'Verification code')}
        error={errors.code?.message}
        action={
          isNeutralBind ? undefined : (
            <button
              type="button"
              className={skin.linkButton}
              disabled={sending || loading || seconds > 0}
              aria-busy={sending}
              onClick={sendCode}
            >
              {sendCodeLabel}
            </button>
          )
        }
      >
        {isNeutralBind ? (
          <div
            className={clsx(
              'mt-1.5 flex w-full items-stretch rounded-lg border bg-white shadow-sm transition-all duration-200',
              errors.code
                ? 'border-red-300 hover:border-red-400 focus-within:border-red-500 focus-within:ring-2 focus-within:ring-red-500/20'
                : 'border-gray-300 hover:border-gray-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-500/20',
            )}
          >
            <input
              id={`${purpose}-code`}
              className="min-w-0 flex-1 rounded-l-lg border-0 bg-transparent px-3.5 py-2.5 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-0 disabled:cursor-not-allowed disabled:opacity-50"
              data-auth="control"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="6-digit code"
              maxLength={6}
              disabled={loading}
              {...register('code')}
            />
            <button
              type="button"
              className="inline-flex shrink-0 items-center justify-center rounded-r-lg border-l border-gray-200 bg-gray-50/70 px-4 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-100/80 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
              disabled={sending || loading || seconds > 0}
              aria-busy={sending}
              onClick={sendCode}
            >
              {sendCodeLabel}
            </button>
          </div>
        ) : (
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
        )}
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
      {isNeutralBind ? <div className="flex justify-end">{submitButton}</div> : submitButton}
    </form>
  );
}
