import { describe, expect, it } from 'vitest';

import { createSignupSchema, loginSchema, passwordSchema, signupSchema } from './auth';

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
      acceptTerms: true,
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.confirmPassword).toContain(
        'Passwords do not match',
      );
    }
  });

  it('requires signup ToS acceptance', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      acceptTerms: false,
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.acceptTerms).toContain(
        'You must agree to the Terms of Service',
      );
    }
  });

  it('accepts signup input when ToS is agreed', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      acceptTerms: true,
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
      acceptTerms: true,
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
      acceptTerms: true,
    });

    expect(result.success).toBe(true);
  });

  it('validates combined use-case length with the runtime site host', () => {
    const runtimeSchema = createSignupSchema(`${'x'.repeat(240)}.example`);
    const result = runtimeSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      useCase: 'a'.repeat(1500),
      discoverySource: 'b'.repeat(400),
      acceptTerms: true,
    });

    expect(result.success).toBe(false);
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
