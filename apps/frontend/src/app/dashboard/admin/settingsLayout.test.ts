import { describe, expect, it } from 'vitest';

import { sectionOf } from '@/components/features/configuration/configDrafts';

import {
  ADVANCED_SECTIONS,
  ADVANCED_SETTINGS,
  CATEGORIES_ELSEWHERE,
  DEFAULT_SETTINGS_TAB,
  MODEL_CATALOG_CATEGORY,
  SETTINGS_TABS,
  advancedSectionId,
  blocksOf,
  runtimeGroupOf,
  sectionOfBlock,
  settingsTabKey,
  tabOfCategory,
  tabOfRuntimeGroup,
  type RuntimeGroupKey,
  type SettingsBlock,
  type SettingsTabDefinition,
} from './settingsLayout';

/** A block in words: `config:routing`, `config:routing:advanced`, `runtime:logging`, `panel:backup`. */
function describeBlock(block: SettingsBlock): string {
  switch (block.kind) {
    case 'config':
      return `config:${sectionOfBlock(block)}`;
    case 'unnamed-categories':
      return 'unnamed-categories';
    case 'runtime':
      return `runtime:${block.group}`;
    case 'panel':
      return `panel:${block.panel}`;
  }
}

function tab(key: string): SettingsTabDefinition {
  const found = SETTINGS_TABS.find((candidate) => candidate.key === key);
  if (!found) throw new Error(`no tab ${key}`);
  return found;
}

