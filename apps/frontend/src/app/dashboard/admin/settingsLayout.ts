/**
 * How Admin → Settings is organised: an Overview, then one tab per topic. The
 * navigation beside the tabs groups them by what an administrator is doing:
 *
 *   Overview                          what is set up, what still needs a value,
 *                                     and where each thing is changed
 *   Access       Sign-up              who may create an account, and how a new
 *                                     one is checked and announced
 *                Sign-in & security   whether inference needs an API key, the
 *                                     administrators, blocking, sessions, phone
 *                                     sign-in and single sign-on
 *                Usage limits         how much each role may run at once and spend
 *   Inference    Providers            provider keys, addresses and the variables
 *                                     routes reference
 *                Routing              how a request finds its endpoint, RouteWise
 *                Batches              batch inference and each model's batch cap
 *   Monitoring   Alerts               muting, the channel, thresholds and rules
 *                Logging & privacy    what is recorded about requests, and the
 *                                     gateway's own log
 *                Analytics            what Admin → Analytics shows, Usage Insights
 *   Deployment   General              the gateway's name, addresses and branding
 *                Email                outgoing mail
 *                Network              browser origins and trusted proxies
 *                Integrations         the cloud agent, the documentation
 *                                     assistant, Qdrant and OpenInfra
 *                Backup & restore
 *                Other                what no other tab names: listed only while
 *                                     it holds something
 *
 * Each tab holds its whole topic: the runtime switches, the database-backed
 * configuration and the sections with an API of their own, so one change never
 * needs two tabs. A tab opens on what an administrator commonly changes; the
 * rest — rate limits, timeouts, intervals and thresholds, the JSON documents
 * and what is set once at deploy time — waits at its bottom, under Advanced,
 * collapsed. A tab with nothing to show on a gateway is not listed.
 *
 * A category can be shown in parts. ADVANCED_SETTINGS names a category's
 * tuning settings, which make a section of their own under the Advanced of
 * the category's tab. TOPIC_SECTIONS takes settings to the tab of their topic:
 * the gateway's log settings to Logging & privacy, and each JSON document to
 * the tab it configures.
 *
 * Whatever this file does not name is shown at the top of a tab: a setting a
 * later backend adds to a category stays with the rest of it (or with the
 * whole category, where that is under Advanced), and a category no tab names
 * and a runtime setting no group takes are listed on Other.
 *
 * The model catalog is the exception: it is on Admin → Routing → Models, and
 * not on this page.
 */

import type { ConfigSectionSplit } from '@/components/features/configuration/configDrafts';

export type SettingsTabKey =
  | 'overview'
  | 'signup'
  | 'security'
  | 'limits'
  | 'providers'
  | 'routing'
  | 'batches'
  | 'alerts'
  | 'logging'
  | 'analytics'
  | 'general'
  | 'email'
  | 'network'
  | 'integrations'
  | 'backup'
  | 'other';

/** A heading of the navigation, over the tabs of one kind of work. */
export type SettingsGroupKey = 'access' | 'inference' | 'monitoring' | 'deployment';

export const SETTINGS_GROUPS: readonly { key: SettingsGroupKey; label: string }[] = [
  { key: 'access', label: 'Access' },
  { key: 'inference', label: 'Inference' },
  { key: 'monitoring', label: 'Monitoring' },
  { key: 'deployment', label: 'Deployment' },
];

/** The configuration category of the model catalog, which Admin → Routing → Models edits. */
export const MODEL_CATALOG_CATEGORY = 'models';

/** Configuration categories another admin tab edits: Settings neither lists, searches nor counts them. */
export const CATEGORIES_ELSEWHERE: ReadonlySet<string> = new Set([MODEL_CATALOG_CATEGORY]);

/** A group of runtime settings, edited together with one Save. */
export type RuntimeGroupKey =
  | 'signup'
  | 'auth'
  | 'concurrency'
  | 'quotas'
  | 'requests'
  | 'logging'
  | 'analytics'
  | 'other';

/** A section with its own API, or one the page composes. */
export type SettingsPanelKey =
  | 'overview'
  | 'usage-insights'
  | 'signup-policy'
  | 'batch-concurrency'
  | 'routewise'
  | 'alert-snooze'
  | 'alert-mutes'
  | 'add-variable'
  | 'backup';

