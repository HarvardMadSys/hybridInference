/**
 * How Admin → Settings is organised: its sub-tabs and what each one holds.
 *
 * Common comes first, and opens by default: the settings changed most often,
 * the runtime switches and limits the Settings tab held before the
 * configuration moved in. The sub-tabs of a topic follow, each with that
 * topic's settings together whatever stores them: the database-backed
 * configuration (one section per category) and the sections with an API of
 * their own. Setup, near the end, holds what is usually set once, when the
 * gateway is deployed; and within any category the configuration lists such
 * settings after the others (`ConfigEntry.one_time`).
 *
 * Anything the backend adds later still has a place: a runtime setting no
 * group takes is listed at the end of Common, and a configuration category no
 * sub-tab names at the end of Setup.
 */

export type SettingsTabKey =
  | 'common'
  | 'access'
  | 'providers'
  | 'models'
  | 'batch'
  | 'alerts'
  | 'setup'
  | 'backup';

/** A group of runtime settings, edited together with one Save. */
export type RuntimeGroupKey =
  | 'logging'
  | 'access'
  | 'requests'
  | 'concurrency'
  | 'quotas'
  | 'other';

/** A section with its own API, or one the page composes. */
export type SettingsPanelKey =
  | 'usage-insights'
  | 'signup-policy'
  | 'model-visibility'
  | 'model-concurrency'
  | 'batch-concurrency'
  | 'alert-snooze'
  | 'alert-mutes'
  | 'add-variable'
  | 'backup';

export type SettingsBlock =
  | { kind: 'config'; category: string }
  /** The configuration categories no sub-tab names. */
  | { kind: 'unnamed-categories' }
  | { kind: 'runtime'; group: RuntimeGroupKey }
  | { kind: 'panel'; panel: SettingsPanelKey };

export interface SettingsTabDefinition {
  key: SettingsTabKey;
  label: string;
  /** What the sub-tab holds, above its sections, where its name alone does not say. */
  description?: string;
  /** The tab's sections, top to bottom. */
  blocks: readonly SettingsBlock[];
}

const config = (category: string): SettingsBlock => ({ kind: 'config', category });
const runtime = (group: RuntimeGroupKey): SettingsBlock => ({ kind: 'runtime', group });
const panel = (key: SettingsPanelKey): SettingsBlock => ({ kind: 'panel', panel: key });

export const SETTINGS_TABS: readonly SettingsTabDefinition[] = [
  {
    key: 'common',
    label: 'Common',
    description:
      'The settings changed most often. The other sub-tabs hold the rest by topic, and Setup what is usually set once.',
    blocks: [
      runtime('access'),
      runtime('concurrency'),
      runtime('quotas'),
      runtime('requests'),
      runtime('logging'),
      runtime('other'),
    ],
  },
  {
    key: 'access',
    label: 'Access',
    blocks: [panel('signup-policy'), config('signup'), config('security'), config('phone')],
  },
  {
    key: 'providers',
    label: 'Providers & Routing',
    // Added variables are listed under Providers, so the form that adds one follows it.
    blocks: [config('providers'), panel('add-variable'), config('routing')],
  },
  {
    key: 'models',
    label: 'Models',
    blocks: [panel('model-visibility'), panel('model-concurrency')],
  },
  {
    key: 'batch',
    label: 'Batch',
    blocks: [config('batch'), panel('batch-concurrency')],
  },
  {
    key: 'alerts',
    label: 'Alerts',
    // Muting is what an incident calls for; the channel and thresholds are set beforehand.
    blocks: [panel('alert-snooze'), panel('alert-mutes'), config('alerts')],
  },
  {
    key: 'setup',
    label: 'Setup',
    description:
      'Settings usually set once, when the gateway is deployed: its addresses, name and logging, outgoing mail, the network in front of it, privacy and integrations.',
    blocks: [
      config('general'),
      config('email'),
      config('network'),
      config('privacy'),
      config('integrations'),
      panel('usage-insights'),
      { kind: 'unnamed-categories' },
    ],
  },
  {
    key: 'backup',
    label: 'Backup',
    blocks: [panel('backup')],
  },
];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = 'common';

/** The sub-tab a `?tab=` value names, or the default for anything else. */
export function settingsTabKey(value: string | undefined | null): SettingsTabKey {
  return SETTINGS_TABS.find((tab) => tab.key === value)?.key ?? DEFAULT_SETTINGS_TAB;
}

/** The configuration categories a sub-tab names, in its order. */
const NAMED_CATEGORIES: ReadonlyMap<string, SettingsTabKey> = new Map(
  SETTINGS_TABS.flatMap((tab) =>
    tab.blocks.flatMap((block) =>
      block.kind === 'config' ? [[block.category, tab.key] as const] : [],
    ),
  ),
);

/** The sub-tab that lists the configuration categories no sub-tab names. */
const UNNAMED_CATEGORIES_TAB: SettingsTabKey =
  SETTINGS_TABS.find((tab) => tab.blocks.some((block) => block.kind === 'unnamed-categories'))
    ?.key ?? DEFAULT_SETTINGS_TAB;

/** Whether a sub-tab names the category; one that none names is listed on Setup. */
export function isNamedCategory(category: string): boolean {
  return NAMED_CATEGORIES.has(category);
}

/** The sub-tab a configuration category is listed on. */
export function tabOfCategory(category: string): SettingsTabKey {
  return NAMED_CATEGORIES.get(category) ?? UNNAMED_CATEGORIES_TAB;
}

export const RUNTIME_GROUPS: Readonly<
  Record<RuntimeGroupKey, { title: string; description: string }>
> = {
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
  if (key.startsWith('log_')) return 'logging';
  if (key === 'user_auth_enabled' || key.startsWith('signup_')) return 'access';
  if (key.startsWith('user_concurrency_')) return 'concurrency';
  if (key.startsWith('user_daily_quota_')) return 'quotas';
  if (REQUEST_HANDLING_SETTINGS.has(key)) return 'requests';
  return 'other';
}
