import { describe, expect, it } from 'vitest';

import { buildCurlExample, pickExampleModel } from './curlExample';

describe('buildCurlExample', () => {
  it('serializes and shell-quotes model ids before rendering the command', () => {
    const singleQuote = String.fromCodePoint(39);
    const command = buildCurlExample({
      exampleApiBase: 'https://api.example.test',
      exampleApiKeyEnvVar: 'EXAMPLE_API_KEY',
      exampleModel: `router${singleQuote}; printf PWNED >&2; #`,
    });

    expect(command).toContain(
      `router${singleQuote}"${singleQuote}"${singleQuote}; printf PWNED >&2; #`,
    );
    expect(command).not.toContain(`router${singleQuote}; printf PWNED >&2; #`);
    expect(command).toContain(`-d ${singleQuote}{`);
    expect(command).toContain('"messages": [');
    expect(command).toContain(`curl ${singleQuote}https://api.example.test/v1/chat/completions`);
    expect(command).toContain('Bearer $EXAMPLE_API_KEY');
  });
});

describe('pickExampleModel', () => {
  it('keeps the configured model while the list is unknown or empty', () => {
    expect(pickExampleModel(null, 'llama-3.3-70b')).toBe('llama-3.3-70b');
    expect(pickExampleModel([], 'llama-3.3-70b')).toBe('llama-3.3-70b');
  });

  it('prefers the configured model when the gateway serves it', () => {
    expect(pickExampleModel(['other', 'llama-3.3-70b'], 'llama-3.3-70b')).toBe('llama-3.3-70b');
  });

  it('falls back to the first served model so the command stays runnable', () => {
    // The build-time default names a model a fresh clone does not serve.
    expect(pickExampleModel(['example-chat', 'local-embedding'], 'llama-3.3-70b')).toBe(
      'example-chat',
    );
  });
});
