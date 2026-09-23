'use client';

import { createContext, useContext } from 'react';

import type { AuthAppearance, AuthFieldLayoutProps } from './contract';

/**
 * The field styling the shared account forms apply.
 *
 * It is context rather than a hook that reads the active module directly for
 * one reason: `AuthField` and `AuthNotice` are rendered *inside* the shared
 * controllers, and those controllers call `SiteUiBoundary`'s resolver only
 * once. Passing the appearance down through context keeps the join in one
 * place, and lets a test render the shared form with the neutral look without
 * standing up a module.
 */
const AuthAppearanceContext = createContext<AuthAppearance | null>(null);

export const AuthAppearanceProvider = AuthAppearanceContext.Provider;

/**
 * The neutral appearance used when no module supplies one.
 *
 * This is the console's existing look, unchanged. Defining it here rather than
 * in the neutral module means `useAuthAppearance()` is total: a form rendered
 * outside a boundary — a unit test, an error page — still gets working styles
 * instead of `undefined.field`.
 */
export const NEUTRAL_AUTH_APPEARANCE: AuthAppearance = {
  form: 'mt-8 space-y-5',
  field: 'block',
  label: 'text-sm font-medium text-gray-700',
  labelRow: 'mt-5 flex items-center justify-end',
  input:
    'mt-1.5 w-full rounded-lg border bg-white px-4 py-2.5 text-sm shadow-sm transition-all duration-200 placeholder:text-gray-400 hover:border-gray-400 border-gray-300 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20',
  // Match the shared input's invalid border and focus styles.
  inputError:
    'mt-1.5 w-full rounded-lg border bg-white px-4 py-2.5 text-sm shadow-sm transition-all duration-200 placeholder:text-gray-400 hover:border-red-400 border-red-300 focus:border-red-500 focus:ring-2 focus:ring-red-500/20',
  hint: 'mt-1.5 block text-xs text-gray-500',
  error: 'mt-1.5 block text-xs text-red-600',
  linkButton: 'text-sm font-medium text-blue-600 hover:text-blue-700',
  // Match the shared Button's default appearance at full form width.
  submit:
    'inline-flex h-10 w-full items-center justify-center rounded-md bg-black px-4 text-sm font-medium text-white shadow-sm transition-colors duration-200 hover:bg-gray-800 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-black focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
  notice: 'rounded border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800',
  noticeError: 'rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700',
  noticeOk: 'rounded border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700',
  consentBlock: 'border-gray-200 bg-gray-50',
  loading: 'h-12 w-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600',
  loadingWrap: 'flex w-full items-center justify-center',
};

/** The default form keeps its field action after the input and validation. */
export function DefaultAuthFieldLayout({
  htmlFor,
  label,
  labelClassName,
  rowClassName,
  control,
  hint,
  error,
  action,
  className,
}: AuthFieldLayoutProps) {
  const labelNode = (
    <label className={labelClassName} data-auth="label" htmlFor={htmlFor}>
      {label}
    </label>
  );

  return (
    <div className={className} data-auth="field">
      {labelNode}
      {control}
      {hint}
      {error}
      {action ? <div className={rowClassName}>{action}</div> : null}
    </div>
  );
}

export function useAuthAppearance(): AuthAppearance {
  return useContext(AuthAppearanceContext) ?? NEUTRAL_AUTH_APPEARANCE;
}
