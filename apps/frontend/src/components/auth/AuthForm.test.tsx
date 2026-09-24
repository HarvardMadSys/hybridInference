// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuthFieldLayout } from '@/site-ui/contract';
import { AuthField, AuthLoading, AuthNotice } from './AuthForm';

/**
 * The stable selectors a module's stylesheet is allowed to depend on.
 *
 * `authAppearance` lets a module name its own classes, which is how a design
 * attaches itself — but a class name is the *module's*, so it cannot be the
 * thing the shared markup guarantees. These attributes are: they are part of the
 * interface, a module's CSS selects on them, and they are added to rather than
 * repurposed.
 *
 * The state half is asserted too, because inferring state from a class name is
 * the mistake this exists to prevent: "this field is in error" has to be an
 * attribute the application set, not something a stylesheet guessed.
 */

/** A module's field layout, when a test installs one. */
const layout = vi.hoisted(() => ({ value: undefined as AuthFieldLayout | undefined }));

vi.mock('@/site-ui/appearance', async () => {
  // The default layout is real, not mocked: it *is* the arrangement these
  // attributes are asserted against, and a stand-in would let the two drift.
  const actual =
    await vi.importActual<typeof import('@/site-ui/appearance')>('@/site-ui/appearance');
  return {
    useAuthFieldLayout: () => layout.value ?? actual.DefaultAuthFieldLayout,
    useAuthAppearance: () => ({
      form: 'f',
      field: 'field',
      label: 'label',
      labelRow: 'row',
      input: 'input',
      inputError: 'input-invalid',
      hint: 'hint',
      error: 'err',
      linkButton: 'link',
      submit: 'submit',
      notice: 'notice',
      noticeError: 'notice-err',
      noticeOk: 'notice-ok',
      consentBlock: 'consent',
      loading: 'spin',
      loadingWrap: 'spin-wrap',
    }),
  };
});

describe('the account forms\u2019 stable selectors', () => {
  afterEach(cleanup);

  it('marks a field, its label, its hint and its error', () => {
    const { container } = render(
      <AuthField id="email" label="Email" hint="We never share it." error="That is not an address">
        <input id="email" data-auth="control" />
      </AuthField>,
    );

    expect(container.querySelector('[data-auth="field"]')).toBeInTheDocument();
    expect(container.querySelector('[data-auth="label"]')).toHaveTextContent('Email');
    expect(container.querySelector('[data-auth="hint"]')).toHaveTextContent('We never share it.');
    expect(container.querySelector('[data-auth="error"]')).toHaveTextContent(
      'That is not an address',
    );
  });

  it('carries the error state as an attribute, not as a class', () => {
    // A stylesheet must be able to say "the error paragraph" and "this field is
    // invalid" without knowing which module is installed, and without matching
    // on a class the module chose. The page passes a bare control: the field is
    // what marks it, so the page cannot forget to.
    const { container } = render(
      <AuthField id="email" label="Email" error="Nope">
        <input id="email" data-auth="control" />
      </AuthField>,
    );

    expect(container.querySelector('[data-auth="error"]')).toHaveAttribute('data-auth-error');
    expect(container.querySelector('[data-auth="control"]')).toHaveAttribute(
      'aria-invalid',
      'true',
    );
  });

  it('omits the hint and error nodes entirely when there is nothing to say', () => {
    const { container } = render(
      <AuthField id="email" label="Email">
        <input id="email" data-auth="control" />
      </AuthField>,
    );

    expect(container.querySelector('[data-auth="hint"]')).toBeNull();
    expect(container.querySelector('[data-auth="error"]')).toBeNull();
  });

  it('marks a notice and states its tone', () => {
    const { container } = render(<AuthNotice tone="error">That did not work</AuthNotice>);

    const notice = container.querySelector('[data-auth="notice"]');
    expect(notice).toHaveTextContent('That did not work');
    expect(notice).toHaveAttribute('data-auth-tone', 'error');
    expect(notice).toHaveAttribute('role', 'alert');
  });

  it('marks the resolving-session region', () => {
    const { container } = render(<AuthLoading />);

    const region = container.querySelector('[data-auth="loading"]');
    expect(region).toHaveAttribute('role', 'status');
    expect(region).toHaveAttribute('aria-live', 'polite');
  });
});

