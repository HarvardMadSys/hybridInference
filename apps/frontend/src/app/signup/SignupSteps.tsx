'use client';

import { useT } from '@/components/providers/useT';

export type SignupStep = 'consent' | 'form';

export function CheckIcon({ className = 'h-3 w-3' }: { className?: string }): JSX.Element {
  return (
    <svg
      className={className}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m5 10.5 3.2 3L15 6.5" />
    </svg>
  );
}

/**
 * Where the visitor is in sign-up: the confirmations, then the account form.
 *
 * Once the confirmations are done their step is a button back to them, so a
 * visitor can re-read what they agreed to without losing either step's input.
 */
export function SignupSteps({
  current,
  onBack,
}: {
  current: SignupStep;
  /** Return to the confirmations; given only on the account form. */
  onBack?: () => void;
}): JSX.Element {
  const t = useT();
  const consentDone = current === 'form';
  const steps = [
    { id: 'consent', label: t('auth.signup.step_consent', 'Agreements') },
    { id: 'form', label: t('auth.signup.step_account', 'Account details') },
  ] as const;

  return (
    <ol
      className="mt-6 grid grid-cols-2 gap-3 text-xs font-medium"
      aria-label={t('auth.signup.steps_label', 'Sign-up progress')}
    >
      {steps.map((step) => {
        const active = step.id === current;
        const done = step.id === 'consent' && consentDone;
        // A bar per step rather than a number: the confirmations below are
        // numbered sections of their own, and two counts would read as one.
        const content = (
          <>
            <span
              aria-hidden="true"
              className={`block h-1 rounded-full ${
                done ? 'bg-emerald-600' : active ? 'bg-gray-900' : 'bg-gray-200'
              }`}
            />
            <span
              className={`mt-2 flex items-center gap-1 ${
                active ? 'text-gray-900' : done ? 'text-emerald-700' : 'text-gray-400'
              }`}
            >
              {done ? <CheckIcon /> : null}
              {step.label}
            </span>
          </>
        );
        return (
          <li key={step.id} aria-current={active ? 'step' : undefined}>
            {done && onBack ? (
              <button
                type="button"
                onClick={onBack}
                className="block w-full rounded text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40 [&:hover>span:last-child]:underline [&>span:last-child]:underline-offset-4"
                aria-label={t('auth.signup.review_agreements', 'Review agreements')}
              >
                {content}
              </button>
            ) : (
              content
            )}
          </li>
        );
      })}
    </ol>
  );
}
