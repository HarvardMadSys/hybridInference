'use client';

import { useT } from '@/components/providers/useT';
import { NEUTRAL_AUTH_APPEARANCE, useAuthAppearance } from '@/site-ui/appearance';

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
  const methodLabel =
    purpose === 'login'
      ? t('auth.login.method_label', 'Login method')
      : t('auth.phone.method_label', 'Account method');
  const options = [
    {
      id: 'email',
      label:
        purpose === 'login'
          ? t('auth.login.password_method', 'Password login')
          : t('auth.phone.email_method', 'Email and password'),
    },
    {
      id: 'phone',
      label:
        purpose === 'login'
          ? t('auth.login.sms_method', 'SMS code login')
          : t('auth.phone.phone_method', 'Phone verification code'),
    },
  ] as const;

  // The console's look is a segmented switch. A module's class map has no
  // entry for one, so under a module the options stay its link buttons.
  if (skin !== NEUTRAL_AUTH_APPEARANCE) {
    return (
      <div
        className={`${purpose === 'login' ? 'mb-6 ' : ''}mt-6 flex gap-5`}
        role="group"
        data-auth="secondary-actions"
        data-auth-method-selector
        aria-label={methodLabel}
      >
        {options.map((option) => (
          <button
            key={option.id}
            type="button"
            className={`${skin.linkButton}${value === option.id ? ' underline underline-offset-4' : ''}`}
            aria-pressed={value === option.id}
            onClick={() => onChange(option.id)}
          >
            {option.label}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div
      className={`${purpose === 'login' ? 'mb-6 ' : ''}mt-6 grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1`}
      role="group"
      data-auth="secondary-actions"
      data-auth-method-selector
      aria-label={methodLabel}
    >
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={`min-h-11 rounded-md px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 ${
            value === option.id
              ? 'bg-white text-gray-900 shadow-sm ring-1 ring-gray-200'
              : 'text-gray-600 hover:text-gray-900'
          }`}
          aria-pressed={value === option.id}
          onClick={() => onChange(option.id)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
