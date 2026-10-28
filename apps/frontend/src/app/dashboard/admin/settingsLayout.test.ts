import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SETTINGS_TAB,
  SETTINGS_TABS,
  runtimeGroupOf,
  settingsTabKey,
  tabOfCategory,
  type RuntimeGroupKey,
} from './settingsLayout';

describe('settingsLayout', () => {
  it('names each configuration category on one sub-tab only', () => {
    const named = SETTINGS_TABS.flatMap((tab) =>
      tab.blocks.flatMap((block) => (block.kind === 'config' ? [block.category] : [])),
    );
    expect(new Set(named).size).toBe(named.length);
  });

  it('lists every category the backend has today on the sub-tab of its topic', () => {
    expect(
      Object.fromEntries(
        [
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
      general: 'setup',
      security: 'access',
      signup: 'access',
      email: 'setup',
      phone: 'access',
      providers: 'providers',
      routing: 'providers',
      batch: 'batch',
      network: 'setup',
      alerts: 'alerts',
      integrations: 'setup',
      privacy: 'setup',
    });
    expect(tabOfCategory('added-later')).toBe('setup');
  });

  it('opens on Common, which holds every runtime setting, and keeps Setup and Backup last', () => {
    expect(SETTINGS_TABS[0].key).toBe('common');
    expect(DEFAULT_SETTINGS_TAB).toBe('common');
    expect(SETTINGS_TABS.slice(-2).map((tab) => tab.key)).toEqual(['setup', 'backup']);

    const groups: RuntimeGroupKey[] = [
      'access',
      'concurrency',
      'quotas',
      'requests',
      'logging',
      'analytics',
      'other',
    ];
    const runtimeBlocks = SETTINGS_TABS.flatMap((tab) =>
      tab.blocks.flatMap((block) => (block.kind === 'runtime' ? [[tab.key, block.group]] : [])),
    );
    expect(runtimeBlocks).toEqual(groups.map((group) => ['common', group]));
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

  it('reads a sub-tab from the address, and the default from anything else', () => {
    expect(settingsTabKey('models')).toBe('models');
    expect(settingsTabKey('setup')).toBe('setup');
    expect(settingsTabKey('nonsense')).toBe('common');
    expect(settingsTabKey(undefined)).toBe('common');
  });
});
