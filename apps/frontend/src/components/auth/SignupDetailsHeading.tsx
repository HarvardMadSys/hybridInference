'use client';

import { useT } from '@/components/providers/useT';

/**
 * Sets the sign-up form's optional questions apart from the account fields
 * above them, so a visitor can tell what is needed from what only helps.
 */
export function SignupDetailsHeading(): JSX.Element {
  const t = useT();
  return (
    <div className="border-t border-gray-200 pt-5">
      <h2 className="text-sm font-semibold text-gray-900">
        {t('auth.signup.details_title', 'Tell us about your project')}
      </h2>
      <p className="mt-0.5 text-xs text-gray-500">
        {t(
          'auth.signup.details_body',
          'Both questions are optional, but answers help admins approve accounts faster.',
        )}
      </p>
    </div>
  );
}