describe('settingsLayout', () => {
  it('names each configuration category on one tab only, and each of its sections once', () => {
    const tabOf = new Map<string, string>();
    for (const candidate of SETTINGS_TABS) {
      for (const block of blocksOf(candidate)) {
        if (block.kind !== 'config') continue;
        expect(tabOf.get(block.category) ?? candidate.key, block.category).toBe(candidate.key);
        tabOf.set(block.category, candidate.key);
      }
    }
    const sections = SETTINGS_TABS.flatMap((candidate) =>
      blocksOf(candidate).flatMap((block) =>
        block.kind === 'config' ? [describeBlock(block)] : [],
      ),
    );
    expect(new Set(sections).size).toBe(sections.length);
  });

  it('has four task tabs, each holding its whole topic', () => {
    expect(SETTINGS_TABS.map((candidate) => candidate.key)).toEqual([
      'access',
      'providers',
      'operations',
      'setup',
    ]);
    expect(SETTINGS_TABS.map((candidate) => candidate.label)).toEqual([
      'Access & Limits',
      'Providers & Routing',
      'Operations',
      'Site & Setup',
    ]);
    expect(DEFAULT_SETTINGS_TAB).toBe('access');
  });

  it('opens each tab on what is commonly changed, and leaves the tuning to Advanced', () => {
    expect(
      Object.fromEntries(
        SETTINGS_TABS.map((candidate) => [
          candidate.key,
          {
            top: candidate.blocks.map(describeBlock),
            advanced: candidate.advanced.map(describeBlock),
          },
        ]),
      ),
    ).toEqual({
      access: {
        top: [
          'runtime:access',
          'runtime:concurrency',
          'runtime:quotas',
          'panel:signup-policy',
          'config:signup',
          'config:security',
          'config:phone',
        ],
        advanced: ['config:signup:advanced', 'config:security:advanced', 'config:phone:advanced'],
      },
      providers: {
        // The form that adds a variable follows Providers, where the variable is listed.
        top: ['config:providers', 'panel:add-variable', 'config:routing', 'runtime:requests'],
        advanced: ['config:providers:advanced', 'config:routing:advanced', 'panel:routewise'],
      },
      operations: {
        top: [
          'panel:alert-snooze',
          'panel:alert-mutes',
          'config:alerts',
          'config:batch',
          'panel:batch-concurrency',
          'runtime:other',
        ],
        advanced: [
          'config:alerts:advanced',
          'config:batch:advanced',
          'runtime:logging',
          'runtime:analytics',
        ],
      },
      setup: {
        top: [
          'config:general',
          'config:email',
          'config:privacy',
          'unnamed-categories',
          'panel:backup',
        ],
        // The JSON documents and the categories seldom touched are under Advanced whole.
        advanced: [
          'config:general:advanced',
          'config:network',
          'config:privacy:advanced',
          'config:integrations',
          'config:application',
          'panel:usage-insights',
        ],
      },
    });
    for (const candidate of SETTINGS_TABS) {
      expect(candidate.advancedDescription, candidate.key).not.toBe('');
    }
  });

  it('splits a category only where ADVANCED_SETTINGS names its tuning, on one tab', () => {
    const split = Object.keys(ADVANCED_SETTINGS);
    for (const candidate of SETTINGS_TABS) {
      for (const block of blocksOf(candidate)) {
        if (block.kind !== 'config' || block.part !== 'advanced') continue;
        // An advanced part names its settings, sits under Advanced, and the rest
        // of its category at the top of the same tab.
        expect(split, block.category).toContain(block.category);
        expect(candidate.advanced, block.category).toContainEqual(block);
        expect(candidate.blocks, block.category).toContainEqual({
          kind: 'config',
          category: block.category,
        });
      }
    }
    for (const category of split) {
      const owner = tab(tabOfCategory(category));
      expect(owner.advanced, category).toContainEqual({
        kind: 'config',
        category,
        part: 'advanced',
      });
    }
    // A setting is named once, by its environment variable.
    const keys = Object.values(ADVANCED_SETTINGS).flat();
    expect(new Set(keys).size).toBe(keys.length);
    for (const key of keys) expect(key).toMatch(/^[A-Z][A-Z0-9_]*$/);
    // The editor takes each as a section of its own, with an id no category has.
    expect(ADVANCED_SECTIONS.map((section) => section.id)).toEqual(
      split.map((category) => advancedSectionId(category)),
    );
    for (const section of ADVANCED_SECTIONS) {
      expect(section.id).not.toBe(section.category);
      expect(section.label('Routing')).toBe('Routing (advanced)');
    }
  });

  it('keeps the switches of a split category at the top, and its numbers under Advanced', () => {
    const at = (key: string, category: string) =>
      sectionOf({ key, category }, ADVANCED_SECTIONS) === category ? 'top' : 'advanced';
    expect(
      Object.fromEntries(
        [
          ['ADMIN_EMAILS', 'security'],
          ['AUTH_FAILURE_BLOCK_ENABLED', 'security'],
          ['AUTH_FAILURE_BLOCK_THRESHOLD', 'security'],
          ['JWT_SECRET_KEY', 'security'],
          ['SIGNUP_NOTIFY_EMAILS', 'signup'],
          ['SIGNUP_RATE_LIMIT_PER_HOUR', 'signup'],
          ['SMS_AUTH_ENABLED', 'phone'],
          ['SMS_OTP_MAX_ATTEMPTS', 'phone'],
          ['LOCAL_API_KEY', 'providers'],
          ['CHUTES_BASE_URL', 'providers'],
          ['ENABLE_ROUTEWISE', 'routing'],
          ['UPSTREAM_CONCURRENCY_ENABLED', 'routing'],
          ['UPSTREAM_CONCURRENCY_MAX_LIMIT', 'routing'],
          ['REQUEST_TIMEOUT_SECONDS', 'routing'],
          ['ALERTS_ENABLED', 'alerts'],
          ['FAILED_REQUEST_ALERT_THRESHOLD', 'alerts'],
          ['BATCH_ENABLED', 'batch'],
          ['BATCH_RETENTION_DAYS', 'batch'],
          ['BATCH_MAX_REQUESTS', 'batch'],
          ['SITE_NAME', 'general'],
          ['LOG_LEVEL', 'general'],
          ['DB_STORE_FULL_CONTENT', 'privacy'],
          ['ERASURE_FENCE_SECRET', 'privacy'],
        ].map(([key, category]) => [key, at(key, category)]),
      ),
    ).toEqual({
      ADMIN_EMAILS: 'top',
      AUTH_FAILURE_BLOCK_ENABLED: 'top',
      AUTH_FAILURE_BLOCK_THRESHOLD: 'advanced',
      JWT_SECRET_KEY: 'advanced',
      SIGNUP_NOTIFY_EMAILS: 'top',
      SIGNUP_RATE_LIMIT_PER_HOUR: 'advanced',
      SMS_AUTH_ENABLED: 'top',
      SMS_OTP_MAX_ATTEMPTS: 'advanced',
      LOCAL_API_KEY: 'top',
      CHUTES_BASE_URL: 'advanced',
      ENABLE_ROUTEWISE: 'top',
      UPSTREAM_CONCURRENCY_ENABLED: 'top',
      UPSTREAM_CONCURRENCY_MAX_LIMIT: 'advanced',
      REQUEST_TIMEOUT_SECONDS: 'advanced',
      ALERTS_ENABLED: 'top',
      FAILED_REQUEST_ALERT_THRESHOLD: 'advanced',
      BATCH_ENABLED: 'top',
      BATCH_RETENTION_DAYS: 'top',
      BATCH_MAX_REQUESTS: 'advanced',
      SITE_NAME: 'top',
      LOG_LEVEL: 'advanced',
      DB_STORE_FULL_CONTENT: 'top',
      ERASURE_FENCE_SECRET: 'advanced',
    });
    // The alert channel edits its three webhooks together, and Email is one
    // card: neither is ever split.
    for (const key of [
      'SLACK_ALERTS_WEBHOOK_URL',
      'SLACK_WEBHOOK_URL',
      'WECOM_ALERTS_WEBHOOK_URL',
    ]) {
      expect(at(key, 'alerts'), key).toBe('top');
    }
    expect(ADVANCED_SETTINGS.email).toBeUndefined();
  });

  it('shows at the top whatever it does not name', () => {
    // A setting a later backend adds to a split category, a provider key or a
    // single sign-on secret found in a document.
    expect(sectionOf({ key: 'ROUTING_ADDED_LATER', category: 'routing' }, ADVANCED_SECTIONS)).toBe(
      'routing',
    );
    expect(sectionOf({ key: 'ZAI_API_KEY', category: 'providers' }, ADVANCED_SECTIONS)).toBe(
      'providers',
    );
    expect(sectionOf({ key: 'GOOGLE_SSO_SECRET', category: 'security' }, ADVANCED_SECTIONS)).toBe(
      'security',
    );
    // A category no tab names, and a runtime setting no group takes.
    expect(tab('setup').blocks).toContainEqual({ kind: 'unnamed-categories' });
    expect(tab('operations').blocks).toContainEqual({ kind: 'runtime', group: 'other' });
    expect(runtimeGroupOf('added_later')).toBe('other');
  });

  it('lists every category the backend has today on the tab of its topic', () => {
    expect(
      Object.fromEntries(
        [
          'application',
          'general',
          'security',
          'signup',
          'email',
          'phone',
          'providers',
          'routing',
          'batch',
          'network',
          'alerts',
          'integrations',
          'privacy',
        ].map((category) => [category, tabOfCategory(category)]),
      ),
    ).toEqual({
      application: 'setup',
      general: 'setup',
      security: 'access',
      signup: 'access',
      email: 'setup',
      phone: 'access',
      providers: 'providers',
      routing: 'providers',
      batch: 'operations',
      network: 'setup',
      alerts: 'operations',
      integrations: 'setup',
      privacy: 'setup',
    });
    expect(tabOfCategory('added-later')).toBe('setup');
  });

  it('keeps every runtime group on the tab of its topic, with no Common tab', () => {
    const expected: Record<RuntimeGroupKey, string> = {
      access: 'access',
      concurrency: 'access',
      quotas: 'access',
      requests: 'providers',
      logging: 'operations',
      analytics: 'operations',
      other: 'operations',
    };
    for (const [group, key] of Object.entries(expected)) {
      expect(tabOfRuntimeGroup(group as RuntimeGroupKey), group).toBe(key);
    }
    const runtimeBlocks = SETTINGS_TABS.flatMap((candidate) =>
      (['blocks', 'advanced'] as const).flatMap((place) =>
        candidate[place].flatMap((block) =>
          block.kind === 'runtime' ? [[candidate.key, block.group, place]] : [],
        ),
      ),
    );
    expect(runtimeBlocks).toEqual([
      ['access', 'access', 'blocks'],
      ['access', 'concurrency', 'blocks'],
      ['access', 'quotas', 'blocks'],
      ['providers', 'requests', 'blocks'],
      ['operations', 'other', 'blocks'],
      // What is logged and which charts show: detail, under Advanced.
      ['operations', 'logging', 'advanced'],
      ['operations', 'analytics', 'advanced'],
    ]);
  });

  it('leaves the model catalog to Routing → Models, and names it on no tab', () => {
    expect(MODEL_CATALOG_CATEGORY).toBe('models');
    expect([...CATEGORIES_ELSEWHERE]).toEqual(['models']);
    expect(SETTINGS_TABS.map((candidate) => candidate.key)).not.toContain('models');
    const named = SETTINGS_TABS.flatMap((candidate) =>
      blocksOf(candidate).flatMap((block) => (block.kind === 'config' ? [block.category] : [])),
    );
    expect(named).not.toContain(MODEL_CATALOG_CATEGORY);
    expect(ADVANCED_SETTINGS[MODEL_CATALOG_CATEGORY]).toBeUndefined();
  });

  it('puts the RouteWise panel under the Advanced of Providers & Routing, with the routing tuning', () => {
    const providers = tab('providers');
    expect(providers.advanced).toContainEqual({ kind: 'panel', panel: 'routewise' });
    expect(providers.advanced).toContainEqual({
      kind: 'config',
      category: 'routing',
      part: 'advanced',
    });
    expect(providers.blocks).toContainEqual({ kind: 'config', category: 'routing' });
  });

  it('puts each runtime setting in the group of its topic', () => {
    expect(
      Object.fromEntries(
        [
          'user_auth_enabled',
          'signup_enabled',
          'signup_require_email_verification',
          'signup_admin_notify_enabled',
          'log_full_payload',
          'log_rejected_requests',
          'log_synthetic_probes',
          'force_chat_completions_streaming',
          'reasoning_small_call_reroute_enabled',
          'auto_mode_enabled',
          'analytics_token_usage_enabled',
          'analytics_users_and_requests_enabled',
          'analytics_performance_metrics_enabled',
          'analytics_growth_rate_enabled',
          'user_concurrency_free',
          'user_concurrency_admin',
          'user_daily_quota_pro',
          'added_later',
        ].map((key) => [key, runtimeGroupOf(key)]),
      ),
    ).toEqual({
      user_auth_enabled: 'access',
      signup_enabled: 'access',
      signup_require_email_verification: 'access',
      signup_admin_notify_enabled: 'access',
      log_full_payload: 'logging',
      log_rejected_requests: 'logging',
      log_synthetic_probes: 'logging',
      force_chat_completions_streaming: 'requests',
      reasoning_small_call_reroute_enabled: 'requests',
      auto_mode_enabled: 'requests',
      analytics_token_usage_enabled: 'analytics',
      analytics_users_and_requests_enabled: 'analytics',
      analytics_performance_metrics_enabled: 'analytics',
      analytics_growth_rate_enabled: 'analytics',
      user_concurrency_free: 'concurrency',
      user_concurrency_admin: 'concurrency',
      user_daily_quota_pro: 'quotas',
      added_later: 'other',
    });
  });

  it('reads a tab from the address, and the default from anything else', () => {
    expect(settingsTabKey('setup')).toBe('setup');
    expect(settingsTabKey('operations')).toBe('operations');
    expect(settingsTabKey('nonsense')).toBe('access');
    expect(settingsTabKey(undefined)).toBe('access');
  });
});
