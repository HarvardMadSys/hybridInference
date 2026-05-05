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

export const signupSchema = z
  .object({
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
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: 'Passwords do not match',
    path: ['confirmPassword'],
  });

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
