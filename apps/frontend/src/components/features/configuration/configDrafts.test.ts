import { describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';

import {
  applicableEntries,
  appliesNow,
  buildPatch,
  canMarkSecret,
  categoryOf,
  draftError,
  errorForKey,
  groupEntries,
  hasStoredValue,
  isDirty,
  isLocked,
  looksSecret,
  matchesQuery,
  matchingChoice,
  sectionOf,
  settleDraft,
  splitGroups,
  withoutMootDrafts,
  type ConfigSectionSplit,
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

  it('holds back an edit its editor says is not valid yet, even one at the saved value', () => {
    const catalog = entry({ key: 'MODEL_CATALOG', type: 'text', value: '{"a": 1}' });
    const draft = { kind: 'value' as const, value: '{"a": 1}', error: 'a: Enter a number.' };

    expect(isDirty(catalog, draft)).toBe(true);
    expect(settleDraft(catalog, draft)).toBe(draft);
    expect(draftError(catalog, draft)).toBe('a: Enter a number.');
    expect(buildPatch([catalog], { MODEL_CATALOG: draft })).toEqual({
      values: {},
      errors: { MODEL_CATALOG: 'a: Enter a number.' },
    });
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

  describe('a section split from its category', () => {
    const tuning: ConfigSectionSplit = {
      id: 'routing:advanced',
      category: 'routing',
      keys: new Set(['REQUEST_TIMEOUT_SECONDS', 'CIRCUIT_FAILURE_THRESHOLD', 'GONE_FROM_BACKEND']),
      label: (categoryLabel) => `${categoryLabel} (advanced)`,
    };
    const groups = groupEntries(
      [
        { id: 'providers', label: 'Providers', description: 'Keys.' },
        { id: 'routing', label: 'Routing', description: 'Route selection.' },
      ],
      [
        entry({ key: 'ZAI_API_KEY', category: 'providers' }),
        entry({ key: 'ENABLE_ROUTEWISE', category: 'routing' }),
        entry({ key: 'REQUEST_TIMEOUT_SECONDS', category: 'routing' }),
        // Added by a later backend, and named by no split.
        entry({ key: 'ROUTING_NEW_SWITCH', category: 'routing' }),
        entry({ key: 'CIRCUIT_FAILURE_THRESHOLD', category: 'routing' }),
      ],
    );

    it('takes its settings into a group of their own, right after the rest of the category', () => {
      const split = splitGroups(groups, [tuning]);

      expect(
        split.map((group) => [group.id, group.label, group.entries.map((e) => e.key)]),
      ).toEqual([
        ['providers', 'Providers', ['ZAI_API_KEY']],
        ['routing', 'Routing', ['ENABLE_ROUTEWISE', 'ROUTING_NEW_SWITCH']],
        [
          'routing:advanced',
          'Routing (advanced)',
          ['REQUEST_TIMEOUT_SECONDS', 'CIRCUIT_FAILURE_THRESHOLD'],
        ],
      ]);
      expect(split[2].description).toBe('Route selection.');
      expect(split.map(categoryOf)).toEqual(['providers', 'routing', 'routing']);
      // The category's own group is not marked as split.
      expect(split[1].category).toBeUndefined();
      expect(split[2].category).toEqual({ id: 'routing', label: 'Routing' });
    });

    it('says which group a setting is edited in', () => {
      expect(sectionOf({ key: 'REQUEST_TIMEOUT_SECONDS', category: 'routing' }, [tuning])).toBe(
        'routing:advanced',
      );
      // A setting the split does not name stays with its category, a new one included.
      expect(sectionOf({ key: 'ROUTING_NEW_SWITCH', category: 'routing' }, [tuning])).toBe(
        'routing',
      );
      // A key the split names, but in another category, is not taken.
      expect(sectionOf({ key: 'REQUEST_TIMEOUT_SECONDS', category: 'batch' }, [tuning])).toBe(
        'batch',
      );
      expect(sectionOf({ key: 'ZAI_API_KEY', category: 'providers' }, [])).toBe('providers');
    });

    it('drops a group left with nothing, and changes nothing without a split', () => {
      const allTuning: ConfigSectionSplit = {
        ...tuning,
        keys: new Set(['ENABLE_ROUTEWISE', 'REQUEST_TIMEOUT_SECONDS', 'ROUTING_NEW_SWITCH']),
      };
      const onlyTuning = splitGroups(
        groups
          .filter((group) => group.id === 'routing')
          .map((group) => ({
            ...group,
            entries: group.entries.filter((e) => e.key !== 'CIRCUIT_FAILURE_THRESHOLD'),
          })),
        [allTuning],
      );
      expect(onlyTuning.map((group) => group.id)).toEqual(['routing:advanced']);

      const none = splitGroups(groups, [{ ...tuning, keys: new Set(['NOT_THERE']) }]);
      expect(none.map((group) => group.id)).toEqual(['providers', 'routing']);
      expect(splitGroups(groups, [])).toEqual(groups);
    });

    it('describes a part in its own words when it has them', () => {
      const policy: ConfigSectionSplit = {
        ...tuning,
        id: 'routing:policy',
        keys: new Set(['ENABLE_ROUTEWISE']),
        label: () => 'Routing policy',
        description: 'The default router.',
      };
      const split = splitGroups(groups, [policy, tuning]);
      const describe = (id: string) => split.find((group) => group.id === id)?.description;
      expect(describe('routing:policy')).toBe('The default router.');
      expect(split.find((group) => group.id === 'routing:policy')?.label).toBe('Routing policy');
      // Without words of its own, a part keeps its category's.
      expect(describe('routing:advanced')).toBe('Route selection.');
    });
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
    // And whatever else a page shows it under, such as a plain name.
    expect(matchesQuery(provider, 'z.ai key', '', ['Z.ai API key'])).toBe(false);
    expect(matchesQuery(provider, 'z.ai api', '', ['Z.ai API key'])).toBe(true);
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

describe('settings that apply to one choice', () => {
  const provider = entry({
    key: 'EMAIL_PROVIDER',
    value: 'smtp',
    choices: ['smtp', 'cloudflare'],
  });
  const host = entry({
    key: 'SMTP_HOST',
    value: 'mx.test',
    applies_when: { key: 'EMAIL_PROVIDER', values: ['smtp'] },
  });
  const account = entry({
    key: 'CLOUDFLARE_ACCOUNT_ID',
    applies_when: { key: 'EMAIL_PROVIDER', values: ['cloudflare'] },
  });
  const sender = entry({ key: 'SMTP_FROM_EMAIL', value: 'noreply@example.test' });
  const entries = [provider, host, account, sender];

  it('follows the choice as the editor shows it, unsaved edit included', () => {
    expect(appliesNow(host, entries, {})).toBe(true);
    expect(appliesNow(account, entries, {})).toBe(false);
    const cloudflare = { EMAIL_PROVIDER: { kind: 'value', value: 'cloudflare' } } as const;
    expect(appliesNow(host, entries, cloudflare)).toBe(false);
    expect(appliesNow(account, entries, cloudflare)).toBe(true);
    // No condition, or a deciding setting the page does not have: it applies.
    expect(appliesNow(sender, entries, cloudflare)).toBe(true);
    expect(appliesNow(account, [account], {})).toBe(true);
  });

  it('keeps a refused value in sight whichever choice is made', () => {
    const refused = { ...account, invalid: 'must be the 32 hexadecimal digits' };
    expect(applicableEntries([provider, host, refused, sender], {}).map((e) => e.key)).toEqual([
      'EMAIL_PROVIDER',
      'SMTP_HOST',
      'CLOUDFLARE_ACCOUNT_ID',
      'SMTP_FROM_EMAIL',
    ]);
    expect(applicableEntries(entries, {}).map((e) => e.key)).toEqual([
      'EMAIL_PROVIDER',
      'SMTP_HOST',
      'SMTP_FROM_EMAIL',
    ]);
  });

  it('matches a choice as the backend does, ignoring case unless told otherwise', () => {
    const level = entry({ key: 'LOG_LEVEL', choices: ['DEBUG', 'INFO'], case_sensitive: false });
    expect(matchingChoice(level, 'info')).toBe('INFO');
    expect(matchingChoice(level, 'verbose')).toBeUndefined();
    const algorithm = entry({ key: 'JWT_ALGORITHM', choices: ['HS256'], case_sensitive: true });
    expect(matchingChoice(algorithm, 'hs256')).toBeUndefined();
    expect(matchingChoice(algorithm, 'HS256')).toBe('HS256');

    // A deciding setting with no value holds its default.
    const unset = { ...provider, value: null, default: 'smtp' };
    expect(appliesNow(host, [unset, host, account], {})).toBe(true);
    expect(appliesNow(account, [unset, host, account], {})).toBe(false);

    // A deciding setting stored in another case still decides.
    const mode = entry({ key: 'MODE', value: 'FAST', choices: ['fast', 'safe'] });
    const tuned = entry({ key: 'FAST_ONLY', applies_when: { key: 'MODE', values: ['fast'] } });
    expect(appliesNow(tuned, [mode, tuned], {})).toBe(true);
  });

  it('drops the edits a saved choice made moot, and no others', () => {
    const saved = [{ ...provider, value: 'cloudflare' }, host, account, sender];
    const drafts = {
      SMTP_HOST: { kind: 'value', value: 'smtp.new.test' },
      SMTP_FROM_EMAIL: { kind: 'value', value: 'mail@example.test' },
    } as const;

    expect(withoutMootDrafts(drafts, saved, ['EMAIL_PROVIDER'])).toEqual({
      SMTP_FROM_EMAIL: { kind: 'value', value: 'mail@example.test' },
    });
    // A save that did not touch the choice leaves its edits alone.
    expect(withoutMootDrafts(drafts, saved, ['SMTP_FROM_EMAIL'])).toBe(drafts);
  });
});
