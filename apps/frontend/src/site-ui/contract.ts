/**
 * Site UI API v1 — the contract between this application and a
 * distribution-owned public-site implementation.
 *
 * ## What this interface is for
 *
 * The console, the authentication controllers and the gateway are shared. The
 * *look* of the public pages is not: an operator that wants its own home page,
 * its own sign-in appearance and its own legal text ships a small UI module
 * instead of forking this application.
 *
 * A module reaches the build as a pinned directory outside this repository (see
 * ./resolve.js) and is compiled into this app. There is one Next.js
 * application, one console and one session at runtime; only the components
 * listed here are replaced.
 *
 * ## What a module may and may not do
 *
 * MAY: render the landing page, its navigation and footer, the
 * frame and field styling of the account pages, the legal text, and the copy
 * those areas display.
 *
 * MAY NOT change: the API base, the auth provider, the session store, the
 * signup policy, the required form fields, the console routes, the proxy rules
 * or anything the database sees. It receives no credential, holds no token and
 * issues no authentication request of its own — the shared pages keep their
 * controllers and hand the module only presentation inputs.
 *
 * A module that needs a new capability needs a new revision of this interface
 * and a review of the shared repository, so that the interface stays small
 * enough to keep working.
 */

/**
 * Every public route a module can take over.
 *
 * The list is closed and owned by this repository: the host decides which
 * paths a module renders, so a module cannot widen its own reach by declaring
 * more routes. `/dashboard`, `/chat`, `/team`, `/authorize`, `/agents` and the
 * rest of the console are deliberately absent and always render the shared
 * console.
 */
export const PUBLIC_ROUTES = [
  'landing',
  'login',
  'signup',
  'forgot-password',
  'reset-password',
  'verify-email',
  'terms',
] as const;

export type PublicRoute = (typeof PUBLIC_ROUTES)[number];

/** Descriptor for the whole interface, asserted at build time. */
export interface SiteUiModuleDescriptor {
  /**
   * Interface revision this module implements. A module written for a
   * different revision fails the build rather than rendering a half-supported
   * page; there is no silent fallback.
   */
  readonly siteUiApi: number;
  /** Stable identifier, for build logs and provenance. Never branched on. */
  readonly id: string;
  /**
   * BCP-47 tag this module's copy is written in, or '' when it ships no fixed
   * language. This describes the module's copy; server `locale` controls the
   * root document's language.
   */
  readonly locale: string;
}

/**
 * Root-route content. A module supplies its own hero, navigation and footer;
 * the shared chrome is not rendered around it.
 *
 * There are no props, and that is the design: the landing page reads the
 * deployment's public configuration through the host facade, so a page and its
 * data stay one unit instead of a page plus a prop contract that has to grow
 * every time the design wants one more value.
 */
export interface LandingPageProps {
  /** Reserved. A module must accept being rendered with no props. */
  readonly __none?: never;
}

/**
 * Account-page chrome. The shared page keeps the form, its validation and its
 * API calls and passes them in as `children`.
 *
 * `shared` carries the pieces the console already renders — the cross-link to
 * sign-up or sign-in, and the terms/privacy line. A module may place them
 * where its design wants them, and must render them somewhere: dropping
 * `topbar` removes the only link between the sign-in and sign-up pages.
 */
export interface AuthFrameProps {
  /** Which account page is being framed. */
  page: Exclude<PublicRoute, 'landing' | 'terms'>;
  /** Already-localized heading inputs, resolved by the shared page. */
  kicker: string;
  title: React.ReactNode;
  subtitle: string;
  topbar?: React.ReactNode;
  legal?: React.ReactNode;
  /** The shared form. Always render it. */
  children: React.ReactNode;
}

/**
 * Section anchor prefix the legal text uses.
 *
 * `#terms-s5` is the privacy target the console's footer, the account pages and
 * the sign-up consent step all link to, so the anchor is part of the interface
 * rather than a detail of one frame: a module that invented its own prefix
 * would leave every one of those links pointing at nothing.
 */
export const TERMS_SECTION_ANCHOR = 'terms-s';

