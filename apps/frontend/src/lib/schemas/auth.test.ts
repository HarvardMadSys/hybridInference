import { describe, expect, it } from 'vitest';

import { translate } from '@/lib/i18n/translate';
import {
  createAuthSchemas,
  createSignupSchema,
  loginSchema,
  passwordSchema,
  signupSchema,
} from './auth';

describe('auth schemas', () => {
  it('accepts a strong password', () => {
    expect(passwordSchema.safeParse('SecurePass123').success).toBe(true);
  });

  it('rejects weak passwords', () => {
    expect(passwordSchema.safeParse('weakpass').success).toBe(false);
    expect(passwordSchema.safeParse('NoNumberHere').success).toBe(false);
    expect(passwordSchema.safeParse('nouppercase1').success).toBe(false);
  });

  it('requires signup password confirmation to match', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.com',
      password: 'SecurePass123',
      confirmPassword: 'DifferentPass123',
      userName: 'Example User',
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.confirmPassword).toContain(
        'Passwords do not match',
      );
    }
  });

  it('does not carry a ToS flag; consent is collected before the form', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      acceptTerms: false,
    });

    expect(result.success).toBe(true);
  });

  it('accepts minimal signup input', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
    });

    expect(result.success).toBe(true);
  });

  it('rejects signup when combined use case and discovery exceed 2000 chars', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      useCase: 'a'.repeat(1900),
      discoverySource: 'b'.repeat(400),
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.discoverySource).toContain(
        'Combined use case and discovery response is too long (max 2000 characters)',
      );
    }
  });

  it('accepts signup when combined use case and discovery stay within 2000 chars', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      useCase: 'a'.repeat(1500),
      discoverySource: 'b'.repeat(400),
    });

    expect(result.success).toBe(true);
  });

  it('measures the combined signup text with the runtime site host', () => {
    const input = {
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      useCase: 'a'.repeat(1900),
      discoverySource: 'b'.repeat(50),
    };
    const runtimeSchema = createSignupSchema('x'.repeat(100));
    const result = runtimeSchema.safeParse(input);

    expect(signupSchema.safeParse(input).success).toBe(true);
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.discoverySource).toContain(
        'Combined use case and discovery response is too long (max 2000 characters)',
      );
    }
  });

  it('accepts valid login input', () => {
    expect(
      loginSchema.safeParse({
        email: 'user@example.com',
        password: 'anything-present',
      }).success,
    ).toBe(true);
  });
});

describe('auth schema distribution messages', () => {
  it('resolves validation messages through the supplied translator', () => {
    // Validation messages are interface copy: a distribution translates them
    // through the same slots as the page around them.
    const {
      createSignupSchema: runtimeSignup,
      emailSchema,
      passwordSchema,
    } = createAuthSchemas((slot, fallback) =>
      slot.startsWith('auth.validation.') ? `<${slot}>` : fallback,
    );

    expect(emailSchema.safeParse('not-an-email').error?.issues[0]?.message).toBe(
      '<auth.validation.email_invalid>',
    );
    expect(passwordSchema.safeParse('short').error?.issues[0]?.message).toBe(
      '<auth.validation.password_min>',
    );
    expect(
      runtimeSignup()
        .safeParse({
          email: 'user@example.org',
          password: 'SecurePass123',
          confirmPassword: 'DifferentPass123',
          userName: 'Example User',
        })
        .error?.flatten().fieldErrors.confirmPassword,
    ).toEqual(['<auth.validation.passwords_differ>']);
  });

  it('keeps today’s English for a deployment that fills no slot', () => {
    // The module-level exports are exactly this call, so a regression in the
    // default path would silently reword every deployment's form errors.
    const { emailSchema } = createAuthSchemas(translate);

    expect(emailSchema.safeParse('not-an-email').error?.issues[0]?.message).toBe(
      'Please enter a valid email address',
    );
  });
});
