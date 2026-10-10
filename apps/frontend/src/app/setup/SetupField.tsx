'use client';

import type { ReactNode } from 'react';

import { NEUTRAL_AUTH_APPEARANCE } from '@/site-ui/appearance';

/** The console's own input look, as on the account pages without a module. */
export const SETUP_INPUT_CLASS = NEUTRAL_AUTH_APPEARANCE.input;
export const SETUP_INPUT_ERROR_CLASS = NEUTRAL_AUTH_APPEARANCE.inputError;

export interface FieldA11y {
  'aria-describedby'?: string;
  'aria-invalid'?: true;
}

/**
 * Label, control, hint and error for one setup field. The control is rendered
 * by the caller with the ARIA attributes this hands it, so a screen reader
 * reads the hint and the error with the input they belong to.
 */
export function SetupField({
  id,
  label,
  optional = false,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  optional?: boolean;
  hint?: ReactNode;
  error?: string;
  children: (a11y: FieldA11y) => ReactNode;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div>
      <label htmlFor={id} className="text-sm font-medium text-gray-700">
        {label}
        {optional ? <span className="ml-1 font-normal text-gray-400">(optional)</span> : null}
      </label>
      {children({
        'aria-describedby': describedBy,
        ...(error ? { 'aria-invalid': true as const } : {}),
      })}
      {hint ? (
        <p id={hintId} className="mt-1.5 text-xs text-gray-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="mt-1.5 text-xs text-red-600" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
