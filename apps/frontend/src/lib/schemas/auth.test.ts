import { describe, expect, it } from 'vitest';

import { loginSchema, passwordSchema, signupSchema } from './auth';

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

  it('accepts valid login input', () => {
    expect(
      loginSchema.safeParse({
        email: 'user@example.com',
        password: 'anything-present',
      }).success,
    ).toBe(true);
  });
});