export type SettingsBlock =
  /**
   * A configuration category. With a `part`, the settings ADVANCED_SETTINGS
   * (`advanced`) or TOPIC_SECTIONS name of it; without, the rest of it.
   */
  | { kind: 'config'; category: string; part?: string }
  /** The configuration categories no tab names. */
  | { kind: 'unnamed-categories' }
  | { kind: 'runtime'; group: RuntimeGroupKey }
  | { kind: 'panel'; panel: SettingsPanelKey };

export interface SettingsTabDefinition {
  key: SettingsTabKey;
  label: string;
  /** The navigation's heading the tab is listed under; none for Overview, which leads them. */
  group?: SettingsGroupKey;
  /** What the tab holds, under its name above its sections. */
  description: string;
  /** The sections an administrator commonly changes, top to bottom. */
  blocks: readonly SettingsBlock[];
  /**
   * The tuning and the sections seldom changed, top to bottom: at the bottom
   * of the tab, under Advanced, which is collapsed until it is opened.
   */
  advanced: readonly SettingsBlock[];
  /** What Advanced holds, said beside it while it is collapsed. Empty when it holds nothing. */
  advancedDescription: string;
}

/**
 * The tuning settings of each category that also holds everyday ones: rate
 * limits, timeouts, intervals and thresholds, size and storage limits, the
 * defaults offered for new routes, and what is generated or set once at
 * deploy time and then left alone. They are listed under the Advanced of the
 * category's tab, in a section of their own; the rest of each category stays
 * at the top, and so does any setting a later backend adds to it until it is
 * named here.
 */
