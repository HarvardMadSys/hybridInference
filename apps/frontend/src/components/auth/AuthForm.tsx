'use client';

import type { ReactNode } from 'react';

import { DefaultAuthFieldLayout, useAuthAppearance } from '@/site-ui/appearance';

/**
 * Presentation primitives shared by every account page.
 *
 * These were extracted from the pages so that a distribution can restyle the
 * account forms without forking the controllers that decide who gets in. They
 * carry no authentication logic: no request, no token, no schema, no redirect.
 *
 * ## What changed, and why it matters
 *
 * This module used to hold two hardcoded class maps — `CLASSIC` and `INFERENCE`
 * — and pick between them with
 *
 * ```ts
 * return branding.presentation.preset === 'inference' ? INFERENCE : CLASSIC;
 * ```
 *
 * That single line is the reason the shared repository owned a distribution's
 * design: the SSV look could not ship without a branch here, and a second
 * distribution would have needed a second branch. The classes now arrive
 * through `useAuthAppearance()`, supplied by whichever UI module the build
 * compiled in (see `src/site-ui/`), and no identifier is compared to anything.
 *
 * Every field is a class string applied to an element whose structure and ARIA
 * contract stay here. Two used to change structure — a boolean for where the
 * heading lived and another for where a field's action sat — and both are gone:
 * the frame draws the heading and the cross-link because the page hands it those
 * nodes, and a field's action goes where `AuthField` puts it.
 *
 * State is carried by `data-auth` names and the attributes the application
 * already sets, never inferred from a class name. See
 * `docs/developer/site-ui.md`.
 */

/**
 * The account pages' "form is not ready yet" state.
 *
 * Rendered instead of a form a visitor could not submit: while the session
 * resolves, or while an already signed-in visitor is being redirected away.
 * The look belongs to the active module, which is why the module supplies a
 * `loading` class rather than this file importing a stylesheet.
 */
/**
 * `data-auth` names, and why they exist.
 *
 * A module restyles these forms from its own stylesheet, and a stylesheet needs
 * a stable selector. Class names are not one: they are the module's own
 * `authAppearance` strings, so a design that renames its classes breaks its own
 * rules, and the shared markup cannot be styled by anything that does not know
 * which module is installed. These attributes are the stable half — part of the
 * interface, added to and never repurposed.
 *
 * State is carried by attributes the application already sets, so a rule never
 * has to infer it from a class name: `aria-invalid` on a control, `disabled` on
 * a button, `aria-busy` while submitting, `data-auth-error` on an error
 * paragraph, `data-auth-tone` on a notice.
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
