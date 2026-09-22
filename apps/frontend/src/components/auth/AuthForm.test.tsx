// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

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

vi.mock('@/site-ui/appearance', async () => {
  // The default layout is real, not mocked: it *is* the arrangement these
  // attributes are asserted against, and a stand-in would let the two drift.
  const actual =
    await vi.importActual<typeof import('@/site-ui/appearance')>('@/site-ui/appearance');
  return {
    DefaultAuthFieldLayout: actual.DefaultAuthFieldLayout,
    useAuthAppearance: () => ({
      form: 'f',
      field: 'field',
      label: 'label',
      labelRow: 'row',
      input: 'input',
      passwordWrap: 'pw',
      reveal: 'reveal',
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
    // on a class the module chose.
    const { container } = render(
      <AuthField id="email" label="Email" error="Nope">
        <input id="email" aria-invalid data-auth="control" />
      </AuthField>,
    );

    expect(container.querySelector('[data-auth="error"]')).toHaveAttribute('data-auth-error');
    expect(container.querySelector('[data-auth="control"]')).toHaveAttribute('aria-invalid');
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
