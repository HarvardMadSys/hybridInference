import { describe, expect, it } from 'vitest';

import { validateSignupDomainInput } from './signupDomainValidation';

describe('validateSignupDomainInput', () => {
  it('accepts a plain exact domain', () => {
    const r = validateSignupDomainInput('acme.com');
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.domain).toBe('acme.com');
      expect(r.isWildcard).toBe(false);
    }
  });

  it('strips whitespace and lowercases', () => {
    const r = validateSignupDomainInput('  ACME.COM  ');
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.domain).toBe('acme.com');
      expect(r.isWildcard).toBe(false);
    }
  });

  it('detects wildcard prefix and strips it', () => {
    const r = validateSignupDomainInput('*.partner.io');
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.domain).toBe('partner.io');
      expect(r.isWildcard).toBe(true);
    }
  });

  it('rejects empty input', () => {
    const r = validateSignupDomainInput('');
    expect(r.ok).toBe(false);
  });

  it('rejects bare *. with no suffix', () => {
    const r = validateSignupDomainInput('*.');
    expect(r.ok).toBe(false);
  });

  it.each([['no-tld'], ['acme@com'], ['ac me.com'], ['*acme.com'], ['acme.*'], ['x.123']])(
    'rejects malformed input %s',
    (value) => {
      const r = validateSignupDomainInput(value);
      expect(r.ok).toBe(false);
    },
  );

  // Leading/trailing hyphens per label are invalid per RFC 1035.
  it.each([['-foo.com'], ['foo-.com'], ['sub.-foo.com'], ['sub.foo-.com'], ['-foo-.com']])(
    'rejects label with leading/trailing hyphen %s',
    (value) => {
      const r = validateSignupDomainInput(value);
      expect(r.ok).toBe(false);
    },
  );

  // Single-char labels and interior hyphens remain valid.
  it.each([['a.com'], ['a-b.com'], ['1foo.com'], ['x1-y2.example.io']])(
    'accepts valid label shape %s',
    (value) => {
      const r = validateSignupDomainInput(value);
      expect(r.ok).toBe(true);
    },
  );
});