export const ADVANCED_SETTINGS: Readonly<Record<string, readonly string[]>> = {
  // Who hears about a new account, and the signup captcha, stay at the top.
  signup: [
    'SIGNUP_RATE_LIMIT_PER_HOUR',
    'SIGNUP_RATE_LIMIT_PER_DAY',
    'SIGNUP_REQUIRE_RESOLVED_CLIENT_IP',
    'UNRESOLVED_SIGNUP_RATE_LIMIT_PER_HOUR',
    'UNRESOLVED_SIGNUP_RATE_LIMIT_PER_DAY',
    // Deprecated: the per-role daily quotas replace it.
    'SIGNUP_DEFAULT_DAILY_QUOTA_USD',
  ],
  // The administrators, whether a failing client is blocked, and the addresses
  // never blocked stay at the top, with the single sign-on secrets.
  security: [
    // Generated on first boot, and changed seldom if ever.
    'JWT_SECRET_KEY',
    'API_KEY_SECRET',
    'ADMIN_TOKEN',
    // Sessions and their cookie.
    'JWT_ALGORITHM',
    'JWT_ACCESS_TOKEN_EXPIRE_MINUTES',
    'JWT_REFRESH_TOKEN_EXPIRE_DAYS',
    'COOKIE_SECURE',
    'COOKIE_DOMAIN',
    'COOKIE_SAMESITE',
    // Sign-in rate limits, and when a failing client is blocked.
    'LOGIN_RATE_LIMIT_PER_15MIN',
    'LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN',
    'LOGIN_RATE_LIMIT_PER_HOUR_PER_IP',
    'AUTH_FAILURE_BLOCK_THRESHOLD',
    'AUTH_FAILURE_BLOCK_WINDOW_SEC',
    'AUTH_FAILURE_BLOCK_DURATION_SEC',
    'UNRESOLVED_AUTH_FAILURE_BLOCK_THRESHOLD',
    'UNRESOLVED_AUTH_FAILURE_BLOCK_WINDOW_SEC',
    'UNRESOLVED_AUTH_FAILURE_BLOCK_DURATION_SEC',
    'AUTH_FAILURE_IDENTIFY_CALLER',
  ],
  // Whether phone sign-in is on, its provider and the numbers it takes stay at the top.
  phone: [
    'SMS_OTP_EXPIRE_SECONDS',
    'SMS_OTP_SEND_COOLDOWN_SECONDS',
    'SMS_OTP_SEND_PER_PHONE_HOUR',
    'SMS_OTP_SEND_PER_PHONE_DAY',
    'SMS_OTP_SEND_PER_IP_HOUR',
    'SMS_OTP_SEND_PER_IP_DAY',
    'SMS_OTP_SEND_UNRESOLVED_PER_HOUR',
    'SMS_OTP_SEND_UNRESOLVED_PER_DAY',
    'SMS_OTP_MAX_ATTEMPTS',
    'SMS_OTP_VERIFY_PER_PHONE_15MIN',
    'SMS_OTP_VERIFY_PER_IP_HOUR',
  ],
  // The provider keys, the variables the routes reference and the local
  // proxies' shared key stay at the top.
  providers: [
    'PROVIDER_ROUTE_TYPES',
    'CHUTES_BASE_URL',
    'FEATHERLESS_BASE_URL',
    'MINIMAX_BASE_URL',
    'MODEL_QUEUE_STATUS_URL',
    'MODEL_QUEUE_STATUS_API_KEY',
  ],
  // The switches stay at the top: RouteWise as the default router, outbound
  // concurrency control, caller affinity and prefill steering.
  routing: [
    // Outbound concurrency per remote account.
    'UPSTREAM_CONCURRENCY_INITIAL_LIMIT',
    'UPSTREAM_CONCURRENCY_MAX_LIMIT',
    'UPSTREAM_CONCURRENCY_PROBE_SUCCESS_INTERVAL',
    'UPSTREAM_CONCURRENCY_ACQUIRE_TIMEOUT_SEC',
    // Endpoint health and the circuit breaker.
    'ROUTER_HEALTH_EWMA_ALPHA',
    'CIRCUIT_FAILURE_THRESHOLD',
    'CIRCUIT_COOLDOWN_SECONDS',
    'CIRCUIT_MIN_AVAILABILITY',
    'CIRCUIT_CALLER_WINDOW_SECONDS',
    // Affinity, prefill steering and scheduling priorities.
    'ROUTING_AFFINITY_MAX_AGE_SEC',
    'ROUTING_PREFILL_ELEPHANT_TOKENS',
    'ROUTING_PREFILL_ELEPHANT_LIMIT',
    'ROUTING_PREFILL_INTERVENE_TOKENS',
    'ROUTING_PREFILL_AFFINITY_CEILING',
    'ROUTING_PREFILL_HINT_TTL_SEC',
    'ROUTING_PRIORITY_INTERACTIVE',
    'ROUTING_PRIORITY_LARGE',
    'ROUTING_PRIORITY_ELEPHANT',
    // Timeouts and the largest request body.
    'UPSTREAM_COMPLETION_TIMEOUT_S',
    'STREAM_IDLE_TIMEOUT_SECONDS',
    'STREAM_FIRST_BYTE_TIMEOUT_SECONDS',
    'STREAM_MAX_IDLE_S',
    'STREAM_MAX_FIRST_FRAME_IDLE_S',
    'HTTP_IDLE_TIMEOUT_SECONDS',
    'REQUEST_TIMEOUT_SECONDS',
    'STREAM_REQUEST_TIMEOUT_SECONDS',
    'REQUEST_MAX_BODY_MB',
    // The reroute of tiny reasoning calls, which a runtime switch turns on.
    'SMALL_MAXTOK_REASONING_TARGET',
    'SMALL_MAXTOK_REASONING_THRESHOLD',
    'SMALL_MAXTOK_REASONING_FLOOR',
  ],
  // Whether alerts run, and the channel they are sent to, stay at the top.
  alerts: [
    // When failed requests page.
    'FAILED_REQUEST_ALERT_THRESHOLD',
    'FAILED_REQUEST_ALERT_WINDOW_MINUTES',
    'FAILED_REQUEST_ALERT_COOLDOWN_MINUTES',
    'FAILED_REQUEST_ALERT_RATE',
    'FAILED_REQUEST_ALERT_RATE_MIN_COUNT',
    // The environment name on alerts, and the alert history.
    'DEPLOYMENT_ENV',
    'ENVIRONMENT',
    'ALERT_HISTORY_ENABLED',
    'ALERT_HISTORY_DEPLOYMENT_ID',
    'ALERT_HISTORY_READ_CREDENTIALS',
    'ALERT_HISTORY_WRITER_CREDENTIALS',
  ],
  // Whether batches run, who may submit them, each model's default cap, and how
  // long a batch has to finish and is kept stay at the top.
  batch: [
    // How batch work shares a model with interactive requests.
    'BATCH_YIELD_TO_INTERACTIVE',
    'BATCH_SHARED_INTERACTIVE_LOAD',
    'BATCH_INTERACTIVE_RESERVE',
    'BATCH_ROLE_CONCURRENCY_FREE',
    'BATCH_ROLE_CONCURRENCY_PRO',
    'BATCH_ROLE_CONCURRENCY_INTERNAL',
    'BATCH_ROLE_CONCURRENCY_ADMIN',
    // Size, storage and upload limits.
    'BATCH_MAX_REQUESTS',
    'BATCH_MAX_BODY_MB',
    'BATCH_MAX_ACTIVE_PER_USER',
    'BATCH_MAX_QUEUED_REQUESTS_PER_USER',
    'BATCH_MAX_LINE_KB',
    'BATCH_MAX_RESULT_KB',
    'BATCH_MAX_RESULTS_MB_PER_BATCH',
    'BATCH_FILES_MAX_TOTAL_MB_PER_USER',
    'BATCH_FILE_UPLOADS_PER_HOUR',
    'BATCH_FILE_RETENTION_DAYS',
    'BATCH_QUEUED_ALERT_HOURS',
    'BATCH_ANTHROPIC_VERSIONS',
    'BATCH_WEBHOOK_ALLOW_PRIVATE_TARGETS',
  ],
  // The gateway's name, its addresses and its support address stay at the top.
  // Its log level and format are on Logging & privacy (TOPIC_SECTIONS).
  general: ['SITE_PUBLIC_BASE_URL', 'SITE_DOCS_URL'],
  // Whether request content is stored stays at the top.
  privacy: ['ERASURE_FENCE_SECRET', 'ERASURE_FENCE_PROTOCOL_READY'],
};

