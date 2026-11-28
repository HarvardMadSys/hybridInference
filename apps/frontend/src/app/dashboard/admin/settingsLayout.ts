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
 *   Site & Setup       what is usually set once at deploy time, the JSON
 *                      documents, and backup & restore at the bottom
 *
 * The model catalog is the exception: it is on Admin → Routing → Models,
 * and not on this page.
 */

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
  | { kind: 'config'; category: string }
  /** The configuration categories no tab names. */
  | { kind: 'unnamed-categories' }
  | { kind: 'runtime'; group: RuntimeGroupKey }
  | { kind: 'panel'; panel: SettingsPanelKey };

export interface SettingsTabDefinition {
  key: SettingsTabKey;
  label: string;
  /** What the tab holds, above its sections, where its name alone does not say. */
  description?: string;
  /** The tab's sections, top to bottom. */
  blocks: readonly SettingsBlock[];
}

const config = (category: string): SettingsBlock => ({ kind: 'config', category });
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
  },
  {
    key: 'providers',
    label: 'Providers & Routing',
    description:
      'Provider credentials, routing (including RouteWise parameters), and how a request is sent to a model. The model catalog itself is under Routing → Models.',
    // Added variables are listed under Providers, so the form that adds one follows it.
    blocks: [
      config('providers'),
      panel('add-variable'),
      config('routing'),
      panel('routewise'),
      runtime('requests'),
    ],
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
      runtime('logging'),
      runtime('analytics'),
      runtime('other'),
    ],
  },
  {
    key: 'setup',
    label: 'Site & Setup',
    description:
      'Usually set once, when the gateway is deployed: its name and addresses, outgoing mail, the network in front of it, privacy, integrations and the JSON documents. Backup & restore is at the bottom.',
    blocks: [
      config('general'),
      config('email'),
      config('network'),
      config('privacy'),
      config('integrations'),
      config('application'),
      panel('usage-insights'),
      { kind: 'unnamed-categories' },
      panel('backup'),
    ],
  },
];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = 'access';

/** The tab a `?tab=` value names, or the default for anything else. */
export function settingsTabKey(value: string | undefined | null): SettingsTabKey {
  return SETTINGS_TABS.find((tab) => tab.key === value)?.key ?? DEFAULT_SETTINGS_TAB;
}

/** The configuration categories a tab names, in its order. */
const NAMED_CATEGORIES: ReadonlyMap<string, SettingsTabKey> = new Map(
  SETTINGS_TABS.flatMap((tab) =>
    tab.blocks.flatMap((block) =>
      block.kind === 'config' ? [[block.category, tab.key] as const] : [],
    ),
  ),
);

/** The tab that lists the configuration categories no tab names. */
const UNNAMED_CATEGORIES_TAB: SettingsTabKey =
  SETTINGS_TABS.find((tab) => tab.blocks.some((block) => block.kind === 'unnamed-categories'))
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
      tab.blocks.some((block) => block.kind === 'runtime' && block.group === group),
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
