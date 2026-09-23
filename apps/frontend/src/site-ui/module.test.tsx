// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AuthFieldLayoutProps } from './contract';

/**
 * The compiled-in module as the host reads it.
 *
 * `@site-ui/client` is the one specifier the resolver points at the selected
 * module, so replacing it here stands in for building against another module.
 * Each test installs its exports and then imports the host afresh — the
 * normalization in `module.tsx` runs once, at import, exactly as it does in a
 * build.
 */

const descriptor = { siteUiApi: 1, id: 'stand-in', locale: '' };

/**
 * Every name the host may read, present and `undefined` unless a test sets it.
 * A module namespace answers `undefined` for a name the module does not export;
 * a Vitest mock throws instead, so the stand-in spells the absences out.
 */
const ABSENT = {
  default: undefined,
  Landing: undefined,
  AuthFrame: undefined,
  TermsFrame: undefined,
  fieldLayout: undefined,
  authAppearance: undefined,
  authMessages: undefined,
};

/** Compile in a module with these exports, and load the host against it. */
async function compileIn(exports: Record<string, unknown>) {
  vi.resetModules();
  vi.doMock('@site-ui/client', () => ({ ...ABSENT, descriptor, ...exports }));
  // Imported together, after the reset, so the provider and the field share one
  // copy of the contexts they communicate through.
  const [{ SITE_UI_CLIENT }, { SiteUiProvider }, { AuthField }] = await Promise.all([
    import('./module'),
    import('./SiteUiCore'),
    import('@/components/auth/AuthForm'),
  ]);
  return { SITE_UI_CLIENT, SiteUiProvider, AuthField };
}

/** A module's arrangement, recognisable by its marker. */
function ModuleFieldLayout({
  htmlFor,
  label,
  control,
  hint,
  error,
  className,
}: AuthFieldLayoutProps) {
  return (
    <div className={className} data-auth="field" data-testid="module-layout">
      <label data-auth="label" htmlFor={htmlFor}>
        {label}
      </label>
      {control}
      {hint}
      {error}
    </div>
  );
}

afterEach(() => {
  cleanup();
  vi.doUnmock('@site-ui/client');
});

describe('a module’s field layout', () => {
  it('is its own export, and arranges every account field', async () => {
    const { SITE_UI_CLIENT, SiteUiProvider, AuthField } = await compileIn({
      fieldLayout: ModuleFieldLayout,
    });

    expect(SITE_UI_CLIENT.fieldLayout).toBe(ModuleFieldLayout);
    render(
      <SiteUiProvider>
        <AuthField id="email" label="Email">
          <input id="email" />
        </AuthField>
      </SiteUiProvider>,
    );
    expect(screen.getByTestId('module-layout')).toContainElement(screen.getByLabelText('Email'));
  });

  it('is not read from the deprecated class map', async () => {
    // Structure is not a class name: a layout nested in `authAppearance` is not
    // the capability, and the class map stays a map of classes.
    const { SITE_UI_CLIENT, SiteUiProvider, AuthField } = await compileIn({
      authAppearance: { fieldLayout: ModuleFieldLayout },
    });

    expect(SITE_UI_CLIENT.fieldLayout).toBeUndefined();
    render(
      <SiteUiProvider>
        <AuthField id="email" label="Email">
          <input id="email" />
        </AuthField>
      </SiteUiProvider>,
    );
    expect(screen.queryByTestId('module-layout')).toBeNull();
  });

  it('leaves the default arrangement in place when the module has none', async () => {
    const { SiteUiProvider, AuthField } = await compileIn({});

    render(
      <SiteUiProvider>
        <AuthField id="password" label="Password" action={<a href="/forgot-password">Forgot?</a>}>
          <input id="password" />
        </AuthField>
      </SiteUiProvider>,
    );
    // The default puts the action after the control.
    const control = screen.getByLabelText('Password');
    const action = screen.getByRole('link', { name: 'Forgot?' });
    expect(control.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByTestId('module-layout')).toBeNull();
  });
});
