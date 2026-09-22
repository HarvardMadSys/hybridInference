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
 * MAY: render the landing page, the public frame around the public routes, the
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
   * language. The public pages set `lang` from it; the console keeps its own.
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
 * The public frame a module renders around its own pages.
 *
 * Exactly one layer owns `<header>`, `<main>` and `<footer>`. A module that
 * renders the frame owns all three; the account pages render their own
 * two-column frame and must not also render this one, because the console's
 * container would then sit inside the design's full-width background.
 */
export interface PublicFrameProps {
  /** Which public route is being framed, when the module wants to vary. */
  route?: PublicRoute;
  children: React.ReactNode;
}

/**
 * Auth field styling: one class string per element the shared page renders.
 *
 * Class names are opaque to the host, so a module may use its own stylesheet,
 * and `data-auth` names every element so that a stylesheet does not have to
 * know which module is installed (see `docs/developer/site-ui.md`).
 *
 * **Nothing here changes structure.** Two entries used to: `titleInCard` said
 * whether the frame or the page drew the heading and the cross-link, and
 * `fieldActionInline` said whether a field's action sat beside its label or
 * after the fields. Both were load-bearing — flipping either changed the markup
 * — which is exactly the wrong kind of thing for a *styling* interface to
 * carry: a distribution's layout decision became a shared-repository branch, and
 * two places could render the same link with a boolean deciding which.
 *
 * The frame draws the heading and the cross-link now, whichever frame it is,
 * because the shared page hands it those nodes. Structural variation belongs in
 * a slot that receives nodes, not in a flag that changes who renders them.
 */
/**
 * How one field arranges the nodes the shared page produces.
 *
 * A module supplies this when its design puts the action somewhere other than
 * after the fields — beside the label, above the control, in a column. It
 * receives **rendered nodes** and returns markup; it does not receive the
 * field's value, its validation, its registration or its submit handler, and it
 * cannot change what any of them do. Every node is rendered exactly once, and
 * the shared component keeps ownership of the markup's ARIA relationships.
 *
 * The default arrangement is what the console has always drawn: the label, or a
 * row holding the label and the action when the field has one, then the control,
 * then the hint, then the error.
 *
 * This exists because two designs disagreed about where the "forgot password"
 * link goes, and the disagreement was previously expressed as a boolean that
 * changed *which layer rendered it*. A slot is the honest shape: the shared
 * field still produces the node, and the module says where it sits.
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

/** @deprecated Transitional class-map compatibility; use scoped semantic CSS. */
export interface AuthAppearance {
  form: string;
  field: string;
  label: string;
  labelRow: string;
  input: string;
  /**
   * The control's classes when the field has an error.
   *
   * A second string rather than a flag, because a layout interface can carry a
   * variant and cannot carry a branch. Without it every control keeps its valid
   * border while invalid, which is what made the console's red-border behaviour
   * disappear when these primitives moved.
   */
  inputError: string;
  passwordWrap: string;
  reveal: string;
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
  /**
   * Where a field's nodes are placed. Absent means the default arrangement,
   * which is what every module got before this existed.
   */
  fieldLayout?: AuthFieldLayout;
}

