import { describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import {
  buildPatch,
  canMarkSecret,
  countDirty,
  draftError,
  errorForKey,
  groupEntries,
  hasStoredValue,
  isDirty,
  isLocked,
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

  it('accepts no edit to a setting the backend locked, set or not', () => {
    // The erasure fence already runs on API_KEY_SECRET: its own key is empty
    // and can no longer be set.
    const fence = entry({
      key: 'ERASURE_FENCE_SECRET',
      secret: true,
      immutable: true,
      is_set: false,
      locked: true,
    });

    expect(isLocked(fence)).toBe(true);
    expect(isDirty(fence, { kind: 'replace', value: 'new' })).toBe(false);
    expect(buildPatch([fence], { ERASURE_FENCE_SECRET: { kind: 'replace', value: 'x' } })).toEqual({
      values: {},
      errors: {},
    });
  });

  it("takes the backend's word on locking, and the old rule from an older backend", () => {
    const base = { secret: true, immutable: true };

    expect(isLocked(entry({ ...base, is_set: false, locked: false }))).toBe(false);
    expect(isLocked(entry({ ...base, is_set: true, locked: true }))).toBe(true);
    // No `locked` at all: immutable and set.
    expect(isLocked(entry({ ...base, is_set: true }))).toBe(true);
    expect(isLocked(entry({ ...base, is_set: false }))).toBe(false);
    expect(isLocked(entry({ immutable: false, is_set: true }))).toBe(false);
  });
});

const STORED_AT = '2026-10-01T12:00:00Z';

describe('marking a variable secret', () => {
  const stored = {
    secret: false,
    source: 'database' as const,
    is_set: true,
    value: 'v',
    updated_at: STORED_AT,
  };

  it.each(['discovered', 'numbered', 'custom'] as const)(
    'applies to a stored %s variable',
    (origin) => {
      expect(canMarkSecret(entry({ ...stored, origin }))).toBe(true);
    },
  );

  it('applies to a reset row too, which keeps the flag', () => {
    // No value stored: the environment's applies, and the row is a reset.
    expect(
      canMarkSecret(entry({ ...stored, origin: 'discovered', source: 'environment', value: 'e' })),
    ).toBe(true);
  });

  it('does not apply to a built-in setting, whose secret flag is fixed', () => {
    expect(canMarkSecret(entry({ ...stored, origin: 'static' }))).toBe(false);
  });

  it('is not offered by a backend that does not report where entries come from', () => {
    expect(canMarkSecret(entry({ ...stored, custom: true }))).toBe(false);
  });

  it('does not apply to a secret, to a variable with no row, or to a locked setting', () => {
    const variable = { ...stored, origin: 'custom' as const };

    expect(canMarkSecret(entry({ ...variable, secret: true, value: null }))).toBe(false);
    expect(canMarkSecret(entry({ ...variable, source: 'environment', updated_at: null }))).toBe(
      false,
    );
    expect(canMarkSecret(entry({ ...variable, locked: true }))).toBe(false);
  });
});

describe('a stored value', () => {
  it('is one in force or one the backend refused, which Reset removes', () => {
    expect(hasStoredValue(entry({ source: 'database', updated_at: STORED_AT }))).toBe(true);
    // Refused: the entry reports what applies instead, but the row is there.
    expect(
      hasStoredValue(
        entry({ source: 'environment', invalid: 'must be an integer', updated_at: STORED_AT }),
      ),
    ).toBe(true);
    expect(
      hasStoredValue(
        entry({ source: 'default', invalid: 'must be one of a, b', updated_at: STORED_AT }),
      ),
    ).toBe(true);
  });

  it('is not a reset row, nor nothing at all', () => {
    expect(hasStoredValue(entry({ source: 'environment', updated_at: STORED_AT }))).toBe(false);
    expect(hasStoredValue(entry({ source: 'default', updated_at: null }))).toBe(false);
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
    expect(looksSecret('MY_PROVIDER_TIMEOUT')).toBe(false);
  });

  it.each([
    'MY_PROVIDER_API_KEY',
    'UPSTREAM_TOKEN',
    'SIGNING_SECRET',
    'DB_PASSWORD',
    'TEAMS_WEBHOOK',
    'PRIVATE_PEM',
    'FTP_PASS',
    'SMB_PWD',
    'GCP_CREDENTIAL',
    'SERVICE_CREDENTIALS',
    'BASIC_AUTH',
    'SENTRY_DSN',
    'UPSTREAM_BEARER',
    'CLIENT_CERT',
    'VERTEX_SA_JSON',
  ])("treats %s as secret, as the backend's word list does", (name) => {
    expect(looksSecret(name)).toBe(true);
    // The form upper-cases a name as it is typed; the rule does not depend on it.
    expect(looksSecret(name.toLowerCase())).toBe(true);
  });
});
