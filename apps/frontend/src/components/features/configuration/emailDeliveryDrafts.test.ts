import { describe, expect, it } from 'vitest';

import type { ConfigEntry } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';

import {
  emailDeliveryParts,
  emailFieldEntry,
  emailFieldMatches,
  emailProviderEntry,
  emailTestFailureMessage,
  looksLikeEmailAddress,
  savedEmailStatus,
  selectedEmailProvider,
  senderWarning,
} from './emailDeliveryDrafts';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'email',
    description: 'From the backend.',
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
    setup: true,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

const SMTP = { key: 'EMAIL_PROVIDER', values: ['smtp'] };
const CLOUDFLARE = { key: 'EMAIL_PROVIDER', values: ['cloudflare'] };
const SES = { key: 'EMAIL_PROVIDER', values: ['tencent_ses'] };

function provider(value: string | null, choices = ['smtp', 'cloudflare']): ConfigEntry {
  return entry({
    key: 'EMAIL_PROVIDER',
    value,
    default: 'smtp',
    choices,
    is_set: value !== null,
  });
}

function category(saved: string | null): ConfigEntry[] {
  return [
    provider(saved, ['smtp', 'cloudflare', 'tencent_ses']),
    entry({ key: 'SMTP_HOST', value: 'mx.test', applies_when: SMTP }),
    entry({ key: 'SMTP_USER', value: '', is_set: false, applies_when: SMTP }),
    entry({ key: 'SMTP_PASSWORD', secret: true, value: null, is_set: true, applies_when: SMTP }),
    entry({ key: 'CLOUDFLARE_ACCOUNT_ID', value: 'abc', applies_when: CLOUDFLARE }),
    entry({
      key: 'CLOUDFLARE_EMAIL_API_TOKEN',
      secret: true,
      value: null,
      applies_when: CLOUDFLARE,
    }),
    entry({ key: 'TENCENT_SES_FROM_EMAIL', value: '', is_set: false, applies_when: SES }),
    entry({ key: 'SMTP_FROM_EMAIL', value: 'hello@example.test' }),
    entry({ key: 'SMTP_FROM_NAME', value: 'Example' }),
  ];
}

describe('the transport choice', () => {
  it('is offered only when the backend offers a choice', () => {
    expect(emailProviderEntry([provider('smtp')])).not.toBeNull();
    expect(emailProviderEntry([provider('smtp', ['smtp'])])).toBeNull();
    expect(emailProviderEntry([entry({ key: 'SMTP_HOST' })])).toBeNull();
  });

  it('follows an unsaved choice, and reads an empty value as the default', () => {
    expect(selectedEmailProvider(provider(null), {})).toBe('smtp');
    expect(
      selectedEmailProvider(provider('smtp'), {
        EMAIL_PROVIDER: { kind: 'value', value: 'cloudflare' },
      }),
    ).toBe('cloudflare');
  });

  it("splits the chosen transport's settings from the sender", () => {
    const parts = emailDeliveryParts(category('cloudflare'), {});

    expect(parts?.transport.map((item) => item.key)).toEqual([
      'CLOUDFLARE_ACCOUNT_ID',
      'CLOUDFLARE_EMAIL_API_TOKEN',
    ]);
    expect(parts?.sender.map((item) => item.key)).toEqual(['SMTP_FROM_EMAIL', 'SMTP_FROM_NAME']);
  });

  it("still shows another transport's setting whose stored value was refused", () => {
    const entries = category('cloudflare').map((item) =>
      item.key === 'SMTP_HOST' ? { ...item, invalid: 'not a host' } : item,
    );

    expect(emailDeliveryParts(entries, {})?.transport.map((item) => item.key)).toContain(
      'SMTP_HOST',
    );
  });
});

describe('the saved status', () => {
  it('names what a built-in transport still needs by its label', () => {
    expect(savedEmailStatus(category('smtp'))).toEqual({
      kind: 'incomplete',
      provider: 'smtp',
      missing: ['Username'],
    });
  });

  it('is ready when a built-in transport has everything it needs', () => {
    expect(savedEmailStatus(category('cloudflare'))).toEqual({
      kind: 'ready',
      provider: 'cloudflare',
    });
  });

  it("lists an extension's missing settings by key, and claims nothing otherwise", () => {
    const missing = category('tencent_ses').map((item) =>
      item.key === 'TENCENT_SES_FROM_EMAIL' ? { ...item, missing: true, required: true } : item,
    );

    expect(savedEmailStatus(missing)).toEqual({
      kind: 'incomplete',
      provider: 'tencent_ses',
      missing: ['TENCENT_SES_FROM_EMAIL'],
    });
    expect(savedEmailStatus(category('tencent_ses'))).toEqual({
      kind: 'unknown',
      provider: 'tencent_ses',
    });
  });

  it('reads the saved choice, not an unsaved one', () => {
    // Nothing here depends on drafts: the status is about what sends now.
    expect(savedEmailStatus(category(null))?.provider).toBe('smtp');
  });
});

