// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

/**
 * A module's legal text, from the two places a visitor meets it.
 *
 * `/terms` publishes it and the sign-up consent step asks a visitor to accept
 * it. These render both pages against the test fixture module — compiled in the
 * way `module.test.tsx` compiles its stand-ins, by pointing `@site-ui/client`
 * at it — and check that they are the same text, and that the step asks for the
 * module's confirmations and waits for all of them.
 */

vi.mock('next/script', () => ({ default: () => null }));
vi.mock('@/lib/api/auth', () => ({ signup: vi.fn() }));

// The fixture imports the host facade, and the facade's `useT` imports
// `module.tsx` — the module being compiled, which is waiting on the fixture. A
// build resolves that cycle; a mock factory cannot wait on a module that waits
// on it. So the facade is assembled here from the same parts, with the plain
// translator: none of the fixture's legal text goes through `t()`.
vi.mock('@site-ui/host', async () => {
  const [contract, interpolate, siteConfig, appearance, i18n] = await Promise.all([
    import('./contract'),
    import('@/lib/utils/interpolate'),
    import('@/components/providers/SiteConfigProvider'),
    import('./appearance'),
    import('@/lib/i18n/translate'),
  ]);
  return {
    PUBLIC_ROUTES: contract.PUBLIC_ROUTES,
    TERMS_SECTION_ANCHOR: contract.TERMS_SECTION_ANCHOR,
    fill: interpolate.fill,
    useSiteConfig: siteConfig.useSiteConfig,
    useBranding: siteConfig.useBranding,
    useSession: () => ({ loading: false, isAuthenticated: false, user: null }),
    useAuthAppearance: appearance.useAuthAppearance,
    useT: () => i18n.translate,
  };
});

/** The fixture's section headings, in order. */
const SECTIONS = ['Using the demonstration', 'Demonstration privacy'];
/** The fixture's confirmations, by label. */
const CONFIRMATIONS = [
  'I accept the demonstration terms.',
  'I understand that the demonstration keeps my requests.',
];

async function compileInFixture() {
  vi.resetModules();
  vi.doMock('@site-ui/client', () => import('../../tests/fixtures/site-ui-demo/client'));
  // One at a time: two imports racing to a mocked module right after a reset
  // can each be handed a different copy, and then the page's `signup` is not
  // the mock this test configures.
  const auth = await import('@/lib/api/auth');
  const { default: TermsPage } = await import('@/app/terms/page');
  const { default: SignupPage } = await import('@/app/signup/page');
  return { TermsPage, SignupPage, signup: vi.mocked(auth.signup) };
}

/** The published or embedded text, whichever the page rendered. */
function fixtureText(container: ParentNode = document): HTMLElement {
  const content = container.querySelector<HTMLElement>('[data-fixture="terms-content"]');
  if (!content) throw new Error('the fixture’s TermsContent is not on the page');
  return content;
}

function scrollTermsToEnd(): void {
  const region = screen.getByRole('region', { name: 'Terms of Service' });
  Object.defineProperty(region, 'scrollHeight', { configurable: true, value: 1000 });
  Object.defineProperty(region, 'clientHeight', { configurable: true, value: 200 });
  Object.defineProperty(region, 'scrollTop', { configurable: true, value: 800 });
  fireEvent.scroll(region);
}

afterEach(() => {
  cleanup();
  vi.doUnmock('@site-ui/client');
});

describe('a module’s legal text', () => {
  it('is what /terms publishes, inside the module’s frame', async () => {
    const { TermsPage } = await compileInFixture();
    render(<TermsPage />);

    const frame = document.querySelector<HTMLElement>('[data-fixture="terms-frame"]');
    expect(frame).not.toBeNull();
    // The host rendered the text into the frame, full size and anchored.
    const content = fixtureText(frame!);
    expect(content).toHaveAttribute('data-compact', 'false');
    expect(
      within(content)
        .getAllByRole('heading', { level: 2 })
        .map((heading) => heading.textContent),
    ).toEqual(SECTIONS);
    expect(document.getElementById('terms-s5')).toHaveTextContent('Demonstration privacy');
    // And the console's text is nowhere on the page.
    expect(screen.queryByText(/logging and data use/i)).toBeNull();
  });

  it('is what the sign-up step shows, with the module’s confirmations and no others', async () => {
    const { TermsPage, SignupPage } = await compileInFixture();
    const published = render(<TermsPage />);
    const publishedText = fixtureText().textContent;
    published.unmount();

    render(<SignupPage />);

    const region = screen.getByRole('region', { name: 'Terms of Service' });
    const embedded = fixtureText(region);
    expect(embedded).toHaveAttribute('data-compact', 'true');
    expect(embedded.textContent).toBe(publishedText);
    expect(
      within(region)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(SECTIONS);

    expect(
      screen.getAllByRole('checkbox').map((checkbox) => checkbox.closest('label')?.textContent),
    ).toEqual(CONFIRMATIONS);
    expect(screen.getByRole('checkbox', { name: CONFIRMATIONS[1] })).toHaveAccessibleDescription(
      'They are kept for as long as the demonstration runs.',
    );
    // None of the console's text or confirmations.
    expect(screen.queryByText(/experimental research service/i)).toBeNull();
    expect(screen.queryByRole('checkbox', { name: /at least 18 years old/i })).toBeNull();
    expect(screen.queryByRole('heading', { name: /research participation/i })).toBeNull();
  });

  it('gates sign-up on every module confirmation, then records acceptance', async () => {
    const { SignupPage, signup } = await compileInFixture();
    signup.mockResolvedValueOnce({
      message: 'Account created successfully. You can now log in.',
      email: 'visitor@example.test',
      user_id: '01VISITOR',
      requires_approval: false,
    });
    render(<SignupPage />);
    const continueButton = screen.getByRole('button', { name: 'Continue' });
    const [first, second] = CONFIRMATIONS.map((name) => screen.getByRole('checkbox', { name }));

    // Read to the end first: until then the confirmations are locked.
    expect(first).toBeDisabled();
    expect(screen.getByText(/scroll to the end of the terms/i)).toBeInTheDocument();
    scrollTermsToEnd();
    expect(first).toBeEnabled();

    fireEvent.click(first);
    expect(continueButton).toBeDisabled();
    expect(screen.getByText('All confirmations are required to continue.')).toBeInTheDocument();

    fireEvent.click(second);
    expect(continueButton).toBeEnabled();
    fireEvent.click(second);
    expect(continueButton).toBeDisabled();
    fireEvent.click(second);

    fireEvent.click(continueButton);
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'visitor@example.test' },
    });
    fireEvent.change(screen.getByLabelText(/^Username/), { target: { value: 'Visitor' } });
    fireEvent.change(screen.getByLabelText(/^Password/), { target: { value: 'Visitor123' } });
    fireEvent.change(screen.getByLabelText('Confirm Password'), {
      target: { value: 'Visitor123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));

    // The backend records one flag for the whole step.
    expect(
      await screen.findByText('Account created successfully. You can now log in.'),
    ).toBeInTheDocument();
    expect(signup).toHaveBeenCalledWith(
      expect.objectContaining({ email: 'visitor@example.test', accepted_tos: true }),
    );
  });
});
