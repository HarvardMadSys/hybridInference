'use client';

import {
  fill,
  TERMS_SECTION_ANCHOR,
  useSession,
  useSiteConfig,
  useT,
  type AuthAppearance,
  type AuthFieldLayout,
  type AuthFieldLayoutProps,
  type AuthFrameProps,
  type ConsentItems,
  type TermsContentProps,
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
 * It renders the shared heading and form nodes in one frame, so boundary tests
 * can check that the host does not duplicate them.
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

/**
 * The legal page's chrome. It brings no text: `children` is the module's own
 * `TermsContent`, which the host renders here and in the sign-up consent step.
 */
export function TermsFrame({ children }: TermsFrameProps) {
  return (
    <div data-fixture="terms-frame">
      <h1>Demonstration terms</h1>
      <nav data-fixture="terms-toc">Contents</nav>
      {children}
    </div>
  );
}

/** Two sections, numbered so the privacy one is where the console links it. */
const TERMS_SECTIONS = [
  {
    number: 1,
    title: 'Using the demonstration',
    body: 'The demonstration is provided to show the Site UI seam, and nothing else.',
  },
  {
    number: 5,
    title: 'Demonstration privacy',
    body: 'The demonstration keeps the requests it receives for as long as it runs.',
  },
];

/**
 * The module's legal text, the same in both places it appears: under the
 * frame at `/terms` and in the consent step's scrolling box, where it is
 * compact and takes the section anchors off.
 */
export function TermsContent({ headingLevel, compact }: TermsContentProps) {
  const Heading = headingLevel === 3 ? 'h3' : 'h2';
  return (
    <div data-fixture="terms-content" data-compact={compact ? 'true' : 'false'}>
      {TERMS_SECTIONS.map((section) => (
        <section
          key={section.number}
          id={compact ? undefined : `${TERMS_SECTION_ANCHOR}${section.number}`}
        >
          <Heading>{section.title}</Heading>
          <p>{section.body}</p>
        </section>
      ))}
    </div>
  );
}

/** What the demonstration asks a visitor to confirm, about the text above. */
export const consentItems: ConsentItems = [
  { id: 'demo-terms', label: 'I accept the demonstration terms.' },
  {
    id: 'demo-retention',
    label: 'I understand that the demonstration keeps my requests.',
    description: 'They are kept for as long as the demonstration runs.',
  },
];

/**
 * Wording the module owns, so the host's fallback chain can be checked in both
 * directions: `auth.login.title` is overridden, and every other key must fall
 * back to the English the shared page passes in.
 */
export const authMessages = {
  'auth.login.title': 'Sign in to the demonstration',
} as const;

/**
 * The action beside the label rather than under the messages: the structural
 * change a stylesheet cannot make, which is why it is an export of its own.
 */
function FieldWithActionBesideLabel({
  htmlFor,
  label,
  labelClassName,
  rowClassName,
  control,
  hint,
  error,
  action,
  className,
}: AuthFieldLayoutProps) {
  return (
    <div className={className} data-auth="field">
      <div className={rowClassName}>
        <label className={labelClassName} data-auth="label" htmlFor={htmlFor}>
          {label}
        </label>
        {action}
      </div>
      {control}
      {hint}
      {error}
    </div>
  );
}

export const fieldLayout: AuthFieldLayout = FieldWithActionBesideLabel;

/** Class strings, to show the appearance is applied rather than inferred. */
export const authAppearance: AuthAppearance = {
  form: 'fixture-form',
  field: 'fixture-field',
  label: 'fixture-label',
  labelRow: 'fixture-label-row',
  input: 'fixture-input',
  inputError: 'fixture-input-invalid',
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