/**
 * What a screen reader hears for one field.
 *
 * The label names the control; the hint and the error describe it. Both are
 * siblings of the control rather than children of its `<label>`, so the only
 * thing joining them to it is `aria-describedby` — and the field, not the page
 * and not the module's layout, is what sets it.
 */
describe('the account fields’ ARIA relationships', () => {
  afterEach(() => {
    cleanup();
    layout.value = undefined;
  });

  it('describes the control by its hint and its error, and marks it invalid', () => {
    render(
      <AuthField id="email" label="Email" hint="We never share it." error="That is not an address">
        <input id="email" />
      </AuthField>,
    );

    const control = screen.getByLabelText('Email');
    expect(control).toHaveAccessibleDescription('We never share it. That is not an address');
    expect(control).toHaveAttribute('aria-describedby', 'email-hint email-error');
    expect(control).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveAttribute('id', 'email-error');
  });

  it('describes a valid control by its hint alone and does not mark it', () => {
    render(
      <AuthField id="email" label="Email" hint="We never share it.">
        <input id="email" />
      </AuthField>,
    );

    const control = screen.getByLabelText('Email');
    expect(control).toHaveAccessibleDescription('We never share it.');
    expect(control).not.toHaveAttribute('aria-invalid');
  });

  it('points at nothing when there is nothing to say', () => {
    render(
      <AuthField id="email" label="Email">
        <input id="email" />
      </AuthField>,
    );

    // A reference to an id that is not in the document is worse than none.
    expect(screen.getByLabelText('Email')).not.toHaveAttribute('aria-describedby');
  });

  it('keeps a description the page gave the control, and adds the field’s', () => {
    render(
      <>
        <p id="email-policy">Work addresses only.</p>
        <AuthField id="email" label="Email" error="That is not an address">
          <input id="email" aria-describedby="email-policy" />
        </AuthField>
      </>,
    );

    expect(screen.getByLabelText('Email')).toHaveAccessibleDescription(
      'Work addresses only. That is not an address',
    );
  });

  it('reaches a control the page wrapped, and leaves the wrapper alone', () => {
    const { container } = render(
      <AuthField id="note" label="Note" hint="Optional." error="Too long">
        <div data-testid="wrapper">
          <textarea id="note" />
        </div>
      </AuthField>,
    );

    const control = screen.getByLabelText('Note');
    expect(control).toHaveAccessibleDescription('Optional. Too long');
    expect(control).toHaveAttribute('aria-invalid', 'true');
    const wrapper = container.querySelector('[data-testid="wrapper"]');
    expect(wrapper).not.toHaveAttribute('aria-describedby');
    expect(wrapper).not.toHaveAttribute('aria-invalid');
  });

  it('keeps the relationship when a module arranges the field', () => {
    // Messages before the label and the control two wrappers deep: where a
    // layout puts the nodes must not decide what the control is described by.
    layout.value = ({ htmlFor, label, control, hint, error, action, className }) => (
      <section className={className} data-auth="field">
        {error}
        {hint}
        <div>
          <div>{control}</div>
        </div>
        <label data-auth="label" htmlFor={htmlFor}>
          {label}
        </label>
        {action}
      </section>
    );
    render(
      <AuthField id="password" label="Password" hint="8 or more." error="Too short">
        <input id="password" type="password" />
      </AuthField>,
    );

    const control = screen.getByLabelText('Password');
    expect(control).toHaveAccessibleDescription('8 or more. Too short');
    expect(control).toHaveAttribute('aria-invalid', 'true');
  });
});
