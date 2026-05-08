import { describe, expect, it } from 'vitest';

import {
  getProviderSpecialInput,
  normalizeChatGPTCookieInput,
  providerSpecialInputs,
} from './providerSpecialInputs';

describe('normalizeChatGPTCookieInput', () => {
  it('trims and returns raw credential input unchanged', () => {
    expect(normalizeChatGPTCookieInput('  session=raw_cookie_value  ')).toBe('session=raw_cookie_value');
  });

  it('normalizes a Cookie header into a canonical cookie string', () => {
    const input = 'Cookie: foo=bar; __Secure-next-auth.session-token=abc123; cf_clearance=clear456';

    expect(normalizeChatGPTCookieInput(input)).toBe(
      '__Secure-next-auth.session-token=abc123; cf_clearance=clear456',
    );
  });

  it('accepts lower-case cookie header prefix', () => {
    const input = 'cookie: __Secure-next-auth.session-token=abc123; other=value';

    expect(normalizeChatGPTCookieInput(input)).toBe('__Secure-next-auth.session-token=abc123');
  });

  it('preserves OAuth session-token aliases from browser cookies', () => {
    const input = 'Cookie: __Host-next-auth.csrf-token=ignored; next-auth.session-token=legacy123';

    expect(normalizeChatGPTCookieInput(input)).toBe('next-auth.session-token=legacy123');
  });

  it('keeps a semicolon-delimited raw cookie string when it contains the required session cookie', () => {
    const input = '__Secure-next-auth.session-token=abc123; cf_clearance=clear456';

    expect(normalizeChatGPTCookieInput(input)).toBe(
      '__Secure-next-auth.session-token=abc123; cf_clearance=clear456',
    );
  });

  it('rejects blank input', () => {
    expect(() => normalizeChatGPTCookieInput('   ')).toThrow('Cookie value must not be blank.');
  });

  it('rejects Cookie headers missing the required session cookie', () => {
    expect(() => normalizeChatGPTCookieInput('Cookie: foo=bar; cf_clearance=clear456')).toThrow(
      'ChatGPT cookie header did not include a session cookie.',
    );
  });

  it('rejects malformed Cookie headers', () => {
    expect(() => normalizeChatGPTCookieInput('Cookie: this-is-not-a-cookie')).toThrow(
      'Cookie header must contain name=value pairs.',
    );
  });
});

describe('providerSpecialInputs', () => {
  it('declares a ChatGPT cookie upload input', () => {
    expect(providerSpecialInputs.chatgpt.kind).toBe('cookieHeader');
    expect(providerSpecialInputs.chatgpt.actionLabel).toBe('Upload cookie');
  });

  it('returns config for ChatGPT and undefined for other providers', () => {
    expect(getProviderSpecialInput('chatgpt')).toBe(providerSpecialInputs.chatgpt);
    expect(getProviderSpecialInput('openrouter')).toBeUndefined();
  });
});
