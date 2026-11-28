import { describe, expect, it } from 'vitest';

import type { RuntimeSettingItem } from '@/lib/api/admin';
import type { ConfigEntry } from '@/lib/api/config';

import { overviewItems, type OverviewItem } from './settingsOverview';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'general',
    description: '',
    type: 'str',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: '',
    default: null,
    source: 'database',
    restart_required: false,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: false,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

function flag(key: string, value: boolean): RuntimeSettingItem {
  return { key, value, value_type: 'bool', default_value: value, description: key };
}

const webhook = (key: string, set: boolean) =>
  entry({ key, category: 'alerts', secret: true, is_set: set, value: null });

/** A gateway set up as the tutorial leaves one: email not yet configured. */
const ENTRIES: ConfigEntry[] = [
  entry({ key: 'SITE_NAME', value: 'Example Router' }),
  entry({ key: 'BASE_URL', value: 'https://gateway.example.test' }),
  entry({ key: 'FRONTEND_URL', value: 'https://console.example.test' }),
  entry({
    key: 'EMAIL_PROVIDER',
    category: 'email',
    value: 'smtp',
    choices: ['smtp', 'cloudflare'],
  }),
  entry({ key: 'SMTP_HOST', category: 'email', is_set: false, value: null }),
  entry({ key: 'SMTP_USER', category: 'email', is_set: false, value: null }),
  entry({ key: 'SMTP_PASSWORD', category: 'email', secret: true, is_set: false, value: null }),
  entry({ key: 'ALERTS_ENABLED', category: 'alerts', type: 'bool', value: true }),
  webhook('SLACK_ALERTS_WEBHOOK_URL', false),
  webhook('SLACK_WEBHOOK_URL', false),
  webhook('WECOM_ALERTS_WEBHOOK_URL', true),
];

const RUNTIME = [
  flag('signup_enabled', true),
  flag('signup_require_email_verification', false),
  flag('user_auth_enabled', true),
];

/** Each line as `id: tone status → tab`. */
function summary(items: OverviewItem[]) {
  return items.map((item) => `${item.id}: ${item.tone} ${item.status} → ${item.tab}`);
}

describe('overviewItems', () => {
  it('says how each thing set up once stands, and where it is changed', () => {
    const items = overviewItems(ENTRIES, RUNTIME);
    expect(summary(items)).toEqual([
      'site: ok Set → general',
      'email: attention Not set up → email',
      'signup: ok Open → signup',
      'api-access: ok Key required → security',
      'alerts: ok On → alerts',
    ]);
    expect(items[0].detail).toBe('Example Router, at https://console.example.test.');
    expect(items[1].detail).toBe(
      'SMTP server still needs SMTP host, Username and Password. Until it works, users cannot reset a forgotten password themselves.',
    );
    expect(items[2].detail).toBe('New users may sign up, without verifying their email.');
    expect(items[4]).toMatchObject({ detail: 'Alerts are sent to WeCom.', hash: 'config-alerts' });
  });

  it('warns when sign-up needs email verification that cannot be sent', () => {
    const [, , signup] = overviewItems(ENTRIES, [
      flag('signup_enabled', true),
      flag('signup_require_email_verification', true),
    ]);
    expect(signup).toMatchObject({ id: 'signup', tone: 'attention', status: 'Open' });
    expect(signup.detail).toMatch(/cannot finish signing up/);
  });

  it('says what is open, closed or off', () => {
    const entries = ENTRIES.map((item) =>
      item.key === 'BASE_URL'
        ? { ...item, value: '' }
        : item.key === 'ALERTS_ENABLED'
          ? { ...item, value: false }
          : item,
    );
    expect(
      summary(
        overviewItems(entries, [flag('signup_enabled', false), flag('user_auth_enabled', false)]),
      ),
    ).toEqual([
      'site: attention Not set → general',
      'email: attention Not set up → email',
      'signup: off Closed → signup',
      'api-access: attention Open → security',
      'alerts: off Off → alerts',
    ]);
  });

  it('says email is ready once its transport has what it needs', () => {
    const ready = ENTRIES.map((item) =>
      item.category === 'email' && item.key !== 'EMAIL_PROVIDER'
        ? { ...item, is_set: true, value: item.secret ? null : 'set' }
        : item,
    );
    const email = overviewItems(ready, RUNTIME).find((item) => item.id === 'email');
    expect(email).toMatchObject({
      tone: 'ok',
      status: 'Ready',
      detail: 'Mail is sent through SMTP server.',
    });
  });

  it('asks for a channel while alerts run without one', () => {
    const entries = ENTRIES.map((item) =>
      item.key === 'WECOM_ALERTS_WEBHOOK_URL' ? { ...item, is_set: false } : item,
    );
    const alerts = overviewItems(entries, RUNTIME).find((item) => item.id === 'alerts');
    expect(alerts).toMatchObject({ tone: 'attention', status: 'No channel' });
  });

  it('leaves out what the gateway does not report', () => {
    expect(overviewItems(null, [])).toEqual([]);
    expect(summary(overviewItems(null, RUNTIME))).toEqual([
      'signup: ok Open → signup',
      'api-access: ok Key required → security',
    ]);
    expect(summary(overviewItems(ENTRIES, []))).toEqual([
      'site: ok Set → general',
      'email: attention Not set up → email',
      'alerts: ok On → alerts',
    ]);
  });
});
