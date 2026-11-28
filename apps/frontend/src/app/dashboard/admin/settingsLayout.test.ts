import { describe, expect, it } from 'vitest';

import { sectionOf } from '@/components/features/configuration/configDrafts';

import {
  ADVANCED_SETTINGS,
  CATEGORIES_ELSEWHERE,
  DEFAULT_SETTINGS_TAB,
  MODEL_CATALOG_CATEGORY,
  SECTION_DESCRIPTIONS,
  SETTINGS_GROUPS,
  SETTINGS_SECTIONS,
  SETTINGS_TABS,
  TOPIC_SECTIONS,
  advancedSectionId,
  blocksOf,
  isNamedCategory,
  partSectionId,
  runtimeGroupOf,
  sectionOfBlock,
  settingsTabKey,
  tabOfRuntimeGroup,
  tabOfSection,
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

/** Where a setting is edited: its category's section, or the part that takes it. */
const sectionOfKey = (key: string, category: string) =>
  sectionOf({ key, category }, SETTINGS_SECTIONS);

describe('settingsLayout', () => {
  it('leads with an Overview, then one tab per topic under four headings', () => {
    expect(SETTINGS_TABS.map((candidate) => [candidate.key, candidate.label])).toEqual([
      ['overview', 'Overview'],
      ['signup', 'Sign-up'],
      ['security', 'Sign-in & security'],
      ['limits', 'Usage limits'],
      ['providers', 'Providers'],
      ['routing', 'Routing'],
      ['batches', 'Batches'],
      ['alerts', 'Alerts'],
      ['logging', 'Logging & privacy'],
      ['analytics', 'Analytics'],
      ['general', 'General'],
      ['email', 'Email'],
      ['network', 'Network'],
      ['integrations', 'Integrations'],
      ['backup', 'Backup & restore'],
      ['other', 'Other'],
    ]);
    expect(DEFAULT_SETTINGS_TAB).toBe('overview');
    expect(SETTINGS_GROUPS.map((group) => group.label)).toEqual([
      'Access',
      'Inference',
      'Monitoring',
      'Deployment',
    ]);
    // Only the Overview has no heading, and each heading's tabs are listed
    // together, so the navigation names each heading once.
    expect(SETTINGS_TABS.filter((candidate) => !candidate.group).map((t) => t.key)).toEqual([
      'overview',
    ]);
    const order = SETTINGS_TABS.flatMap((candidate) => (candidate.group ? [candidate.group] : []));
    expect(order.filter((group, index) => group !== order[index - 1])).toEqual(
      SETTINGS_GROUPS.map((group) => group.key),
    );
    for (const candidate of SETTINGS_TABS) {
      expect(candidate.description, candidate.key).not.toBe('');
      // A tab says what its Advanced holds exactly when it has one.
      expect(candidate.advancedDescription === '', candidate.key).toBe(
        candidate.advanced.length === 0,
      );
    }
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
      overview: { top: ['panel:overview'], advanced: [] },
      signup: {
        top: ['runtime:signup', 'panel:signup-policy', 'config:signup'],
        advanced: ['config:signup:advanced'],
      },
      security: {
        top: ['runtime:auth', 'config:security', 'config:phone'],
        advanced: ['config:security:advanced', 'config:phone:advanced', 'config:application:sso'],
      },
      limits: { top: ['runtime:concurrency', 'runtime:quotas'], advanced: [] },
      providers: {
        // The form that adds a variable follows Providers, where the variable is listed.
        top: ['config:providers', 'panel:add-variable'],
        advanced: ['config:providers:advanced'],
      },
      routing: {
        top: ['config:routing', 'runtime:requests'],
        advanced: [
          'config:routing:advanced',
          'panel:routewise',
          'config:application:routing-policy',
        ],
      },
      batches: {
        top: ['config:batch', 'panel:batch-concurrency'],
        advanced: ['config:batch:advanced'],
      },
      alerts: {
        // Snoozing first, for an incident; the long list of types last.
        top: ['panel:alert-snooze', 'config:alerts', 'panel:alert-mutes'],
        advanced: ['config:alerts:advanced', 'config:application:alert-rules'],
      },
      logging: {
        top: ['config:privacy', 'runtime:logging'],
        advanced: ['config:general:logging', 'config:privacy:advanced'],
      },
      analytics: { top: ['runtime:analytics', 'panel:usage-insights'], advanced: [] },
      general: {
        top: ['config:general'],
        advanced: [
          'config:general:advanced',
          'config:application:site-configuration',
          'config:application:branding',
          // The documents the layout does not place.
          'config:application',
        ],
      },
      email: { top: ['config:email'], advanced: [] },
      network: { top: ['config:network'], advanced: [] },
      integrations: { top: ['config:integrations'], advanced: [] },
      backup: { top: ['panel:backup'], advanced: [] },
      // A category no tab names, and a runtime setting no group takes.
      other: { top: ['unnamed-categories', 'runtime:other'], advanced: [] },
    });
  });

  it('places every configuration section once, and every part its layout makes', () => {
    const placed = SETTINGS_TABS.flatMap((candidate) =>
      blocksOf(candidate).flatMap((block) =>
        block.kind === 'config' ? [sectionOfBlock(block)] : [],
      ),
    );
    expect(new Set(placed).size).toBe(placed.length);
    // Each part is placed, and so is the rest of its category, where a setting
    // a later backend adds to it is listed.
    for (const section of SETTINGS_SECTIONS) {
      expect(placed, section.id).toContain(section.id);
      expect(placed, section.id).toContain(section.category);
    }
    // A category's tuning sits under the Advanced of a tab.
    for (const category of Object.keys(ADVANCED_SETTINGS)) {
      const owner = tab(tabOfSection(advancedSectionId(category), category));
      expect(owner.advanced, category).toContainEqual({
        kind: 'config',
        category,
        part: 'advanced',
      });
    }
  });

  it('names each setting once, by its variable, in sections with ids no category has', () => {
    const keys = [
      ...Object.values(ADVANCED_SETTINGS).flat(),
      ...TOPIC_SECTIONS.flatMap((section) => section.keys),
    ];
    expect(new Set(keys).size).toBe(keys.length);
    for (const key of keys) expect(key).toMatch(/^[A-Z][A-Z0-9_]*$/);

    const ids = SETTINGS_SECTIONS.map((section) => section.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual([
      ...Object.keys(ADVANCED_SETTINGS).map((category) => advancedSectionId(category)),
      ...TOPIC_SECTIONS.map((section) => partSectionId(section.category, section.part)),
    ]);
    for (const section of SETTINGS_SECTIONS) expect(section.id).not.toBe(section.category);
    // A category's tuning is named after it; a part of one after its topic.
    expect(SETTINGS_SECTIONS.find((s) => s.id === 'routing:advanced')?.label('Routing')).toBe(
      'Routing (advanced)',
    );
    const policy = SETTINGS_SECTIONS.find((s) => s.id === 'application:routing-policy');
    expect(policy?.label('Application')).toBe('Routing policy');
    expect(policy?.description).toMatch(/routing policy/);
  });

  it('describes a section in its own words only where it names one that is placed', () => {
    const placed = new Set(
      SETTINGS_TABS.flatMap((candidate) =>
        blocksOf(candidate).flatMap((block) =>
          block.kind === 'config' ? [sectionOfBlock(block)] : [],
        ),
      ),
    );
    for (const id of Object.keys(SECTION_DESCRIPTIONS)) expect(placed, id).toContain(id);
    // A part of a category carries its description into the editor.
    expect(SETTINGS_SECTIONS.find((s) => s.id === 'general:advanced')?.description).toBe(
      SECTION_DESCRIPTIONS['general:advanced'],
    );
  });

  it('keeps the switches of a split category at the top, and its numbers under Advanced', () => {
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
          ['SITE_DOCS_URL', 'general'],
          ['LOG_LEVEL', 'general'],
          ['DB_STORE_FULL_CONTENT', 'privacy'],
          ['ERASURE_FENCE_SECRET', 'privacy'],
          ['SSO_PROVIDERS', 'application'],
          ['ROUTING_POLICY', 'application'],
          ['ALERT_RULES', 'application'],
          ['SITE_CONFIGURATION', 'application'],
          ['SITE_BRANDING', 'application'],
          // An older backend keeps the catalog with the other documents.
          ['MODEL_CATALOG', 'application'],
        ].map(([key, category]) => [key, sectionOfKey(key, category)]),
      ),
    ).toEqual({
      ADMIN_EMAILS: 'security',
      AUTH_FAILURE_BLOCK_ENABLED: 'security',
      AUTH_FAILURE_BLOCK_THRESHOLD: 'security:advanced',
      JWT_SECRET_KEY: 'security:advanced',
      SIGNUP_NOTIFY_EMAILS: 'signup',
      SIGNUP_RATE_LIMIT_PER_HOUR: 'signup:advanced',
      SMS_AUTH_ENABLED: 'phone',
      SMS_OTP_MAX_ATTEMPTS: 'phone:advanced',
      LOCAL_API_KEY: 'providers',
      CHUTES_BASE_URL: 'providers:advanced',
      ENABLE_ROUTEWISE: 'routing',
      UPSTREAM_CONCURRENCY_ENABLED: 'routing',
      UPSTREAM_CONCURRENCY_MAX_LIMIT: 'routing:advanced',
      REQUEST_TIMEOUT_SECONDS: 'routing:advanced',
      ALERTS_ENABLED: 'alerts',
      FAILED_REQUEST_ALERT_THRESHOLD: 'alerts:advanced',
      BATCH_ENABLED: 'batch',
      BATCH_RETENTION_DAYS: 'batch',
      BATCH_MAX_REQUESTS: 'batch:advanced',
      SITE_NAME: 'general',
      SITE_DOCS_URL: 'general:advanced',
      LOG_LEVEL: 'general:logging',
      DB_STORE_FULL_CONTENT: 'privacy',
      ERASURE_FENCE_SECRET: 'privacy:advanced',
      SSO_PROVIDERS: 'application:sso',
      ROUTING_POLICY: 'application:routing-policy',
      ALERT_RULES: 'application:alert-rules',
      SITE_CONFIGURATION: 'application:site-configuration',
      SITE_BRANDING: 'application:branding',
      MODEL_CATALOG: 'application',
    });
    // The alert channel edits its three webhooks together, and Email is one
    // card: neither is ever split.
    for (const key of [
      'SLACK_ALERTS_WEBHOOK_URL',
      'SLACK_WEBHOOK_URL',
      'WECOM_ALERTS_WEBHOOK_URL',
    ]) {
      expect(sectionOfKey(key, 'alerts'), key).toBe('alerts');
    }
    expect(ADVANCED_SETTINGS.email).toBeUndefined();
    expect(TOPIC_SECTIONS.some((section) => section.category === 'email')).toBe(false);
  });

  it('shows at the top whatever it does not name', () => {
    // A setting a later backend adds to a split category, a provider key or a
    // single sign-on secret found in a document.
    expect(sectionOfKey('ROUTING_ADDED_LATER', 'routing')).toBe('routing');
    expect(sectionOfKey('ZAI_API_KEY', 'providers')).toBe('providers');
    expect(sectionOfKey('GOOGLE_SSO_SECRET', 'security')).toBe('security');
    // A category no tab names, and a runtime setting no group takes, on Other.
    expect(isNamedCategory('added-later')).toBe(false);
    expect(tabOfSection('added-later')).toBe('other');
    expect(runtimeGroupOf('added_later')).toBe('other');
    expect(tabOfRuntimeGroup('other')).toBe('other');
  });

  it('lists every section the backend has today on the tab of its topic', () => {
    const sections: [string, string][] = [
      ['application', 'application'],
      ['application:sso', 'application'],
      ['application:routing-policy', 'application'],
      ['application:alert-rules', 'application'],
      ['application:site-configuration', 'application'],
      ['application:branding', 'application'],
      ['general', 'general'],
      ['general:advanced', 'general'],
      ['general:logging', 'general'],
      ['security', 'security'],
      ['security:advanced', 'security'],
      ['signup', 'signup'],
      ['signup:advanced', 'signup'],
      ['email', 'email'],
      ['phone', 'phone'],
      ['phone:advanced', 'phone'],
      ['providers', 'providers'],
      ['providers:advanced', 'providers'],
      ['routing', 'routing'],
      ['routing:advanced', 'routing'],
      ['batch', 'batch'],
      ['batch:advanced', 'batch'],
      ['network', 'network'],
      ['alerts', 'alerts'],
      ['alerts:advanced', 'alerts'],
      ['integrations', 'integrations'],
      ['privacy', 'privacy'],
      ['privacy:advanced', 'privacy'],
    ];
    expect(
      Object.fromEntries(sections.map(([id, category]) => [id, tabOfSection(id, category)])),
    ).toEqual({
      application: 'general',
      'application:sso': 'security',
      'application:routing-policy': 'routing',
      'application:alert-rules': 'alerts',
      'application:site-configuration': 'general',
      'application:branding': 'general',
      general: 'general',
      'general:advanced': 'general',
      'general:logging': 'logging',
      security: 'security',
      'security:advanced': 'security',
      signup: 'signup',
      'signup:advanced': 'signup',
      email: 'email',
      phone: 'security',
      'phone:advanced': 'security',
      providers: 'providers',
      'providers:advanced': 'providers',
      routing: 'routing',
      'routing:advanced': 'routing',
      batch: 'batches',
      'batch:advanced': 'batches',
      network: 'network',
      alerts: 'alerts',
      'alerts:advanced': 'alerts',
      integrations: 'integrations',
      privacy: 'logging',
      'privacy:advanced': 'logging',
    });
    // A part no tab places is listed with the rest of its category.
    expect(tabOfSection('routing:added-later', 'routing')).toBe('routing');
  });

  it('keeps every runtime group on the tab of its topic', () => {
    const expected: Record<RuntimeGroupKey, string> = {
      signup: 'signup',
      auth: 'security',
      concurrency: 'limits',
      quotas: 'limits',
      requests: 'routing',
      logging: 'logging',
      analytics: 'analytics',
      other: 'other',
    };
    for (const [group, key] of Object.entries(expected)) {
      expect(tabOfRuntimeGroup(group as RuntimeGroupKey), group).toBe(key);
    }
    // Each is placed once, at the top of its tab: runtime switches are what
    // an administrator changes.
    const runtimeBlocks = SETTINGS_TABS.flatMap((candidate) =>
      (['blocks', 'advanced'] as const).flatMap((place) =>
        candidate[place].flatMap((block) =>
          block.kind === 'runtime' ? [[block.group, place]] : [],
        ),
      ),
    );
    expect(runtimeBlocks.map(([group]) => group).sort()).toEqual(Object.keys(expected).sort());
    expect(runtimeBlocks.every(([, place]) => place === 'blocks')).toBe(true);
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
    expect(TOPIC_SECTIONS.some((section) => section.category === MODEL_CATALOG_CATEGORY)).toBe(
      false,
    );
  });

  it('puts the RouteWise panel and the routing policy under the Advanced of Routing', () => {
    const routing = tab('routing');
    expect(routing.advanced).toContainEqual({ kind: 'panel', panel: 'routewise' });
    expect(routing.advanced).toContainEqual({
      kind: 'config',
      category: 'routing',
      part: 'advanced',
    });
    expect(routing.advanced).toContainEqual({
      kind: 'config',
      category: 'application',
      part: 'routing-policy',
    });
    expect(routing.blocks).toContainEqual({ kind: 'config', category: 'routing' });
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
      user_auth_enabled: 'auth',
      signup_enabled: 'signup',
      signup_require_email_verification: 'signup',
      signup_admin_notify_enabled: 'signup',
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
    expect(settingsTabKey('email')).toBe('email');
    expect(settingsTabKey('logging')).toBe('logging');
    // The tabs of the layout before this one are gone: the Overview points on.
    expect(settingsTabKey('setup')).toBe('overview');
    expect(settingsTabKey('operations')).toBe('overview');
    expect(settingsTabKey('nonsense')).toBe('overview');
    expect(settingsTabKey(undefined)).toBe('overview');
  });
});