/**
 * Legal-page chrome, for a module that ships its own legal text.
 *
 * Optional, and its absence is a statement rather than an omission: a module
 * that does not export `TermsFrame` is saying "the console's terms page is the
 * one this deployment publishes", and the console then keeps its own container.
 *
 * The two cannot be mixed. A frame is chrome *around* a body, so a module that
 * draws a full-width legal header while the console supplies a card inside it
 * renders one layout inside another. The distribution's text and the
 * distribution's frame travel together, which is also why they are one module.
 */
export interface TermsFrameProps {
  children: React.ReactNode;
}

/**
 * How one field arranges the nodes the shared page produces — the module's
 * `fieldLayout` export.
 *
 * A module may place the action beside the label, above the control or in a
 * separate column. It receives rendered nodes, not field values, validation or
 * submit handlers. Render every supplied node once, associate the label with
 * `htmlFor`, and preserve the field and label's `data-auth` hooks.
 *
 * The control arrives already described: its `aria-describedby` names the hint
 * and error nodes by id, and it carries `aria-invalid` while the error shows.
 * The host sets both before the layout runs, so the relationship survives any
 * arrangement — provided each supplied node is rendered.
 *
 * The default layout renders the label, control, hint, error and then action.
 */
export interface AuthFieldLayoutProps {
  /**
   * The label's classes, from `AuthAppearance.label`.
   *
   * Passed rather than read from context so a layout remains a pure function of
   * its props — which is what lets it be tested and replaced without a provider.
   */
  labelClassName?: string;
  /** The row's classes, from `AuthAppearance.labelRow`, when there is an action. */
  rowClassName?: string;
  /** The field's id, for `<label for>` and the hint/error ids. */
  readonly htmlFor: string;
  readonly label: React.ReactNode;
  readonly control: React.ReactNode;
  readonly hint: React.ReactNode;
  readonly error: React.ReactNode;
  /** The field's action, or `null` when it has none. */
  readonly action: React.ReactNode;
  /** The class the module gave `field`, applied to the wrapper. */
  readonly className: string;
}

export type AuthFieldLayout = React.ComponentType<AuthFieldLayoutProps>;

/**
 * @deprecated Supported compatibility surface throughout Site UI API v1.
 * Prefer scoped `data-auth` and state selectors for new styling. Removing this
 * class map requires the next API revision and a documented migration.
 *
 * Class names only. Structure is not styling, so where a field's nodes go is
 * the separate, supported `fieldLayout` export rather than an entry here.
 */
export interface AuthAppearance {
  form: string;
  field: string;
  label: string;
  labelRow: string;
  input: string;
  /** Classes applied to an invalid control instead of `input`. */
  inputError: string;
  hint: string;
  error: string;
  linkButton: string;
  submit: string;
  notice: string;
  noticeError: string;
  noticeOk: string;
  consentBlock: string;
  /**
   * The "session is still resolving" spinner, and the box it sits in.
   *
   * Two entries because a module whose frame is full-height needs the spinner
   * vertically centred while the console's card does not. The element itself —
   * a `role="status"` region with `aria-live` — stays in the shared component.
   */
  loading: string;
  loadingWrap: string;
}

/**
 * Message keys a module may translate.
 *
 * The keys are declared here rather than open-ended so that a module can only
 * change *wording*: the validation rules, the field set and the error
 * semantics stay in the shared schemas. A key absent from a module's
 * dictionary falls back to the shared English default, which is what keeps a
 * partially translated deployment usable.
 *
 * The list is enforced, not advisory: `module.tsx` drops every other key from
 * a module's dictionary before any page reads it. Console pages translate
 * through the same `t()` — `/authorize` with `auth.authorize.*`, `/chat` with
 * `chat.*` — and stay closed because their keys are not here. Each key listed
 * is one a page actually reads; one that nothing reads would be a promise the
 * host does not keep.
 */
