/**
 * How Admin → Settings is organised: four task tabs, each holding everything
 * for one job — the runtime switches, the database-backed configuration and
 * the sections with an API of their own — so an administrator never visits
 * two tabs for one change.
 *
 *   Access & Limits    who may sign up or sign in, and how much each role
 *                      may run and spend
 *   Providers & Routing  provider credentials, routing and how a request is
 *                      sent to a model
 *   Operations         alerts, batch work, logging and analytics — watching
 *                      and running the gateway day to day
 *   Site & Setup       what is usually set once at deploy time, and backup &
 *                      restore
 *
 * Each tab opens on what an administrator commonly changes. The rest — rate
 * limits, timeouts, intervals and thresholds, logging and analytics detail,
 * the JSON documents and the categories seldom touched — waits at the bottom
 * of the tab, under Advanced, collapsed. A category that holds both kinds has
 * its tuning settings named in ADVANCED_SETTINGS: they make a section of their
 * own under Advanced, and the rest of the category stays at the top.
 *
 * Whatever this file does not name is shown at the top: a setting a later
 * backend adds to a category (unless the whole category is under Advanced), a
 * category no tab names, and a runtime setting no group takes.
 *
 * The model catalog is the exception: it is on Admin → Routing → Models,
 * and not on this page.
 */

import type { ConfigSectionSplit } from '@/components/features/configuration/configDrafts';

export type SettingsTabKey = 'access' | 'providers' | 'operations' | 'setup';

/** The configuration category of the model catalog, which Admin → Routing → Models edits. */
export const MODEL_CATALOG_CATEGORY = 'models';

/** Configuration categories another admin tab edits: Settings neither lists, searches nor counts them. */
export const CATEGORIES_ELSEWHERE: ReadonlySet<string> = new Set([MODEL_CATALOG_CATEGORY]);

/** A group of runtime settings, edited together with one Save. */
export type RuntimeGroupKey =
  | 'logging'
  | 'analytics'
  | 'access'
  | 'requests'
  | 'concurrency'
  | 'quotas'
  | 'other';

/** A section with its own API, or one the page composes. */
export type SettingsPanelKey =
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
   * A configuration category. Where ADVANCED_SETTINGS names some of its
   * settings, the block with `part: 'advanced'` shows those, and the block
   * without it the rest of the category.
   */
  | { kind: 'config'; category: string; part?: 'advanced' }
  /** The configuration categories no tab names. */
  | { kind: 'unnamed-categories' }
  | { kind: 'runtime'; group: RuntimeGroupKey }
  | { kind: 'panel'; panel: SettingsPanelKey };

export interface SettingsTabDefinition {
  key: SettingsTabKey;
  label: string;
  /** What the tab holds, above its sections, where its name alone does not say. */
  description?: string;
  /** The sections an administrator commonly changes, top to bottom. */
  blocks: readonly SettingsBlock[];
  /**
   * The tuning and the sections seldom changed, top to bottom: at the bottom
   * of the tab, under Advanced, which is collapsed until it is opened.
   */
  advanced: readonly SettingsBlock[];
  /** What Advanced holds, said beside it while it is collapsed. */
  advancedDescription: string;
}

/**
 * The tuning settings of each category that also holds everyday ones: rate
 * limits, timeouts, intervals and thresholds, size and storage limits, the
 * defaults offered for new routes, and what is generated or set once at
 * deploy time and then left alone. They are listed under the tab's Advanced,
 * in a section of their own; the rest of each category stays at the top,
 * and so does any setting a later backend adds to it until it is named here.
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
  general: ['SITE_PUBLIC_BASE_URL', 'SITE_DOCS_URL', 'LOG_LEVEL', 'LOG_FORMAT'],
  // Whether request content is stored stays at the top.
  privacy: ['ERASURE_FENCE_SECRET', 'ERASURE_FENCE_PROTOCOL_READY'],
};

/** The id of the section that holds a category's advanced settings. */
export function advancedSectionId(category: string): string {
  return `${category}:advanced`;
}

/** The sections ADVANCED_SETTINGS makes, as the configuration editor takes them. */
export const ADVANCED_SECTIONS: readonly ConfigSectionSplit[] = Object.entries(
  ADVANCED_SETTINGS,
).map(([category, keys]) => ({
  id: advancedSectionId(category),
  category,
  keys: new Set(keys),
  label: (categoryLabel: string) => `${categoryLabel} (advanced)`,
}));

/** The configuration section a block shows: its category's, or that category's advanced one. */
export function sectionOfBlock(block: Extract<SettingsBlock, { kind: 'config' }>): string {
  return block.part === 'advanced' ? advancedSectionId(block.category) : block.category;
}

const config = (category: string): SettingsBlock => ({ kind: 'config', category });
/** The settings of a category that ADVANCED_SETTINGS names. */
const advancedConfig = (category: string): SettingsBlock => ({
  kind: 'config',
  category,
  part: 'advanced',
});
const runtime = (group: RuntimeGroupKey): SettingsBlock => ({ kind: 'runtime', group });
const panel = (key: SettingsPanelKey): SettingsBlock => ({ kind: 'panel', panel: key });

