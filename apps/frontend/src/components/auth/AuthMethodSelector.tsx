'use client';

import { useT } from '@/components/providers/useT';
import { useAuthAppearance } from '@/site-ui/appearance';

export function AuthMethodSelector({
  value,
  onChange,
  purpose = 'signup',
}: {
  value: 'email' | 'phone';
  onChange: (method: 'email' | 'phone') => void;
  purpose?: 'login' | 'signup';
}) {
  const t = useT();
  const skin = useAuthAppearance();
  return (
    <div
      className="mb-6 mt-6 grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1"
      role="group"
      data-auth="secondary-actions"
      data-auth-method-selector
      aria-label={
        purpose === 'login'
          ? t('auth.login.method_label', 'Login method')
          : t('auth.phone.method_label', 'Account method')
      }
    >
      <button
        type="button"
        className={`${skin.linkButton} min-h-11 rounded-md px-3 py-2 aria-pressed:bg-white aria-pressed:shadow-sm`}
        aria-pressed={value === 'email'}
        onClick={() => onChange('email')}
      >
        {purpose === 'login'
          ? t('auth.login.password_method', 'Password login')
          : t('auth.phone.email_method', 'Email and password')}
      </button>
      <button
        type="button"
        className={`${skin.linkButton} min-h-11 rounded-md px-3 py-2 aria-pressed:bg-white aria-pressed:shadow-sm`}
        aria-pressed={value === 'phone'}
        onClick={() => onChange('phone')}
      >
        {purpose === 'login'
          ? t('auth.login.sms_method', 'SMS code login')
          : t('auth.phone.phone_method', 'Phone verification code')}
      </button>
    </div>
  );
}