/** A part of a category shown on the tab of its topic, apart from the rest of the category. */
export interface TopicSection {
  category: string;
  /** Names the part: its section's id is `category:part`. */
  part: string;
  keys: readonly string[];
  /** The section's title, in place of its category's. */
  label: string;
  /** What the section is for, in place of its category's description. */
  description: string;
}

/**
 * Settings shown with their topic rather than with the rest of their
 * category: each application document on the tab it configures, and the
 * gateway's own log settings with the other logging. A document a later
 * backend adds stays in Application, under the Advanced of General.
 */
export const TOPIC_SECTIONS: readonly TopicSection[] = [
  {
    category: 'general',
    part: 'logging',
    keys: ['LOG_LEVEL', 'LOG_FORMAT'],
    label: 'Server log',
    description: 'How much the gateway writes to its own log, and in which format.',
  },
  {
    category: 'application',
    part: 'sso',
    keys: ['SSO_PROVIDERS'],
    label: 'Single sign-on providers',
    description: 'The identity providers users may sign in with, as a document.',
  },
  {
    category: 'application',
    part: 'routing-policy',
    keys: ['ROUTING_POLICY'],
    label: 'Routing policy',
    description:
      'The deployment-wide routing policy, as a document: the router a model gets when it names none.',
  },
  {
    category: 'application',
    part: 'alert-rules',
    keys: ['ALERT_RULES'],
    label: 'Alert rules',
    description: 'Alert thresholds and which rules run, as a document.',
  },
  {
    category: 'application',
    part: 'site-configuration',
    keys: ['SITE_CONFIGURATION'],
    label: 'Site configuration',
    description: 'The public site identity and feature policy, as a document.',
  },
  {
    category: 'application',
    part: 'branding',
    keys: ['SITE_BRANDING'],
    label: 'Branding',
    description: 'The public branding, as a document.',
  },
];

/** The part ADVANCED_SETTINGS makes of a category. */
const ADVANCED_PART = 'advanced';

/** The id of the section that holds a part of a category. */
export function partSectionId(category: string, part: string): string {
  return `${category}:${part}`;
}

/** The id of the section that holds a category's advanced settings. */
export function advancedSectionId(category: string): string {
  return partSectionId(category, ADVANCED_PART);
}

