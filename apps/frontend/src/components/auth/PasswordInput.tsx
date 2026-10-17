'use client';

import { forwardRef, useState, type InputHTMLAttributes } from 'react';

import { useT } from '@/components/providers/useT';

function EyeIcon({ crossed }: { crossed: boolean }): JSX.Element {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
      {crossed ? <path d="M4 4l16 16" /> : null}
    </svg>
  );
}

/**
 * A password box with a button that shows what was typed.
 *
 * In a field's control slot this component is the control: `AuthField` hands
 * it the hint and error ids, and every prop but the type lands on the input.
 * The input is a block so its top margin, which the class map gives every
 * control, sits outside the wrapper the button is positioned against.
 */
export const PasswordInput = forwardRef<
  HTMLInputElement,
  Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>
>(({ className = '', id, ...props }, ref) => {
  const t = useT();
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <input
        ref={ref}
        id={id}
        type={visible ? 'text' : 'password'}
        className={`${className} block pr-11`}
        {...props}
      />
      <button
        type="button"
        className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-lg text-gray-400 transition-colors hover:text-gray-700 focus:outline-none focus-visible:text-gray-900 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500/40"
        aria-label={t('auth.signup.show_password', 'Show password')}
        aria-controls={id}
        aria-pressed={visible}
        // Keep focus in the box: a click that took it would blur the field and
        // report it unfinished while the visitor is still typing.
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => setVisible((value) => !value)}
      >
        <EyeIcon crossed={visible} />
      </button>
    </div>
  );
});
PasswordInput.displayName = 'PasswordInput';

/**
 * The password rules as a checklist that ticks as the visitor types: the four
 * `passwordSchema` enforces, which stays the authority on submit.
 *
 * It replaces the hint sentence on screen but not for assistive technology:
 * the sentence stays in the hint, visually hidden, and the checklist is hidden
 * from the accessibility tree, so the field is described exactly as before and
 * a screen reader does not re-read four items on every keystroke.
 */
export function PasswordRules({ value, hint }: { value: string; hint: string }): JSX.Element {
  const t = useT();
  const rules = [
    { label: t('auth.signup.password_rule_length', '8+ characters'), met: value.length >= 8 },
    { label: t('auth.signup.password_rule_upper', 'Uppercase'), met: /[A-Z]/.test(value) },
    { label: t('auth.signup.password_rule_lower', 'Lowercase'), met: /[a-z]/.test(value) },
    { label: t('auth.signup.password_rule_number', 'Number'), met: /[0-9]/.test(value) },
  ];
  return (
    <>
      <span className="sr-only">{hint}</span>
      <span aria-hidden="true" className="flex flex-wrap gap-x-4 gap-y-1">
        {rules.map((rule) => (
          <span
            key={rule.label}
            className={`inline-flex items-center gap-1.5 transition-colors ${
              rule.met ? 'text-emerald-700' : ''
            }`}
          >
            <span
              className={`flex h-3.5 w-3.5 items-center justify-center rounded-full ${
                rule.met ? 'bg-emerald-600 text-white' : 'border border-gray-300'
              }`}
            >
              {rule.met ? (
                <svg
                  className="h-2.5 w-2.5"
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={3}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="m5 10.5 3.2 3L15 6.5" />
                </svg>
              ) : null}
            </span>
            {rule.label}
          </span>
        ))}
      </span>
    </>
  );
}
