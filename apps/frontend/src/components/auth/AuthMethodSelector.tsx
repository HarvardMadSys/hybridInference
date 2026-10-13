'use client';

import { useT } from '@/components/providers/useT';
import { useAuthAppearance } from '@/site-ui/appearance';

export function AuthMethodSelector({
  value,
  onChange,
}: {
  value: 'email' | 'phone';
  onChange: (method: 'email' | 'phone') => void;
}) {
  const t = useT();
  const skin = useAuthAppearance();
  return (
    <div
      className="mt-6 flex gap-5"
      role="group"
      data-auth="secondary-actions"
      aria-label={t('auth.phone.method_label', 'Account method')}
    >
      <button
        type="button"
        className={`${skin.linkButton}${value === 'email' ? ' underline underline-offset-4' : ''}`}
        aria-pressed={value === 'email'}
        onClick={() => onChange('email')}
      >
        {t('auth.phone.email_method', 'Email and password')}
      </button>
      <button
        type="button"
        className={`${skin.linkButton}${value === 'phone' ? ' underline underline-offset-4' : ''}`}
        aria-pressed={value === 'phone'}
        onClick={() => onChange('phone')}
      >
        {t('auth.phone.phone_method', 'Phone verification code')}
      </button>
    </div>
  );
}
