import { describe, expect, it } from 'vitest';

import { SETTINGS_TABS, runtimeGroupOf, settingsTabKey, tabOfCategory } from './settingsLayout';

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
      general: 'general',
      security: 'access',
      signup: 'access',
      email: 'access',
      phone: 'access',
      providers: 'providers',
      routing: 'providers',
      batch: 'batch',
      network: 'general',
      alerts: 'alerts',
      integrations: 'general',
      privacy: 'general',
    });
    expect(tabOfCategory('added-later')).toBe('general');
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
      user_concurrency_free: 'concurrency',
      user_concurrency_admin: 'concurrency',
      user_daily_quota_pro: 'quotas',
      added_later: 'other',
    });
  });

  it('reads a sub-tab from the address, and the default from anything else', () => {
    expect(settingsTabKey('limits')).toBe('limits');
    expect(settingsTabKey('nonsense')).toBe('general');
    expect(settingsTabKey(undefined)).toBe('general');
  });
});
