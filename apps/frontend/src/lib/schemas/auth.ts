import { z } from 'zod';
import { translate, type Translate } from '@/lib/i18n/translate';

/**
 * Build the auth form schemas for one translator.
 *
 * Validation messages are interface copy like any other string, so they go
 * through the same slot resolver the pages use: the second argument is the
 * neutral English default, which is what a distribution that has not filled
 * the slot renders. Every call therefore spells its English fallback out in
 * full, and `createAuthSchemas(translator(undefined))` — the module-level
 * exports below — resolves every one of them.
 */
export function createAuthSchemas(t: Translate) {
  const emailSchema = z
    .string()
    .email(t('auth.validation.email_invalid', 'Please enter a valid email address'))
    .max(255, t('auth.validation.email_too_long', 'Email address is too long'));

  const passwordSchema = z
    .string()
    .min(8, t('auth.validation.password_min', 'Password must be at least 8 characters'))
    .regex(
      /[A-Z]/,
      t('auth.validation.password_upper', 'Password must contain at least one uppercase letter'),
    )
    .regex(
      /[a-z]/,
      t('auth.validation.password_lower', 'Password must contain at least one lowercase letter'),
    )
    .regex(
      /[0-9]/,
      t('auth.validation.password_number', 'Password must contain at least one number'),
    );

  const signupFieldsSchema = z.object({
    email: emailSchema,
    password: passwordSchema,
    confirmPassword: z.string(),
    userName: z
      .string()
      .trim()
      .min(2, t('auth.validation.username_min', 'Username must be at least 2 characters'))
      .max(50, t('auth.validation.username_max', 'Username cannot exceed 50 characters')),
    useCase: z
      .string()
      .trim()
      .max(2000, t('auth.validation.use_case_max', 'Use case cannot exceed 2000 characters'))
      .optional()
      .or(z.literal('')),
    discoverySource: z
      .string()
      .trim()
      .max(500, t('auth.validation.discovery_max', 'Response cannot exceed 500 characters'))
      .optional()
      .or(z.literal('')),
  });

  const passwordMatchMessage = t('auth.validation.passwords_differ', 'Passwords do not match');
  const combinedMaxMessage = t(
    'auth.validation.combined_max',
    'Combined use case and discovery response is too long (max 2000 characters)',
  );

  /**
   * The signup schema names the site host in the text it measures, so the
   * factory stays a factory: each call mints fresh refinements over the
   * translator it was given.
   */
  function createSignupSchema(siteHost = 'this service') {
    return signupFieldsSchema
      .refine((data) => data.password === data.confirmPassword, {
        message: passwordMatchMessage,
        path: ['confirmPassword'],
      })
      .refine(
        (data) => buildCombinedUseCase(data.useCase, data.discoverySource, siteHost).length <= 2000,
        {
          message: combinedMaxMessage,
          path: ['discoverySource'],
        },
      );
  }

  const signupSchema = createSignupSchema();

  const loginSchema = z.object({
    email: emailSchema,
    password: z
      .string()
      .min(1, t('auth.validation.password_required', 'Please enter your password')),
  });

  const profileUpdateSchema = z.object({
    userName: z
      .string()
      .trim()
      .min(2, t('auth.validation.username_min', 'Username must be at least 2 characters'))
      .max(50, t('auth.validation.username_max', 'Username cannot exceed 50 characters'))
      .optional(),
  });

  return {
    emailSchema,
    passwordSchema,
    signupSchema,
    loginSchema,
    profileUpdateSchema,
    createSignupSchema,
  };
}

export function buildCombinedUseCase(
  useCase?: string,
  discoverySource?: string,
  siteHost = 'this service',
): string {
  const trimmedUseCase = useCase?.trim();
  const trimmedDiscovery = discoverySource?.trim();
  return [
    trimmedUseCase,
    trimmedDiscovery ? `How did you find ${siteHost}? ${trimmedDiscovery}` : undefined,
  ]
    .filter(Boolean)
    .join('\n\n');
}

/**
 * The console's instances: English, byte for byte what every call site passes.
 *
 * A distribution's module gets its own schema instances through
 * `createAuthSchemas` with its own resolver, which is why the factory exists —
 * the *messages* are interface copy, the rules are not, and only the messages
 * are translatable.
 */
const consoleSchemas = createAuthSchemas(translate);

export const emailSchema = consoleSchemas.emailSchema;
export const passwordSchema = consoleSchemas.passwordSchema;
export const signupSchema = consoleSchemas.signupSchema;
export const loginSchema = consoleSchemas.loginSchema;
export const profileUpdateSchema = consoleSchemas.profileUpdateSchema;
export const createSignupSchema = consoleSchemas.createSignupSchema;

// Type exports
export type SignupFormData = z.infer<typeof signupSchema>;
export type LoginFormData = z.infer<typeof loginSchema>;
export type ProfileUpdateData = z.infer<typeof profileUpdateSchema>;
