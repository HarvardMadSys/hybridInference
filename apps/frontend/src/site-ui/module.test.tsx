// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AUTH_MESSAGE_KEYS, type AuthFieldLayoutProps } from './contract';

/**
 * The compiled-in module as the host reads it.
 *
 * `@site-ui/client` is the one specifier the resolver points at the selected
 * module, so replacing it here stands in for building against another module.
 * Each test installs its exports and then imports the host afresh — the
 * normalization in `module.tsx` runs once, at import, exactly as it does in a
 * build.
 */

// What the pages below need to render at all: a signed-in visitor and no
// network. None of it is under test.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/',
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: true,
      user: { id: 'user_1', email: 'visitor@example.test', role: 'user' },
    },
  }),
}));
vi.mock('@/lib/api/auth', () => ({ forgotPassword: vi.fn() }));
vi.mock('@/lib/api/chat', () => ({ streamRagChat: vi.fn() }));
vi.mock('@/lib/api/identity', () => ({ createAuthorizationCode: vi.fn() }));

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

describe('a module\u2019s wording', () => {
  /** One declared key, beside keys only console pages read. */
  const WORDING = {
    'auth.forgot.title': 'Recover your account',
    'auth.authorize.failed_title': 'Module wording for /authorize',
    'auth.authorize.incomplete': 'Module wording for the incomplete link',
    'chat.title': 'Module wording for /chat',
    'chat.empty_hint': 'Module wording for the empty chat',
  };

  it('reaches the host with only the keys the contract declares', async () => {
    const { SITE_UI_CLIENT } = await compileIn({
      authMessages: { ...WORDING, 'auth.login.title': 42 },
    });

    // The value that is not a string goes too: the contract localizes
    // sentences, not markup.
    expect({ ...SITE_UI_CLIENT.authMessages }).toEqual({
      'auth.forgot.title': 'Recover your account',
    });
  });

  it('rewords a declared page', async () => {
    await compileIn({ authMessages: WORDING });
    const { default: ForgotPasswordPage } = await import('@/app/forgot-password/page');

    render(<ForgotPasswordPage />);
    expect(screen.getByRole('heading', { name: 'Recover your account' })).toBeInTheDocument();
  });

  it('does not reword /authorize, whose keys the contract does not declare', async () => {
    await compileIn({ authMessages: WORDING });
    const { default: AuthorizePage } = await import('@/app/authorize/page');

    // No query: the page explains that the sign-in link is incomplete.
    render(<AuthorizePage />);
    expect(screen.getByRole('heading', { name: 'Sign-in failed' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'This sign-in link is incomplete. Please start again from the application.',
    );
    expect(screen.queryByText(/Module wording/)).toBeNull();
  });

  it('does not reword /chat, whose keys the contract does not declare', async () => {
    await compileIn({ authMessages: WORDING });
    const { default: ChatPage } = await import('@/app/chat/page');
    // The transcript scrolls itself on render; jsdom has no element scrolling.
    Object.defineProperty(Element.prototype, 'scrollTo', { configurable: true, value: vi.fn() });

    try {
      render(<ChatPage />);
      expect(screen.getByRole('heading', { name: 'Docs Assistant' })).toBeInTheDocument();
      expect(screen.getByText('Try one of these to get started:')).toBeInTheDocument();
      expect(screen.queryByText(/Module wording/)).toBeNull();
    } finally {
      cleanup();
      delete (Element.prototype as Partial<Element>).scrollTo;
    }
  });

  it('declares only keys that some page reads', () => {
    // A declared key nothing reads is a translation a module can supply and no
    // page will ever show. Read the call sites rather than trust the list.
    const source = join(__dirname, '..');
    const read = new Set<string>();
    const visit = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== 'generated' && entry.name !== 'active') visit(path);
        } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
          for (const match of readFileSync(path, 'utf8').matchAll(/\bt\(\s*'([^']+)'/g)) {
            read.add(match[1]);
          }
        }
      }
    };
    visit(source);

    expect(AUTH_MESSAGE_KEYS.filter((key) => !read.has(key))).toEqual([]);
  });
});