/**
 * What a section is for, by its id, where the description of its category
 * would say what the page shows elsewhere: General's speaks of logging, which
 * TOPIC_SECTIONS takes to Logging & privacy.
 */
export const SECTION_DESCRIPTIONS: Readonly<Record<string, string>> = {
  general: 'Public addresses and site identity.',
  [advancedSectionId('general')]:
    'Overrides of the site addresses that backend-produced content links to.',
};

/**
 * Every part ADVANCED_SETTINGS and TOPIC_SECTIONS make, as the configuration
 * editor takes them: each a section of its own, with its own Save.
 */
export const SETTINGS_SECTIONS: readonly ConfigSectionSplit[] = [
  ...Object.entries(ADVANCED_SETTINGS).map(([category, keys]) => ({
    id: advancedSectionId(category),
    category,
    keys: new Set(keys),
    label: (categoryLabel: string) => `${categoryLabel} (advanced)`,
    description: SECTION_DESCRIPTIONS[advancedSectionId(category)],
  })),
  ...TOPIC_SECTIONS.map((section) => ({
    id: partSectionId(section.category, section.part),
    category: section.category,
    keys: new Set(section.keys),
    label: () => section.label,
    description: section.description,
  })),
];

/** The configuration section a block shows: its category's, or a part of it. */
export function sectionOfBlock(block: Extract<SettingsBlock, { kind: 'config' }>): string {
  return block.part ? partSectionId(block.category, block.part) : block.category;
}

const config = (category: string): SettingsBlock => ({ kind: 'config', category });
/** The settings of a category that ADVANCED_SETTINGS names. */
const advancedConfig = (category: string): SettingsBlock => ({
  kind: 'config',
  category,
  part: ADVANCED_PART,
});
/** A part of a category that TOPIC_SECTIONS names. */
const topicConfig = (category: string, part: string): SettingsBlock => ({
  kind: 'config',
  category,
  part,
});
const runtime = (group: RuntimeGroupKey): SettingsBlock => ({ kind: 'runtime', group });
const panel = (key: SettingsPanelKey): SettingsBlock => ({ kind: 'panel', panel: key });

