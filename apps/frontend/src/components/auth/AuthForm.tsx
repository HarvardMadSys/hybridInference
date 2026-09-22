'use client';

import type { ReactNode } from 'react';

import { DefaultAuthFieldLayout, useAuthAppearance } from '@/site-ui/appearance';

/**
 * Shared account presentation primitives. Controllers retain requests, schemas,
 * tokens and redirects. A module can supply scoped semantic CSS, a compatible
 * class map and a field layout without changing authentication behavior.
 *
 * `data-auth` hooks are stable selectors; state comes from `aria-invalid`,
 * `disabled`, `aria-busy`, `data-auth-error` and `data-auth-tone`.
 */
export const AUTH_DATA = {
  form: 'form',
  field: 'field',
  label: 'label',
  control: 'control',
  passwordControl: 'password-control',
  reveal: 'reveal',
  hint: 'hint',
  error: 'error',
  notice: 'notice',
  submit: 'submit',
  fieldAction: 'field-action',
  secondaryActions: 'secondary-actions',
  loading: 'loading',
} as const;

export function AuthLoading() {
  const appearance = useAuthAppearance();

  return (
    <div
      className={appearance.loadingWrap}
      role="status"
      aria-live="polite"
      data-auth={AUTH_DATA.loading}
    >
      <div className={appearance.loading} />
    </div>
  );
}
/** Label, control, hint and validation message for one field. */
export function AuthField({
  id,
  label,
  hint,
  error,
  action,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  /** The field's own action — the "forgot password" link — when it has one. */
  action?: ReactNode;
  children: ReactNode;
}) {
  const appearance = useAuthAppearance();
  const Layout = appearance.fieldLayout ?? DefaultAuthFieldLayout;

  // The nodes are built here, once, and handed to the layout. A module places
  // them; it cannot change what they are, and the ARIA relationships between
  // them stay with the ids this component owns.
  return (
    <Layout
      className={appearance.field}
      htmlFor={id}
      label={label}
      labelClassName={appearance.label}
      rowClassName={appearance.labelRow}
      control={children}
      action={action ? <span data-auth={AUTH_DATA.fieldAction}>{action}</span> : null}
      hint={
        hint ? (
          <p className={appearance.hint} id={`${id}-hint`} data-auth={AUTH_DATA.hint}>
            {hint}
          </p>
        ) : null
      }
      error={
        error ? (
          <p className={appearance.error} role="alert" data-auth={AUTH_DATA.error} data-auth-error>
            {error}
          </p>
        ) : null
      }
    />
  );
}

/** A message box in the active look: info, error or confirmation. */
export function AuthNotice({
  tone = 'info',
  children,
}: {
  tone?: 'info' | 'error' | 'ok';
  children: ReactNode;
}) {
  const appearance = useAuthAppearance();
  const className =
    tone === 'error'
      ? appearance.noticeError
      : tone === 'ok'
        ? appearance.noticeOk
        : appearance.notice;

  return (
    <div
      className={className}
      role={tone === 'error' ? 'alert' : 'status'}
      data-auth={AUTH_DATA.notice}
      data-auth-tone={tone}
    >
      {children}
    </div>
  );
}