describe('the sender warning', () => {
  const sender = (value: string) => [
    provider('smtp'),
    entry({ key: 'SMTP_FROM_EMAIL', value, default: 'noreply@localhost' }),
  ];

  it('warns about an address at localhost, in the words of the chosen service', () => {
    expect(senderWarning(sender('noreply@localhost'), {}, 'smtp')).toMatch(/^Most mail servers/);
    expect(senderWarning(sender('noreply@LOCALHOST'), {}, 'cloudflare')).toMatch(
      /onboarded under Email Service → Email Sending/,
    );
  });

  it('follows the address being typed', () => {
    const drafts = { SMTP_FROM_EMAIL: { kind: 'value' as const, value: 'hi@example.test' } };

    expect(senderWarning(sender('noreply@localhost'), drafts, 'smtp')).toBeNull();
  });

  it("leaves an extension's own sender alone", () => {
    expect(senderWarning(sender('noreply@localhost'), {}, 'tencent_ses')).toBeNull();
  });
});

describe('the names the card shows', () => {
  it('replaces a built-in description and keeps an extension one', () => {
    const host = entry({ key: 'SMTP_HOST', description: 'Required while public signup…' });
    const region = entry({ key: 'TENCENT_SES_REGION', description: 'Tencent SES region.' });

    expect(emailFieldEntry(host, 'smtp').description).toMatch(/^Your mail server's host name/);
    expect(emailFieldEntry(region, 'tencent_ses').description).toBe('Tencent SES region.');
  });

  it("describes the sender in the chosen service's terms", () => {
    const address = entry({ key: 'SMTP_FROM_EMAIL' });

    expect(emailFieldEntry(address, 'cloudflare').description).toMatch(/onboarded/);
    expect(emailFieldEntry(address, 'smtp').description).toMatch(/mail server may send as/);
  });

  it('lets a search find a setting by its friendly name or a service', () => {
    expect(emailFieldMatches(entry({ key: 'SMTP_FROM_EMAIL' }), 'sender')).toBe(true);
    expect(emailFieldMatches(entry({ key: 'EMAIL_PROVIDER' }), 'Cloudflare email')).toBe(true);
    expect(emailFieldMatches(entry({ key: 'SMTP_HOST', category: 'general' }), 'smtp host')).toBe(
      false,
    );
    expect(emailFieldMatches(entry({ key: 'SMTP_HOST' }), '  ')).toBe(false);
  });
});

describe('a failed test', () => {
  it("shows the backend's own sentence", () => {
    const refused = new APIError('HTTP_502', 'Cloudflare did not accept the API token.', 502);
    const missing = new APIError(
      'HTTP_400',
      'SMTP needs SMTP_PASSWORD. Save them, then send the test.',
      400,
    );

    expect(emailTestFailureMessage(refused)).toBe('Cloudflare did not accept the API token.');
    expect(emailTestFailureMessage(missing)).toMatch(/^SMTP needs SMTP_PASSWORD/);
  });

  it('explains an older backend, a rate limit and an answer without a sentence', () => {
    expect(emailTestFailureMessage(new APIError('HTTP_404', 'Not Found', 404))).toMatch(/Upgrade/);
    expect(emailTestFailureMessage(new APIError('RATE_LIMIT_EXCEEDED', 'x', 429))).toMatch(
      /^Wait a few seconds/,
    );
    expect(emailTestFailureMessage(new APIError('HTTP_502', 'HTTP 502: Bad Gateway', 502))).toMatch(
      /^The test email could not be sent/,
    );
    expect(emailTestFailureMessage(new TypeError('Failed to fetch'))).toMatch(
      /^The test email could not be sent/,
    );
  });
});

describe('the address check', () => {
  it.each([
    ['ops@example.test', true],
    ['  ops@example.test ', true],
    ['ops@localhost', false],
    ['not an address', false],
    ['', false],
  ])('%s → %s', (value, expected) => {
    expect(looksLikeEmailAddress(value)).toBe(expected);
  });
});