/**
 * Message keys a module may translate.
 *
 * The keys are declared here rather than open-ended so that a module can only
 * change *wording*: the validation rules, the field set and the error
 * semantics stay in the shared schemas. A key absent from a module's
 * dictionary falls back to the shared English default, which is what keeps a
 * partially translated deployment usable.
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
  'auth.reset.password_label',
  'auth.reset.remember',
  'auth.reset.submit',
  'auth.reset.submitting',
  'auth.reset.subtitle',
  'auth.reset.success_body',
  'auth.reset.success_kicker',
  'auth.reset.success_title',
  'auth.reset.success_toast',
  'auth.reset.title',
  'auth.shell.back_home',
  'auth.shell.brand_aria',
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
  'auth.signup.nickname_label',
  'auth.signup.password_hint',
  'auth.signup.password_label',
  'auth.signup.pending_title',
  'auth.signup.result_subtitle',
  'auth.signup.signin_link',
  'auth.signup.submit',
  'auth.signup.submitting',
  'auth.signup.subtitle',
  'auth.signup.success_title',
  'auth.signup.success_toast',
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
  'auth.verify.failure_title',
  'auth.verify.go_to_login',
  'auth.verify.kicker',
  'auth.verify.loading_body',
  'auth.verify.loading_title',
  'auth.verify.missing_token',
  'auth.verify.resend_button',
  'auth.verify.resend_prompt',
  'auth.verify.resending',
  'auth.verify.resent_notice',
  'auth.verify.resent_toast',
  'auth.verify.signup_again',
  'auth.verify.subtitle',
  'auth.verify.success_body',
  'auth.verify.success_fallback',
  'auth.verify.success_title',
  'auth.verify.title',
  'auth.verify.try_another',
  'chrome.footer.privacy',
  'chrome.footer.terms',
  'landing',
  'login',
  'meta.team.description',
  'meta.team.title',
  'meta.terms.description',
  'meta.terms.title',
  'signup',
  'team.avatar_label',
  'team.eyebrow',
  'team.photo_alt',
  'team.subtitle',
  'team.subtitle_org_lead',
  'team.title_prefix',
  'terms',
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
 * The client half of a module: React components plus optional appearance and
 * wording.
 *
 * Every key except the three optional ones is required. A module missing one
 * fails `assertSiteUiModule` at build time and the build stops, because the
 * alternative — falling back to the neutral page — publishes a site that is
 * half one design and half another.
 *
 * The three optional ones are the page-owning frames. Each answers the same
 * question for its route: *did the module draw this whole page?* — and the host
 * cannot ask it if the answer is always yes.
 */
export interface SiteUiClientModule {
  descriptor: SiteUiModuleDescriptor;
  /**
   * Rendered at `/` in place of the console's landing page.
   *
   * Optional, and `null` is an answer rather than an omission: the neutral
   * module declares `null`, meaning "the console's own landing page is the right
   * page here". Moving that page behind this interface would be churn with no
   * reader — it is a component tree of its own, with its own tests — so the host
   * asks a question the answer can legitimately be "no" to.
   */
  Landing?: React.ComponentType<LandingPageProps> | null;
  /**
   * Frame for `/login`, `/signup`, `/forgot-password`, `/reset-password`,
   * `/verify-email`.
   *
   * **Supplying one is a claim about the whole page, chrome included.** A frame
   * that exists renders the account page *entirely* — its own header, its own
   * `<main>`, its own footer — so `PublicRouteBoundary` steps out of the way for
   * these routes exactly as it does for a module's landing page or legal page.
   * A module that supplies one and draws only a card produces five pages with no
   * header and no footer; that is not a styling choice the host can correct.
   *
   * `null` — which the neutral module declares — means "the console's container
   * is the page here, and my frame is the card inside it", and the host renders
   * its own header, `<main>` and footer around the shared form. This is the one
   * question the boundary could not previously ask, because the field was
   * required and the answer was therefore always yes: the neutral module's
   * card-only frame claimed the whole page and the five default account routes
   * lost their chrome.
   *
   * Same shape as `Landing` and `TermsFrame`, and for the same reason: layout
   * and the decision to own a page travel together, so the host asks who drew
   * the page rather than reading a deployment's name.
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
   * The module's own layout, for the module's own pages.
   *
   * **The host never renders this.** It is here because a module with several
   * pages of its own needs a shared layout, and having one place the interface
   * names is better than each module inventing the same file — but nothing in
   * the host calls it, so it is optional. Requiring it asked every module for a
   * component nothing would use, and the neutral module answered with a
   * passthrough that did nothing, which is exactly the kind of export this
   * interface exists not to demand.
   */
  PublicFrame?: React.ComponentType<PublicFrameProps> | null;
  /** Field styling for the shared account forms. */
  authAppearance?: AuthAppearance;
  /** Wording for the account pages. */
  authMessages?: AuthMessages;
}

/**
 * Static, non-React half of a module, readable on the server.
 *
 * Both fields are optional and the descriptor deliberately does not appear: it
 * lives on the client half, so the two entries cannot disagree about which
 * interface revision is installed. A module that says nothing here gets the
 * console's own document language, title and icons.
 */
export interface SiteUiServerModule {
  /**
   * Overrides for the document `<head>` of the public routes. Plain,
   * serializable values: the host renders them and never executes them.
   */
  metadata?: SiteUiMetadata;
  /**
   * BCP-47 tag for `<html lang>` on the public routes. Ignored for the console,
   * which keeps its own language.
   */
  locale?: string;
}

export interface SiteUiMetadata {
  title?: string;
  description?: string;
  faviconUrl?: string;
}
