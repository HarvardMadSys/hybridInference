// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { forwardRef, memo } from 'react';
import ts from 'typescript';
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
  Landing: undefined,
  AuthFrame: undefined,
  TermsFrame: undefined,
  TermsContent: undefined,
  consentItems: undefined,
  fieldLayout: undefined,
  authAppearance: undefined,
  authMessages: undefined,
};

/** Compile in a module with these exports, and load the host against it. */
async function compileIn(exports: Record<string, unknown>) {
  vi.resetModules();
  vi.doMock('@site-ui/client', () => ({ ...ABSENT, descriptor, ...exports }));
  // Imported after the reset, so the provider and the field share one copy of
  // the contexts they communicate through — and one at a time, the host first.
  // Concurrent imports of a module that reaches the mock can each be handed a
  // different copy; and when the host refuses the module, an import started
  // beside it would still be loading after this test ends, and could put a
  // copy of the host compiled against the *next* test's module in the
  // registry.
  const { SITE_UI_CLIENT } = await import('./module');
  const { SiteUiProvider } = await import('./SiteUiCore');
  const { AuthField } = await import('@/components/auth/AuthForm');
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

describe('a module\u2019s component exports', () => {
  function Frame({ children }: { children?: React.ReactNode }) {
    return <div>{children}</div>;
  }

  it('fail as the module loads when one is not a component', async () => {
    // What the type check stops at build time, stopped again at runtime for a
    // value the compiler could not see: an `any`, or a cast in the module.
    await expect(compileIn({ Landing: 42 })).rejects.toThrow(
      "The Site UI module 'stand-in' exports Landing as a number. Landing must be a React component, or null to keep the host's default.",
    );
    // An element is not a component either: it is what a component returns.
    await expect(compileIn({ AuthFrame: <Frame /> })).rejects.toThrow(
      /exports AuthFrame as an object/,
    );
    await expect(compileIn({ fieldLayout: 'beside-label' })).rejects.toThrow(
      /exports fieldLayout as a string/,
    );
  });

  it('accept a function, a memo or forwardRef component, and null', async () => {
    const Memoized = memo(Frame);
    const Forwarded = forwardRef<HTMLDivElement, { children?: React.ReactNode }>(
      ({ children }, ref) => <div ref={ref}>{children}</div>,
    );
    Forwarded.displayName = 'Forwarded';
    const { SITE_UI_CLIENT } = await compileIn({
      Landing: Frame,
      AuthFrame: Memoized,
      TermsFrame: Forwarded,
      TermsContent: Frame,
      consentItems: [{ id: 'terms', label: 'I accept the terms.' }],
      fieldLayout: null,
    });

    expect(SITE_UI_CLIENT.Landing).toBe(Frame);
    expect(SITE_UI_CLIENT.AuthFrame).toBe(Memoized);
    expect(SITE_UI_CLIENT.legalText?.TermsFrame).toBe(Forwarded);
    expect(SITE_UI_CLIENT.legalText?.TermsContent).toBe(Frame);
    expect(SITE_UI_CLIENT.fieldLayout).toBeUndefined();
  });

  it('are read from named exports only', async () => {
    // A default export is not part of the contract: a module that put its
    // landing page there has supplied no landing page.
    const { SITE_UI_CLIENT } = await compileIn({ default: { Landing: Frame } });

    expect(SITE_UI_CLIENT.Landing).toBeNull();
  });
});

describe('a module\u2019s legal text', () => {
  function Frame({ children }: { children?: React.ReactNode }) {
    return <div>{children}</div>;
  }
  const ITEMS = [{ id: 'terms', label: 'I accept the terms.' }];

  it('is refused as the module loads when TermsFrame comes without the text and confirmations', async () => {
    // A frame whose text the consent step does not show is exactly the
    // mismatch the set exists to prevent: /terms would publish one text while
    // the sign-up step asked visitors to accept another.
    await expect(compileIn({ TermsFrame: Frame })).rejects.toThrow(
      "The Site UI module 'stand-in' exports TermsFrame without TermsContent and consentItems. " +
        'A module that publishes its own legal text exports TermsFrame, TermsContent and ' +
        'consentItems together, so that /terms and the sign-up consent step show the same text.',
    );
  });

  it('is refused without the frame, or without the confirmations', async () => {
    await expect(compileIn({ TermsContent: Frame, consentItems: ITEMS })).rejects.toThrow(
      /exports TermsContent and consentItems without TermsFrame\./,
    );
    await expect(compileIn({ TermsFrame: Frame, TermsContent: Frame })).rejects.toThrow(
      /exports TermsFrame and TermsContent without consentItems\./,
    );
  });

  it.each([
    ['not a list', { terms: true }, /exports consentItems as an object/],
    ['an empty list', [], /exports consentItems as an empty list/],
    ['an item without an id', [{ label: 'I accept.' }], /with no id at position 0/],
    ['a repeated id', [ITEMS[0], ITEMS[0]], /with the id 'terms' twice/],
    ['an item without a label', [{ id: 'terms', label: ' ' }], /with no label for 'terms'/],
    [
      'a description that is not text',
      [{ id: 'terms', label: 'I accept.', description: 42 }],
      /with a description for 'terms' that is not a string/,
    ],
  ])('refuses confirmations that are %s', async (_case, consentItems, message) => {
    await expect(
      compileIn({ TermsFrame: Frame, TermsContent: Frame, consentItems }),
    ).rejects.toThrow(message);
  });

  it('keeps a frozen copy of the confirmations, with the declared fields only', async () => {
    const { SITE_UI_CLIENT } = await compileIn({
      TermsFrame: Frame,
      TermsContent: Frame,
      consentItems: [
        { id: 'terms', label: 'I accept the terms.', description: 'Read them first.', html: '<b>' },
      ],
    });

    const items = SITE_UI_CLIENT.legalText?.consentItems;
    expect(items).toEqual([
      { id: 'terms', label: 'I accept the terms.', description: 'Read them first.' },
    ]);
    expect(Object.isFrozen(items)).toBe(true);
    expect(Object.isFrozen(items?.[0])).toBe(true);
  });

  it('is null when the module exports none of the three, or null for each', async () => {
    expect((await compileIn({})).SITE_UI_CLIENT.legalText).toBeNull();
    const nulls = { TermsFrame: null, TermsContent: null, consentItems: null };
    expect((await compileIn(nulls)).SITE_UI_CLIENT.legalText).toBeNull();
  });
});

/**
 * The compile-time half, run the way the build runs it: `module.tsx` checked
 * against a module the build selected. The program is built in-process with
 * `@site-ui/client` pointed at each candidate, so nothing the rest of the suite
 * reads — the generated bridge, `tsconfig.generated.json` — is rewritten.
 */
describe('the client module type check', () => {
  const frontend = join(__dirname, '..', '..');
  const hostModule = join(__dirname, 'module.tsx');

  function typeCheckAgainst(client: string): string[] {
    const config = ts.getParsedCommandLineOfConfigFile(
      join(frontend, 'tsconfig.json'),
      {},
      { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => undefined },
    );
    if (!config) throw new Error('tsconfig.json could not be read');
    const program = ts.createProgram([hostModule], {
      ...config.options,
      noEmit: true,
      incremental: false,
      paths: { ...config.options.paths, '@site-ui/client': [client] },
    });
    return ts
      .getPreEmitDiagnostics(program)
      .map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'));
  }

  it('fails on a module whose export does not match the contract', () => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-type-check-'));
    try {
      writeFileSync(
        join(dir, 'client.tsx'),
        "export const descriptor = { siteUiApi: 1, id: 'wrong', locale: '' } as const;\n" +
          'export const Landing = 42;\n',
      );

      const errors = typeCheckAgainst(join(dir, 'client.tsx'));

      expect(errors).toHaveLength(1);
      expect(errors[0]).toMatch(/is not assignable to type 'SiteUiClientModule'/);
      expect(errors[0]).toMatch(/Types of property 'Landing' are incompatible/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 60_000);

  it('fails on a module that exports TermsFrame without its text and confirmations', () => {
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-type-check-'));
    try {
      writeFileSync(
        join(dir, 'client.tsx'),
        "export const descriptor = { siteUiApi: 1, id: 'frame-only', locale: '' } as const;\n" +
          'export const TermsFrame = (_props: { children?: unknown }) => null;\n',
      );

      const errors = typeCheckAgainst(join(dir, 'client.tsx'));

      expect(errors).toHaveLength(1);
      expect(errors[0]).toMatch(/is not assignable to type 'SiteUiClientModule'/);
      expect(errors[0]).toMatch(/TermsContent, consentItems/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 60_000);

  it('fails on a module whose legal set asks for no confirmation', () => {
    // The load-time guard refuses an empty list too, but the build renders no
    // page, so only the type can stop it before an image is published.
    const dir = mkdtempSync(join(tmpdir(), 'site-ui-type-check-'));
    try {
      writeFileSync(
        join(dir, 'client.tsx'),
        "export const descriptor = { siteUiApi: 1, id: 'no-items', locale: '' } as const;\n" +
          'export const TermsFrame = (_props: { children?: unknown }) => null;\n' +
          'export const TermsContent = (_props: { headingLevel: 2 | 3 }) => null;\n' +
          'export const consentItems = [] as const;\n',
      );

      const errors = typeCheckAgainst(join(dir, 'client.tsx'));

      expect(errors).toHaveLength(1);
      expect(errors[0]).toMatch(/Types of property 'consentItems' are incompatible/);
      expect(errors[0]).toMatch(/Source has 0 element\(s\) but target requires 1/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 60_000);

  it.each([
    ['the neutral module', join(frontend, 'src', 'site-ui', 'neutral', 'client.tsx')],
    ['the test fixture module', join(frontend, 'tests', 'fixtures', 'site-ui-demo', 'client.tsx')],
    [
      'the example module',
      join(frontend, '..', '..', 'distributions', 'example', 'frontend', 'site-ui', 'client.tsx'),
    ],
  ])(
    'passes on %s',
    (_name, client) => {
      expect(typeCheckAgainst(client)).toEqual([]);
    },
    60_000,
  );
});
