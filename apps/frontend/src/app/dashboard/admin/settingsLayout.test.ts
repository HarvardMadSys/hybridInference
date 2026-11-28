import { describe, expect, it } from 'vitest';

import {
  CATEGORIES_ELSEWHERE,
  DEFAULT_SETTINGS_TAB,
  MODEL_CATALOG_CATEGORY,
  SETTINGS_TABS,
  runtimeGroupOf,
  settingsTabKey,
  tabOfCategory,
  tabOfRuntimeGroup,
  type RuntimeGroupKey,
} from './settingsLayout';

describe('settingsLayout', () => {
  it('names each configuration category on one tab only', () => {
    const named = SETTINGS_TABS.flatMap((tab) =>
      tab.blocks.flatMap((block) => (block.kind === 'config' ? [block.category] : [])),
    );
    expect(new Set(named).size).toBe(named.length);
  });

  it('has four task tabs, each holding its whole topic', () => {
    expect(SETTINGS_TABS.map((tab) => tab.key)).toEqual([
      'access',
      'providers',
      'operations',
      'setup',
    ]);
    expect(SETTINGS_TABS.map((tab) => tab.label)).toEqual([
      'Access & Limits',
      'Providers & Routing',
      'Operations',
      'Site & Setup',
    ]);
    expect(DEFAULT_SETTINGS_TAB).toBe('access');
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
    for (const [group, tab] of Object.entries(expected)) {
      expect(tabOfRuntimeGroup(group as RuntimeGroupKey), group).toBe(tab);
    }
    const runtimeBlocks = SETTINGS_TABS.flatMap((tab) =>
      tab.blocks.flatMap((block) => (block.kind === 'runtime' ? [[tab.key, block.group]] : [])),
    );
    expect(runtimeBlocks).toEqual([
      ['access', 'access'],
      ['access', 'concurrency'],
      ['access', 'quotas'],
      ['providers', 'requests'],
      ['operations', 'logging'],
      ['operations', 'analytics'],
      ['operations', 'other'],
    ]);
  });

  it('leaves the model catalog to Routing → Models, and names it on no tab', () => {
    expect(MODEL_CATALOG_CATEGORY).toBe('models');
    expect([...CATEGORIES_ELSEWHERE]).toEqual(['models']);
    expect(SETTINGS_TABS.map((tab) => tab.key)).not.toContain('models');
    const named = SETTINGS_TABS.flatMap((tab) =>
      tab.blocks.flatMap((block) => (block.kind === 'config' ? [block.category] : [])),
    );
    expect(named).not.toContain(MODEL_CATALOG_CATEGORY);
  });

  it('puts the RouteWise panel on Providers & Routing, with its routing config', () => {
    const providers = SETTINGS_TABS.find((tab) => tab.key === 'providers');
    expect(providers?.blocks).toContainEqual({ kind: 'panel', panel: 'routewise' });
    expect(providers?.blocks).toContainEqual({ kind: 'config', category: 'routing' });
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