export const AUTH_MESSAGE_KEYS = [
  'auth.consent.age_body',
  'auth.consent.age_confirm',
  'auth.consent.age_title',
  'auth.consent.all_required',
  'auth.consent.continue',
  'auth.consent.intro',
  'auth.consent.kicker',
  'auth.consent.research_body',
  'auth.consent.research_consent',
  'auth.consent.research_item_1',
  'auth.consent.research_item_2',
  'auth.consent.research_item_3',
  'auth.consent.research_item_4',
  'auth.consent.research_item_5',
  'auth.consent.research_item_6',
  'auth.consent.research_title',
  'auth.consent.research_use',
  'auth.consent.research_voluntary',
  'auth.consent.research_warning',
  'auth.consent.sharing_body_1',
  'auth.consent.sharing_body_2',
  'auth.consent.sharing_body_3',
  'auth.consent.sharing_consent',
  'auth.consent.sharing_title',
  'auth.consent.terms_agree',
  'auth.consent.terms_body',
  'auth.consent.terms_link',
  'auth.consent.terms_scroll_hint',
  'auth.consent.terms_title',
  'auth.consent.title',
  'auth.forgot.back_to_login',
  'auth.forgot.email_label',
  'auth.forgot.kicker',
  'auth.forgot.login_link',
  'auth.forgot.remember',
  'auth.forgot.submit',
  'auth.forgot.submitting',
  'auth.forgot.subtitle',
  'auth.forgot.success_body',
  'auth.forgot.success_kicker',
  'auth.forgot.success_title',
  'auth.forgot.title',
  'auth.legal.see',
  'auth.login.email_label',
  'auth.login.forgot_password',
  'auth.login.kicker',
  'auth.login.no_account',
  'auth.login.password_label',
  'auth.login.resend_button',
  'auth.login.resend_prompt',
  'auth.login.resent_notice',
  'auth.login.resent_toast',
  'auth.login.signup_link',
  'auth.login.submit',
  'auth.login.submitting',
  'auth.login.subtitle',
  'auth.login.success_toast',
  'auth.login.title',
  'auth.reset.confirm_password_label',
  'auth.reset.go_to_login',
  'auth.reset.invalid_token',
  'auth.reset.kicker',
  'auth.reset.login_link',
  'auth.reset.new_password_hint',
  'auth.reset.new_password_label',
  'auth.reset.remember',
  'auth.reset.submit',
  'auth.reset.submitting',
  'auth.reset.subtitle',
  'auth.reset.success_body',
  'auth.reset.success_kicker',
  'auth.reset.success_title',
  'auth.reset.title',
  'auth.signup.back_to_login',
  'auth.signup.captcha_required',
  'auth.signup.confirm_password_label',
  'auth.signup.discovery_hint',
  'auth.signup.discovery_label',
  'auth.signup.discovery_placeholder',
  'auth.signup.email_label',
  'auth.signup.fast_track',
  'auth.signup.have_account',
  'auth.signup.kicker',
  'auth.signup.kicker_result',
  'auth.signup.kicker_unavailable',
  'auth.signup.login_link',
  'auth.signup.password_hint',
  'auth.signup.password_label',
  'auth.signup.pending_title',
  'auth.signup.result_subtitle',
  'auth.signup.signin_link',
  'auth.signup.submit',
  'auth.signup.submitting',
  'auth.signup.subtitle',
  'auth.signup.success_title',
  'auth.signup.title',
  'auth.signup.unavailable_body',
  'auth.signup.unavailable_title',
  'auth.signup.use_case_hint',
  'auth.signup.use_case_label',
  'auth.signup.use_case_placeholder',
  'auth.signup.username_hint',
  'auth.signup.username_label',
  'auth.validation.combined_max',
  'auth.validation.discovery_max',
  'auth.validation.email_invalid',
  'auth.validation.email_too_long',
  'auth.validation.password_lower',
  'auth.validation.password_min',
  'auth.validation.password_number',
  'auth.validation.password_required',
  'auth.validation.password_upper',
  'auth.validation.passwords_differ',
  'auth.validation.use_case_max',
  'auth.validation.username_max',
  'auth.validation.username_min',
  'auth.verify.already_body',
  'auth.verify.already_message',
  'auth.verify.already_title',
  'auth.verify.back_to_login',
  'auth.verify.email_label',
  'auth.verify.error_body',
  'auth.verify.error_title',
  'auth.verify.go_to_login',
  'auth.verify.kicker',
  'auth.verify.loading_body',
  'auth.verify.loading_title',
  'auth.verify.missing_token',
  'auth.verify.resend_button',
  'auth.verify.resend_prompt',
  'auth.verify.resent_notice',
  'auth.verify.resent_toast',
  'auth.verify.signup_again',
  'auth.verify.success_body',
  'auth.verify.success_fallback',
  'auth.verify.success_title',
  'auth.verify.try_another',
  'chrome.footer.privacy',
  'chrome.footer.terms',
  'meta.team.description',
  'meta.team.title',
  'meta.terms.description',
  'meta.terms.title',
  'team.avatar_label',
  'team.eyebrow',
  'team.photo_alt',
  'team.subtitle',
  'team.subtitle_org_lead',
  'team.title_prefix',
  'terms.header.intro',
  'terms.header.title',
  'terms.s1.body_1',
  'terms.s1.body_2',
  'terms.s1.title',
  'terms.s10.body_1',
  'terms.s10.body_2',
  'terms.s10.title',
  'terms.s2.body_1',
  'terms.s2.body_2',
  'terms.s2.body_3',
  'terms.s2.title',
  'terms.s3.body_1',
  'terms.s3.body_2',
  'terms.s3.title',
  'terms.s4.body_1',
  'terms.s4.body_2',
  'terms.s4.title',
  'terms.s5.body_1',
  'terms.s5.body_2',
  'terms.s5.body_3',
  'terms.s5.body_4',
  'terms.s5.title',
  'terms.s6.body_1',
  'terms.s6.body_2',
  'terms.s6.title',
  'terms.s7.body_1',
  'terms.s7.body_2',
  'terms.s7.title',
  'terms.s8.body_1',
  'terms.s8.body_2',
  'terms.s8.title',
  'terms.s9.body_1',
  'terms.s9.title',
] as const;

