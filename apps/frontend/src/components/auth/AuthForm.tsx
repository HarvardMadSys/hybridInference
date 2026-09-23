'use client';

import { Children, cloneElement, Fragment, isValidElement, type ReactNode } from 'react';

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

/** Elements that are a field's control, as opposed to markup around one. */
const CONTROL_TAGS = new Set(['input', 'select', 'textarea']);

type ControlProps = {
  'aria-describedby'?: string;
  'aria-invalid'?: boolean | 'true' | 'false' | 'grammar' | 'spelling';
  children?: ReactNode;
};

/** A control's own `aria-describedby` ids, then the field's, each once. */
function describedBy(own: string | undefined, ids: string[]): string | undefined {
  const merged = new Set([...(own ?? '').split(/\s+/).filter(Boolean), ...ids]);
  return merged.size > 0 ? [...merged].join(' ') : undefined;
}

/**
 * Point every control among `children` at the field's hint and error, and mark
 * it invalid while the error shows.
 *
 * The page's control and the field's messages are siblings rather than one
 * `<label>`, so nothing but these attributes tells a screen reader that the
 * paragraph under a password box is about it. They are set here, on the element
 * the page passed in and before any layout sees it, so a module's `fieldLayout`
 * receives a control that already names its description and cannot drop the
 * relationship by arranging the nodes differently.
 *
 * Markup around a control — a fragment, a wrapper `<div>` — is searched rather
 * than annotated. A component is annotated: in a field's control slot, it is
 * the control. An `aria-invalid` the page set itself is kept when there is no
 * error to report.
 */
function describeControls(children: ReactNode, ids: string[], invalid: boolean): ReactNode {
  return Children.map(children, (child) => {
    if (!isValidElement<ControlProps>(child)) return child;
    const isWrapper =
      child.type === Fragment || (typeof child.type === 'string' && !CONTROL_TAGS.has(child.type));
    if (isWrapper) {
      if (child.props.children === undefined) return child;
      return cloneElement(child, undefined, describeControls(child.props.children, ids, invalid));
    }
    return cloneElement(child, {
      'aria-describedby': describedBy(child.props['aria-describedby'], ids),
      ...(invalid ? { 'aria-invalid': true } : null),
    });
  });
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
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;

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
      control={describeControls(
        children,
        [hintId, errorId].filter((value): value is string => value !== undefined),
        Boolean(error),
      )}
      action={action ? <span data-auth={AUTH_DATA.fieldAction}>{action}</span> : null}
      hint={
        hint ? (
          <p className={appearance.hint} id={hintId} data-auth={AUTH_DATA.hint}>
            {hint}
          </p>
        ) : null
      }
      error={
        error ? (
          <p
            className={appearance.error}
            id={errorId}
            role="alert"
            data-auth={AUTH_DATA.error}
            data-auth-error
          >
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