export const SETTINGS_TABS: readonly SettingsTabDefinition[] = [
  {
    key: 'access',
    label: 'Access & Limits',
    description:
      'Who may sign up or sign in, and how much each role may run at once and spend per day. Changes take effect immediately.',
    blocks: [
      runtime('access'),
      runtime('concurrency'),
      runtime('quotas'),
      panel('signup-policy'),
      config('signup'),
      config('security'),
      config('phone'),
    ],
    advanced: [advancedConfig('signup'), advancedConfig('security'), advancedConfig('phone')],
    advancedDescription:
      'Rate limits on sign-up and sign-in, when a failing client is blocked, sessions and their cookie, the signing secrets, and the limits on phone verification codes.',
  },
  {
    key: 'providers',
    label: 'Providers & Routing',
    description:
      'Provider credentials, routing, and how a request is sent to a model. The model catalog itself is under Routing → Models.',
    // Added variables are listed under Providers, so the form that adds one follows it.
    blocks: [config('providers'), panel('add-variable'), config('routing'), runtime('requests')],
    advanced: [advancedConfig('providers'), advancedConfig('routing'), panel('routewise')],
    advancedDescription:
      'The defaults offered for new routes, the model queue, timeouts and the largest request, endpoint health and the circuit breaker, outbound concurrency limits, prefill steering, and the RouteWise parameters of each model.',
  },
  {
    key: 'operations',
    label: 'Operations',
    description: 'Alerts, batch work, and what the gateway records and shows about requests.',
    // Muting is what an incident calls for; the channel and thresholds are set beforehand.
    blocks: [
      panel('alert-snooze'),
      panel('alert-mutes'),
      config('alerts'),
      config('batch'),
      panel('batch-concurrency'),
      // Runtime settings no group takes are new to this console: shown, not tucked away.
      runtime('other'),
    ],
    advanced: [
      advancedConfig('alerts'),
      advancedConfig('batch'),
      runtime('logging'),
      runtime('analytics'),
    ],
    advancedDescription:
      'When failed requests page, the environment name and history of alerts, the limits on batch size, storage and each role, what is logged about requests, and which sections Admin → Analytics shows.',
  },
  {
    key: 'setup',
    label: 'Site & Setup',
    description:
      'Usually set once, when the gateway is deployed: its name and addresses, outgoing mail and privacy. Backup & restore follows them.',
    blocks: [
      config('general'),
      config('email'),
      config('privacy'),
      { kind: 'unnamed-categories' },
      panel('backup'),
    ],
    advanced: [
      advancedConfig('general'),
      config('network'),
      advancedConfig('privacy'),
      config('integrations'),
      config('application'),
      panel('usage-insights'),
    ],
    advancedDescription:
      'Logging, overrides of the site addresses, the network in front of the gateway, account deletion, the integrations, the JSON documents and Usage Insights.',
  },
];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = 'access';

/** The tab a `?tab=` value names, or the default for anything else. */
export function settingsTabKey(value: string | undefined | null): SettingsTabKey {
  return SETTINGS_TABS.find((tab) => tab.key === value)?.key ?? DEFAULT_SETTINGS_TAB;
}

/** Every block of a tab: those at the top, then those under Advanced. */
export function blocksOf(tab: SettingsTabDefinition): readonly SettingsBlock[] {
  return [...tab.blocks, ...tab.advanced];
}

/** The configuration categories a tab names, in its order. */
const NAMED_CATEGORIES: ReadonlyMap<string, SettingsTabKey> = new Map(
  SETTINGS_TABS.flatMap((tab) =>
    blocksOf(tab).flatMap((block) =>
      block.kind === 'config' ? [[block.category, tab.key] as const] : [],
    ),
  ),
);

/** The tab that lists the configuration categories no tab names. */
const UNNAMED_CATEGORIES_TAB: SettingsTabKey =
  SETTINGS_TABS.find((tab) => blocksOf(tab).some((block) => block.kind === 'unnamed-categories'))
    ?.key ?? DEFAULT_SETTINGS_TAB;

/** Whether a tab names the category; one that none names is listed on Site & Setup. */
export function isNamedCategory(category: string): boolean {
  return NAMED_CATEGORIES.has(category);
}

/** The tab a configuration category is listed on. */
export function tabOfCategory(category: string): SettingsTabKey {
  return NAMED_CATEGORIES.get(category) ?? UNNAMED_CATEGORIES_TAB;
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
  analytics: {
    title: 'Analytics',
    description: 'Choose which sections appear on the Analytics overview.',
  },
  logging: {
    title: 'Request Logging',
    description: 'What the gateway records about requests. Changes take effect immediately.',
  },
  access: {
    title: 'Access Controls',
    description:
      'Whether inference needs an API key, whether new users may sign up, and how a signup is checked. Changes take effect immediately.',
  },
  requests: {
    title: 'Request Handling',
    description: 'How requests are sent to models. Changes take effect immediately.',
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
  if (key === 'user_auth_enabled' || key.startsWith('signup_')) return 'access';
  if (key.startsWith('user_concurrency_')) return 'concurrency';
  if (key.startsWith('user_daily_quota_')) return 'quotas';
  if (REQUEST_HANDLING_SETTINGS.has(key)) return 'requests';
  return 'other';
}
