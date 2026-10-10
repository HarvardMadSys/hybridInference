import { describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import {
  buildPatch,
  countDirty,
  draftError,
  errorForKey,
  groupEntries,
  isDirty,
  looksSecret,
  matchesQuery,
  settleDraft,
} from './configDrafts';

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
    source: 'default',
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

describe('the PATCH body', () => {
  it('sends each type as its JSON type', () => {
    const entries = [
      entry({ key: 'FLAG', type: 'bool', value: false }),
      entry({ key: 'PORT', type: 'int', value: 25 }),
      entry({ key: 'RATIO', type: 'float', value: 0.5 }),
      entry({ key: 'HOST', type: 'str', value: 'a' }),
      entry({ key: 'ORIGINS', type: 'list', value: 'https://a.test' }),
      entry({ key: 'PEM', type: 'text', value: '' }),
    ];

    const { values, errors } = buildPatch(entries, {
      FLAG: { kind: 'value', value: true },
      PORT: { kind: 'value', value: '587' },
      RATIO: { kind: 'value', value: '0.25' },
      HOST: { kind: 'value', value: 'smtp.example.test' },
      ORIGINS: { kind: 'value', value: 'https://a.test,https://b.test' },
      PEM: { kind: 'value', value: '-----BEGIN KEY-----\nabc\n' },
    });

    expect(errors).toEqual({});
    expect(values).toEqual({
      FLAG: true,
      PORT: 587,
      RATIO: 0.25,
      HOST: 'smtp.example.test',
      ORIGINS: 'https://a.test,https://b.test',
      PEM: '-----BEGIN KEY-----\nabc\n',
    });
  });

  it('replaces a secret with the typed string and clears it with ""', () => {
    const entries = [
      entry({ key: 'SMTP_PASSWORD', secret: true, value: null }),
      entry({ key: 'SLACK_WEBHOOK_URL', secret: true, value: null }),
    ];

    const { values } = buildPatch(entries, {
      SMTP_PASSWORD: { kind: 'replace', value: 'hunter2' },
      SLACK_WEBHOOK_URL: { kind: 'clear' },
    });

    expect(values).toEqual({ SMTP_PASSWORD: 'hunter2', SLACK_WEBHOOK_URL: '' });
  });

  it('sends nothing for an open but empty replacement', () => {
    const secret = entry({ key: 'SMTP_PASSWORD', secret: true, value: null });

    expect(isDirty(secret, { kind: 'replace', value: '' })).toBe(false);
    expect(buildPatch([secret], { SMTP_PASSWORD: { kind: 'replace', value: '' } }).values).toEqual(
      {},
    );
  });

  it('refuses a number field that does not hold its type', () => {
    const entries = [
      entry({ key: 'PORT', type: 'int', value: 25 }),
      entry({ key: 'RATIO', type: 'float', value: 0.5 }),
    ];

    const { values, errors } = buildPatch(entries, {
      PORT: { kind: 'value', value: '2.5' },
      RATIO: { kind: 'value', value: '' },
    });

    expect(values).toEqual({});
    expect(errors).toEqual({ PORT: 'Enter a whole number.', RATIO: 'Enter a number.' });
  });

  it('ignores an edit back to the current value', () => {
    const port = entry({ key: 'PORT', type: 'int', value: 25 });
    const flag = entry({ key: 'FLAG', type: 'bool', value: true });

    expect(isDirty(port, { kind: 'value', value: '25' })).toBe(false);
    expect(isDirty(port, { kind: 'value', value: '025' })).toBe(false);
    expect(settleDraft(flag, { kind: 'value', value: true })).toBeUndefined();
    expect(settleDraft(flag, { kind: 'value', value: false })).toEqual({
      kind: 'value',
      value: false,
    });
    expect(countDirty([port, flag], { PORT: { kind: 'value', value: '26' } })).toBe(1);
  });

  it('accepts no edit to an immutable setting that is already set', () => {
    const locked = entry({ key: 'API_KEY_SECRET', secret: true, immutable: true, is_set: true });
    const unset = entry({ key: 'API_KEY_SECRET', secret: true, immutable: true, is_set: false });

    expect(isDirty(locked, { kind: 'replace', value: 'new' })).toBe(false);
    expect(isDirty(unset, { kind: 'replace', value: 'new' })).toBe(true);
    expect(draftError(locked, { kind: 'replace', value: 'new' })).toBeNull();
  });
});

describe('grouping and search', () => {
  it('follows the response order and keeps an undescribed category', () => {
    const groups = groupEntries(
      [
        { id: 'email', label: 'Email (SMTP)', description: 'Outgoing mail' },
        { id: 'security', label: 'Security', description: '' },
        { id: 'alerts', label: 'Alerts', description: '' },
      ],
      [
        entry({ key: 'JWT_SECRET_KEY', category: 'security' }),
        entry({ key: 'SMTP_HOST', category: 'email' }),
        entry({ key: 'MYSTERY', category: 'future' }),
      ],
    );

    expect(groups.map((group) => [group.label, group.entries.map((e) => e.key)])).toEqual([
      ['Email (SMTP)', ['SMTP_HOST']],
      ['Security', ['JWT_SECRET_KEY']],
      ['Future', ['MYSTERY']],
    ]);
  });

  it('matches the name, description, category and models', () => {
    const provider = entry({
      key: 'ZAI_API_KEY',
      description: 'Zhipu key',
      used_by: ['glm-4.6'],
    });

    expect(matchesQuery(provider, 'zai')).toBe(true);
    expect(matchesQuery(provider, 'zhipu')).toBe(true);
    expect(matchesQuery(provider, 'GLM-4')).toBe(true);
    expect(matchesQuery(provider, 'providers', 'Providers')).toBe(true);
    expect(matchesQuery(provider, 'smtp')).toBe(false);
  });
});

describe('error details', () => {
  it('attributes "KEY: reason" to a key in the batch', () => {
    expect(errorForKey('SMTP_PORT: must be an integer', ['SMTP_PORT'])).toEqual({
      key: 'SMTP_PORT',
      reason: 'must be an integer',
    });
    expect(errorForKey('SMTP_PORT: must be an integer', ['OTHER'])).toBeNull();
    expect(errorForKey('Something went wrong', ['SMTP_PORT'])).toBeNull();
  });

  it('guesses secrets the way the backend does', () => {
    expect(looksSecret('MY_PROVIDER_API_KEY')).toBe(true);
    expect(looksSecret('SLACK_WEBHOOK_URL')).toBe(true);
    expect(looksSecret('MY_PROVIDER_BASE_URL')).toBe(false);
  });
});
