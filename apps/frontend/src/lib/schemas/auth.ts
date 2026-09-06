import { z } from 'zod';

export const emailSchema = z
  .string()
  .email('Please enter a valid email address')
  .max(255, 'Email address is too long');

export const passwordSchema = z
  .string()
  .min(8, 'Password must be at least 8 characters')
  .regex(/[A-Z]/, 'Password must contain at least one uppercase letter')
  .regex(/[a-z]/, 'Password must contain at least one lowercase letter')
  .regex(/[0-9]/, 'Password must contain at least one number');

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

const signupFieldsSchema = z.object({
  email: emailSchema,
  password: passwordSchema,
  confirmPassword: z.string(),
  userName: z
    .string()
    .trim()
    .min(2, 'Username must be at least 2 characters')
    .max(50, 'Username cannot exceed 50 characters'),
  useCase: z
    .string()
    .trim()
    .max(2000, 'Use case cannot exceed 2000 characters')
    .optional()
    .or(z.literal('')),
  discoverySource: z
    .string()
    .trim()
    .max(500, 'Response cannot exceed 500 characters')
    .optional()
    .or(z.literal('')),
});

export function createSignupSchema(siteHost = 'this service') {
  return signupFieldsSchema
    .refine((data) => data.password === data.confirmPassword, {
      message: 'Passwords do not match',
      path: ['confirmPassword'],
    })
    .refine(
      (data) => buildCombinedUseCase(data.useCase, data.discoverySource, siteHost).length <= 2000,
      {
        message: 'Combined use case and discovery response is too long (max 2000 characters)',
        path: ['discoverySource'],
      },
    );
}

export const signupSchema = createSignupSchema();

export const loginSchema = z.object({
  email: emailSchema,
  password: z.string().min(1, 'Please enter your password'),
});

export const profileUpdateSchema = z.object({
  userName: z
    .string()
    .trim()
    .min(2, 'Username must be at least 2 characters')
    .max(50, 'Username cannot exceed 50 characters')
    .optional(),
});

// Type exports
export type SignupFormData = z.infer<typeof signupSchema>;
export type LoginFormData = z.infer<typeof loginSchema>;
export type ProfileUpdateData = z.infer<typeof profileUpdateSchema>;