export const SETTINGS_TABS: readonly SettingsTabDefinition[] = [
  {
    key: 'overview',
    label: 'Overview',
    description:
      'What is set up, what still needs a value, and where each thing is changed. Search finds any setting by its name, its description or a model.',
    blocks: [panel('overview')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'signup',
    label: 'Sign-up',
    group: 'access',
    description:
      'Who may create an account, how a new one is checked, and who hears about it. Switches take effect immediately.',
    blocks: [runtime('signup'), panel('signup-policy'), config('signup')],
    advanced: [advancedConfig('signup')],
    advancedDescription:
      'Sign-up rate limits, and the default quota the per-role daily quotas replace.',
  },
  {
    key: 'security',
    label: 'Sign-in & security',
    group: 'access',
    description:
      'Whether inference needs an API key, who the administrators are, how failing clients are blocked, and phone sign-in.',
    blocks: [runtime('auth'), config('security'), config('phone')],
    advanced: [
      advancedConfig('security'),
      advancedConfig('phone'),
      topicConfig('application', 'sso'),
    ],
    advancedDescription:
      'The signing secrets and the admin token, sessions and their cookie, sign-in rate limits and when a failing client is blocked, the limits on phone verification codes, and the single sign-on providers.',
  },
  {
    key: 'limits',
    label: 'Usage limits',
    group: 'access',
    description:
      'How many requests one user may have in flight, and how much a new API key may spend a day, by role. Changes take effect immediately.',
    blocks: [runtime('concurrency'), runtime('quotas')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'providers',
    label: 'Providers',
    group: 'inference',
    description:
      'The API keys and addresses of model providers, and the variables your routes reference. The model catalog itself is under Routing → Models.',
    // Added variables are listed under Providers, so the form that adds one follows it.
    blocks: [config('providers'), panel('add-variable')],
    advanced: [advancedConfig('providers')],
    advancedDescription:
      'The route types offered per provider, the default base URLs offered for new routes, and the model queue.',
  },
  {
    key: 'routing',
    label: 'Routing',
    group: 'inference',
    description:
      'How a request finds its endpoint: the default router, outbound concurrency, affinity and prefill steering, and how requests are sent upstream.',
    blocks: [config('routing'), runtime('requests')],
    advanced: [
      advancedConfig('routing'),
      panel('routewise'),
      topicConfig('application', 'routing-policy'),
    ],
    advancedDescription:
      'Timeouts and the largest request, endpoint health and the circuit breaker, outbound concurrency limits, prefill steering, the RouteWise parameters of each model, and the routing policy.',
  },
  {
    key: 'batches',
    label: 'Batches',
    group: 'inference',
    description:
      'Whether batch inference runs, who may submit batches, how long they are kept, and how much of each model batch work may use.',
    blocks: [config('batch'), panel('batch-concurrency')],
    advanced: [advancedConfig('batch')],
    advancedDescription:
      'How batch work shares a model with interactive requests, the per-role caps, and the limits on batch size, storage and uploads.',
  },
  {
    key: 'alerts',
    label: 'Alerts',
    group: 'monitoring',
    description:
      'Where backend alerts are sent, and which are sent. Snooze or mute them during maintenance or a known incident.',
    // Snoozing is what an incident calls for; the channel follows it, and the
    // long list of alert types to mute one by one comes last.
    blocks: [panel('alert-snooze'), config('alerts'), panel('alert-mutes')],
    advanced: [advancedConfig('alerts'), topicConfig('application', 'alert-rules')],
    advancedDescription:
      'When failed requests page, the environment name on alerts, the alert history, and the alert rules.',
  },
  {
    key: 'logging',
    label: 'Logging & privacy',
    group: 'monitoring',
    description:
      'What the gateway records about requests, whether it keeps their content, and what it writes to its own log.',
    blocks: [config('privacy'), runtime('logging')],
    advanced: [topicConfig('general', 'logging'), advancedConfig('privacy')],
    advancedDescription:
      'The level and format of the gateway’s own log, and the fence that keeps deleted accounts deleted.',
  },
  {
    key: 'analytics',
    label: 'Analytics',
    group: 'monitoring',
    description:
      'Which sections Admin → Analytics shows every administrator, and the model Usage Insights analyses usage with.',
    blocks: [runtime('analytics'), panel('usage-insights')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'general',
    label: 'General',
    group: 'deployment',
    description:
      'The gateway’s name, its public addresses and its support address: usually set once, when it is deployed.',
    blocks: [config('general')],
    advanced: [
      advancedConfig('general'),
      topicConfig('application', 'site-configuration'),
      topicConfig('application', 'branding'),
      // The documents this layout does not place: none, until a backend adds one.
      config('application'),
    ],
    advancedDescription:
      'Overrides of the site addresses in backend-produced content, the site configuration and the branding.',
  },
  {
    key: 'email',
    label: 'Email',
    group: 'deployment',
    description:
      'How the gateway sends verification, password-reset and notification emails. Send a test once it is saved.',
    blocks: [config('email')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'network',
    label: 'Network',
    group: 'deployment',
    description:
      'Which browser origins may call the API, and which proxies may report a client’s address. Usually set once, when the gateway is deployed.',
    blocks: [config('network')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'integrations',
    label: 'Integrations',
    group: 'deployment',
    description:
      'The cloud agent’s identity tokens, the documentation assistant, the Qdrant vector database and OpenInfra collection.',
    blocks: [config('integrations')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'backup',
    label: 'Backup & restore',
    group: 'deployment',
    description:
      'Download the whole configuration, or restore it from a backup. Take one before a large change.',
    blocks: [panel('backup')],
    advanced: [],
    advancedDescription: '',
  },
  {
    key: 'other',
    label: 'Other',
    group: 'deployment',
    description:
      'Settings this console has no other place for yet, such as ones a newer backend added.',
    // Runtime settings no group takes are new to this console: shown, not tucked away.
    blocks: [{ kind: 'unnamed-categories' }, runtime('other')],
    advanced: [],
    advancedDescription: '',
  },
];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = 'overview';

/** The tab a `?tab=` value names, or the default for anything else. */
export function settingsTabKey(value: string | undefined | null): SettingsTabKey {
  return SETTINGS_TABS.find((tab) => tab.key === value)?.key ?? DEFAULT_SETTINGS_TAB;
}

/** Every block of a tab: those at the top, then those under Advanced. */
export function blocksOf(tab: SettingsTabDefinition): readonly SettingsBlock[] {
  return [...tab.blocks, ...tab.advanced];
}

/** The tab that places each configuration section, by the section's id. */
const SECTION_TABS: ReadonlyMap<string, SettingsTabKey> = new Map(
  SETTINGS_TABS.flatMap((tab) =>
    blocksOf(tab).flatMap((block) =>
      block.kind === 'config' ? [[sectionOfBlock(block), tab.key] as const] : [],
    ),
  ),
);

/** The configuration categories a tab names, whole or in part. */
const NAMED_CATEGORIES: ReadonlySet<string> = new Set(
  SETTINGS_TABS.flatMap((tab) =>
    blocksOf(tab).flatMap((block) => (block.kind === 'config' ? [block.category] : [])),
  ),
);

/** The tab that lists the configuration categories no tab names. */
const UNNAMED_CATEGORIES_TAB: SettingsTabKey =
  SETTINGS_TABS.find((tab) => blocksOf(tab).some((block) => block.kind === 'unnamed-categories'))
    ?.key ?? DEFAULT_SETTINGS_TAB;

/** Whether a tab names the category; one that none names is listed on Other. */
export function isNamedCategory(category: string): boolean {
  return NAMED_CATEGORIES.has(category);
}

/**
 * The tab a configuration section is listed on: the one that places it, or,
 * for a part no tab places, the one that places the rest of its category. A
 * category no tab names is listed on Other.
 */
export function tabOfSection(sectionId: string, category: string = sectionId): SettingsTabKey {
  return SECTION_TABS.get(sectionId) ?? SECTION_TABS.get(category) ?? UNNAMED_CATEGORIES_TAB;
}

/** The tab a runtime group is listed on. */
export function tabOfRuntimeGroup(group: RuntimeGroupKey): SettingsTabKey {
  return (
    SETTINGS_TABS.find((tab) =>
      blocksOf(tab).some((block) => block.kind === 'runtime' && block.group === group),
    )?.key ?? DEFAULT_SETTINGS_TAB
  );
}

export const RUNTIME_GROUPS: Readonly<
  Record<RuntimeGroupKey, { title: string; description: string }>
> = {
  signup: {
    title: 'Sign-up Controls',
    description:
      'Whether new users may sign up, whether they verify their email first, and whether administrators hear about each one. Changes take effect immediately.',
  },
  auth: {
    title: 'API Access',
    description:
      'Whether inference requests need a user’s API key. Signing in to the console is not affected. Changes take effect immediately.',
  },
  concurrency: {
    title: 'Per-User Concurrency',
    description:
      'How many requests one user may have in flight, by role. Changes take effect immediately.',
  },
  quotas: {
    title: 'Daily Quotas',
    description:
      'The daily spend quota a new API key gets, by role. Apply a saved quota to change the keys users already have.',
  },
  requests: {
    title: 'Request Handling',
    description: 'How requests are sent to models. Changes take effect immediately.',
  },
  logging: {
    title: 'Request Logging',
    description: 'What the gateway records about requests. Changes take effect immediately.',
  },
  analytics: {
    title: 'Analytics',
    description: 'Choose which sections appear on the Analytics overview.',
  },
  other: {
    title: 'Other Runtime Settings',
    description:
      'Runtime settings with no other place on this page. Changes take effect immediately.',
  },
};

const REQUEST_HANDLING_SETTINGS: ReadonlySet<string> = new Set([
  'force_chat_completions_streaming',
  'reasoning_small_call_reroute_enabled',
  'auto_mode_enabled',
]);

/** The group a runtime setting is edited in. */
export function runtimeGroupOf(key: string): RuntimeGroupKey {
  if (key.startsWith('analytics_')) return 'analytics';
  if (key.startsWith('log_')) return 'logging';
  if (key === 'user_auth_enabled') return 'auth';
  if (key.startsWith('signup_')) return 'signup';
  if (key.startsWith('user_concurrency_')) return 'concurrency';
  if (key.startsWith('user_daily_quota_')) return 'quotas';
  if (REQUEST_HANDLING_SETTINGS.has(key)) return 'requests';
  return 'other';
}
