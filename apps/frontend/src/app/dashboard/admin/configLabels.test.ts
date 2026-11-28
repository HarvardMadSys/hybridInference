import { describe, expect, it } from 'vitest';

import { configLabel } from './configLabels';
import { ADVANCED_SETTINGS, TOPIC_SECTIONS } from './settingsLayout';

describe('configLabel', () => {
  it('gives a built-in setting a plain name', () => {
    expect(configLabel({ key: 'ADMIN_EMAILS', category: 'security', origin: 'static' })).toBe(
      'Administrator emails',
    );
    expect(configLabel({ key: 'REQUEST_TIMEOUT_SECONDS', category: 'routing' })).toBe(
      'Request timeout (seconds)',
    );
    expect(configLabel({ key: 'ROUTING_POLICY', category: 'application' })).toBe('Routing policy');
  });

  it('names a provider key or base URL after its provider', () => {
    expect(configLabel({ key: 'ZAI_API_KEY', category: 'providers', origin: 'static' })).toBe(
      'Z.ai API key',
    );
    expect(configLabel({ key: 'MOONSHOT_API_KEY', category: 'providers' })).toBe(
      'Moonshot (Kimi) API key',
    );
    expect(configLabel({ key: 'CHUTES_BASE_URL', category: 'providers' })).toBe('Chutes base URL');
    // A provider an extension registers is named from its variable.
    expect(configLabel({ key: 'ACME_CLOUD_API_KEY', category: 'providers' })).toBe(
      'Acme Cloud API key',
    );
    // Only in Providers: elsewhere such a name is not a provider's.
    expect(configLabel({ key: 'ACME_API_KEY', category: 'integrations' })).toBeUndefined();
  });

  it('uses the email card’s names for the email settings', () => {
    expect(configLabel({ key: 'SMTP_HOST', category: 'email' })).toBe('SMTP host');
    expect(configLabel({ key: 'EMAIL_PROVIDER', category: 'email' })).toBe('Delivery service');
  });

  it('keeps the name of a variable that is known by it', () => {
    // A route's ${NAME}, a numbered provider key and one an administrator added.
    for (const origin of ['discovered', 'numbered', 'custom'] as const) {
      expect(
        configLabel({ key: 'EXAMPLE_UPSTREAM_API_KEY', category: 'providers', origin }),
        origin,
      ).toBeUndefined();
      expect(
        configLabel({ key: 'ZAI_API_KEY', category: 'providers', origin }),
        origin,
      ).toBeUndefined();
    }
    // A setting a later backend adds keeps its name until it is given one.
    expect(configLabel({ key: 'ROUTING_ADDED_LATER', category: 'routing' })).toBeUndefined();
  });

  it('names every setting the layout places by hand', () => {
    const keys = [
      ...Object.entries(ADVANCED_SETTINGS).flatMap(([category, list]) =>
        list.map((key) => ({ key, category })),
      ),
      ...TOPIC_SECTIONS.flatMap((section) =>
        section.keys.map((key) => ({ key, category: section.category })),
      ),
    ];
    for (const entry of keys) expect(configLabel(entry), entry.key).toBeTruthy();
  });
});
