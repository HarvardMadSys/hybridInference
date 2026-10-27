/**
 * How Admin → Settings is organised: its sub-tabs, by topic, and what each
 * one holds. A topic's settings sit together whatever stores them: the
 * database-backed configuration (one section per category), the runtime
 * settings (in groups), and the sections with an API of their own.
 *
 * Anything the backend adds later still has a place: a configuration
 * category no sub-tab names, and a runtime setting no group takes, are listed
 * at the end of General.
 */

export type SettingsTabKey =
  | 'general'
  | 'access'
  | 'providers'
  | 'models'
  | 'limits'
  | 'batch'
  | 'alerts'
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
  /** The tab's sections, top to bottom. */
  blocks: readonly SettingsBlock[];
}

const config = (category: string): SettingsBlock => ({ kind: 'config', category });
const runtime = (group: RuntimeGroupKey): SettingsBlock => ({ kind: 'runtime', group });
const panel = (key: SettingsPanelKey): SettingsBlock => ({ kind: 'panel', panel: key });

export const SETTINGS_TABS: readonly SettingsTabDefinition[] = [
  {
    key: 'general',
    label: 'General',
    blocks: [
      config('general'),
      runtime('logging'),
      config('network'),
      config('privacy'),
      config('integrations'),
      panel('usage-insights'),
      { kind: 'unnamed-categories' },
      runtime('other'),
    ],
  },
  {
    key: 'access',
    label: 'Access',
    blocks: [
      runtime('access'),
      config('signup'),
      panel('signup-policy'),
      config('email'),
      config('phone'),
      config('security'),
    ],
  },
  {
    key: 'providers',
    label: 'Providers & Routing',
    // Added variables are listed under Providers, so the form that adds one is here.
    blocks: [panel('add-variable'), config('providers'), config('routing')],
  },
  {
    key: 'models',
    label: 'Models',
    blocks: [runtime('requests'), panel('model-visibility')],
  },
  {
    key: 'limits',
    label: 'Limits',
    blocks: [runtime('concurrency'), panel('model-concurrency'), runtime('quotas')],
  },
  {
    key: 'batch',
    label: 'Batch',
    blocks: [config('batch'), panel('batch-concurrency')],
  },
  {
    key: 'alerts',
    label: 'Alerts',
    blocks: [config('alerts'), panel('alert-snooze'), panel('alert-mutes')],
  },
  {
    key: 'backup',
    label: 'Backup',
    blocks: [panel('backup')],
  },
];

export const DEFAULT_SETTINGS_TAB: SettingsTabKey = 'general';

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

/** Whether a sub-tab names the category; one that none names is listed on General. */
export function isNamedCategory(category: string): boolean {
  return NAMED_CATEGORIES.has(category);
}

/** The sub-tab a configuration category is listed on. */
export function tabOfCategory(category: string): SettingsTabKey {
  return NAMED_CATEGORIES.get(category) ?? 'general';
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
