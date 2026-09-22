'use client';

import {
  fill,
  useSession,
  useSiteConfig,
  useT,
  type AuthAppearance,
  type AuthFrameProps,
  type PublicFrameProps,
  type TermsFrameProps,
} from '@site-ui/host';

/**
 * A deliberately tiny Site UI module.
 *
 * Its job is to prove the seam, not to look like anything. It is *not* a
 * template and *not* a second design: it is the smallest module that satisfies
 * the interface, used by `src/site-ui/*.test.tsx` and by the build checks to
 * show that
 *
 * - a module the shared repository has never seen can be compiled in;
 * - the replacement touches only the routes the interface allows;
 * - the console and the shared account controllers are untouched;
 * - the neutral UI still builds with no module at all.
 *
 * If this file ever needs a change to the host to keep working, the interface
 * has grown a dependency on one distribution's design and that is the bug.
 *
 * Note the import: `@site-ui/host`, the one module path a distribution may
 * reach into. It is an alias, so the facade can move inside this repository
 * without every module having to follow it.
 */

export const descriptor = {
  siteUiApi: 1,
  id: 'site-ui-demo',
  locale: 'en-GB',
} as const;

export function Landing() {
  const t = useT();
  const { branding } = useSiteConfig();

  return (
    <div data-fixture="landing">
      <h1>
        {fill(t('landing.title', 'A demonstration site for {app_name}'), {
          app_name: branding.appName,
        })}
      </h1>
    </div>
  );
}

/**
 * A frame that shows where the shared form lands without restyling it much.
 *
 * It checks that `titleInCard: false` really does suppress the console's own
 * heading block — if the host ignores the flag, the heading appears twice and
 * the assertion in `SiteUiBoundary.test.tsx` fails.
 */
export function AuthFrame({
  page,
  kicker,
  title,
  subtitle,
  topbar,
  legal,
  children,
}: AuthFrameProps) {
  return (
    <div data-fixture="auth-frame" data-page={page}>
      <header data-fixture="auth-topbar">{topbar}</header>
      <p data-fixture="auth-kicker">{kicker}</p>
      <h1 data-fixture="auth-title">{title}</h1>
      <p data-fixture="auth-subtitle">{subtitle}</p>
      <main data-fixture="auth-body">{children}</main>
      <footer data-fixture="auth-legal">{legal}</footer>
    </div>
  );
}

export function TermsFrame({ children }: TermsFrameProps) {
  return (
    <div data-fixture="terms-frame">
      <nav data-fixture="terms-toc">Contents</nav>
      {children}
    </div>
  );
}

/**
 * The module's own layout, which the host never renders.
 *
 * Kept in the fixture because it is part of the documented interface even though
 * nothing in the host calls it, and a fixture that exercises every branch should
 * exercise the ones that only a module uses. The example module in
 * `distributions/example` deliberately does not export one, which is the other
 * half of the same property: an optional export really is optional.
 */
export function PublicFrame({ route, children }: PublicFrameProps) {
  return (
    <div data-fixture="public-frame" data-route={route}>
      {children}
    </div>
  );
}

/**
 * Wording the module owns, so the host's fallback chain can be checked in both
 * directions: `auth.login.title` is overridden, and every other key must fall
 * back to the English the shared page passes in.
 */
export const authMessages = {
  'auth.login.title': 'Sign in to the demonstration',
} as const;

/** Class strings, to show the appearance is applied rather than inferred. */
export const authAppearance: AuthAppearance = {
  form: 'fixture-form',
  field: 'fixture-field',
  label: 'fixture-label',
  labelRow: 'fixture-label-row',
  input: 'fixture-input',
  inputError: 'fixture-input-invalid',
  passwordWrap: 'fixture-password',
  reveal: 'fixture-reveal',
  hint: 'fixture-hint',
  error: 'fixture-error',
  linkButton: 'fixture-link',
  submit: 'fixture-submit',
  notice: 'fixture-notice',
  noticeError: 'fixture-notice-error',
  noticeOk: 'fixture-notice-ok',
  consentBlock: 'fixture-consent',
  loading: 'fixture-loading',
  loadingWrap: 'fixture-loading-wrap',
};

/** Only used by the fixture's own smoke test. */
export function useFixtureIdentity() {
  return useSession().user?.email ?? '';
}