export type AuthMessageKey = (typeof AUTH_MESSAGE_KEYS)[number];

/**
 * A module's wording for the account pages. Values are plain strings: this
 * interface localizes sentences, not markup, so a module cannot inject an
 * element into a place the shared page renders.
 */
export type AuthMessages = Partial<Record<AuthMessageKey, string>>;

/**
 * Client components, optional form styling and wording. Only the descriptor is
 * required. A page-owning component replaces the host's chrome for its route;
 * omitting it (or exporting `null`) preserves the shared page and chrome.
 */
export interface SiteUiClientModule {
  descriptor: SiteUiModuleDescriptor;
  /** Rendered at `/`, including its header, main region and footer. */
  Landing?: React.ComponentType<LandingPageProps> | null;
  /**
   * Owns the complete account-page chrome around the shared forms on `/login`,
   * `/signup`, `/forgot-password`, `/reset-password` and `/verify-email`.
   * Omitted or `null` keeps the console chrome and default account card.
   */
  AuthFrame?: React.ComponentType<AuthFrameProps> | null;
  /**
   * Frame for `/terms`, when the module publishes its own legal text.
   *
   * Absent means the console's terms page is the one this deployment shows.
   * Required *iff* the module also ships that text: the boundary asks whether a
   * frame exists to decide who draws the header, and it cannot ask a question
   * the answer is always yes to.
   */
  TermsFrame?: React.ComponentType<TermsFrameProps> | null;
  /**
   * Where each account field's label, control, hint, error and action go.
   *
   * Omitted means the default arrangement. A stylesheet can restyle a field
   * but not reorder it — putting the action beside the label is a change of
   * structure — so this is its own capability rather than part of the
   * deprecated class map, and it stays supported when that map is retired.
   */
  fieldLayout?: AuthFieldLayout;
  /** @deprecated Supported in v1; prefer semantic selectors for new styling. */
  authAppearance?: AuthAppearance;
  /** Wording for the account pages. */
  authMessages?: AuthMessages;
}

/** Static, non-React values read by the shared root layout. */
export interface SiteUiServerModule {
  /**
   * BCP-47 tag for the root `<html lang>`, including console routes.
   * Omitted or empty preserves the shared default (`en`). Titles, descriptions
   * and icons come from runtime branding and are not part of this module API.
   */
  locale?: string;
}
