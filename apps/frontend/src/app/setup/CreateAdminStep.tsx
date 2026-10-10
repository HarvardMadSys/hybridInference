'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';

import { useAuth } from '@/components/providers';
import { Button } from '@/components/ui/Button';
import {
  createSetupAdmin,
  isWellFormedSetupCode,
  normalizeSetupCode,
  SETUP_CODE_LENGTH,
} from '@/lib/api/setup';
import { LOGIN_NAME_PATTERN, passwordSchema } from '@/lib/schemas/auth';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { navigateTo } from '@/lib/utils/navigation';

import { SETUP_INPUT_CLASS, SETUP_INPUT_ERROR_CLASS, SetupField } from './SetupField';

/** The command that finds the code; the backend logs it at every boot until setup is done. */
export const SETUP_CODE_LOG_COMMAND =
  "docker logs hybridinference-backend 2>&1 | grep 'setup code'";

const createAdminSchema = z
  .object({
    setupCode: z
      .string()
      .trim()
      .min(1, 'Enter the setup code from the backend log.')
      .refine(
        isWellFormedSetupCode,
        `A setup code has ${SETUP_CODE_LENGTH} letters and digits, like ABCD-EFGH-JKLM.`,
      ),
    loginName: z
      .string()
      .trim()
      .regex(
        LOGIN_NAME_PATTERN,
        'Use 3–32 letters, digits, dots, underscores or hyphens, starting with a letter or digit.',
      ),
    // Optional; when given, the backend's 2–50 characters.
    displayName: z
      .string()
      .trim()
      .refine(
        (value) => value === '' || (value.length >= 2 && value.length <= 50),
        'A display name has 2–50 characters.',
      ),
    password: passwordSchema,
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: 'Passwords do not match.',
    path: ['confirmPassword'],
  });

type CreateAdminForm = z.infer<typeof createAdminSchema>;

/** Request field → form field, for a 422's per-field messages. */
const FIELD_FOR: Record<string, keyof CreateAdminForm> = {
  setup_code: 'setupCode',
  login_name: 'loginName',
  display_name: 'displayName',
  password: 'password',
};

/**
 * Step 1: the first administrator, authorized by the setup code.
 *
 * Success signs the browser in (the response is a login), so the next step
 * can call the admin API straight away.
 */
export function CreateAdminStep({ onCreated }: { onCreated: () => void }) {
  const { adoptSession } = useAuth();
  const [formError, setFormError] = useState<string | null>(null);
  const [alreadySetUp, setAlreadySetUp] = useState(false);
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<CreateAdminForm>({
    resolver: zodResolver(createAdminSchema),
    defaultValues: {
      setupCode: '',
      loginName: '',
      displayName: '',
      password: '',
      confirmPassword: '',
    },
  });

  const onSubmit = async (data: CreateAdminForm) => {
    setFormError(null);
    try {
      const result = await createSetupAdmin({
        setup_code: normalizeSetupCode(data.setupCode),
        login_name: data.loginName,
        password: data.password,
        display_name: data.displayName || undefined,
      });
      await adoptSession(result.access_token);
      onCreated();
    } catch (err) {
      if (!(err instanceof APIError)) {
        setFormError(getErrorMessage(err));
        return;
      }
      switch (err.statusCode) {
        case 403:
          setError('setupCode', {
            message:
              'That setup code is not correct. Use the most recent code in the backend log — a new one is printed every time the backend starts.',
          });
          return;
        case 409:
          setAlreadySetUp(true);
          return;
        case 422: {
          const fields = (err.details?.fields ?? {}) as Record<string, string>;
          let placed = false;
          for (const [field, message] of Object.entries(fields)) {
            const formField = FIELD_FOR[field];
            if (formField) {
              setError(formField, { message });
              placed = true;
            }
          }
          if (!placed) setFormError(err.message || 'Check the details and try again.');
          return;
        }
        case 429:
          setFormError(
            'Too many attempts. Wait a few minutes before trying again — attempts are limited to protect the setup code.',
          );
          return;
        case 503:
          setFormError(
            'The backend cannot reach its database. Check the database, then try again.',
          );
          return;
        default:
          setFormError(getErrorMessage(err));
      }
    }
  };

  if (alreadySetUp) {
    return (
      <div className="space-y-4" role="status">
        <p className="text-sm text-gray-700">
          This deployment has already been set up. Sign in with an administrator account to change
          its configuration.
        </p>
        {/* A full page load: this page's site configuration still says setup
            is pending, and a client-side navigation would be sent back here. */}
        <Button type="button" onClick={() => navigateTo('/login')}>
          Go to login
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-5" noValidate>
      {formError ? (
        <div
          className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
          role="alert"
        >
          {formError}
        </div>
      ) : null}

      <SetupField
        id="setup-code"
        label="Setup code"
        error={errors.setupCode?.message}
        hint={
          <>
            Find the setup code in the backend log, for example with{' '}
            <code className="rounded bg-gray-100 px-1 font-mono text-gray-800">
              {SETUP_CODE_LOG_COMMAND}
            </code>{' '}
            (use your backend container&apos;s name). The code stays the same until setup is
            complete.
          </>
        }
      >
        {(a11y) => (
          <input
            id="setup-code"
            type="text"
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="XXXX-XXXX-XXXX"
            className={`${errors.setupCode ? SETUP_INPUT_ERROR_CLASS : SETUP_INPUT_CLASS} font-mono tracking-wider`}
            {...a11y}
            {...register('setupCode')}
          />
        )}
      </SetupField>

      <SetupField
        id="setup-login-name"
        label="Username"
        error={errors.loginName?.message}
        hint="You sign in with this. 3–32 letters, digits, dots, underscores or hyphens."
      >
        {(a11y) => (
          <input
            id="setup-login-name"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            className={errors.loginName ? SETUP_INPUT_ERROR_CLASS : SETUP_INPUT_CLASS}
            {...a11y}
            {...register('loginName')}
          />
        )}
      </SetupField>

      <SetupField
        id="setup-display-name"
        label="Display name"
        optional
        error={errors.displayName?.message}
        hint="Shown in the console. Defaults to the username."
      >
        {(a11y) => (
          <input
            id="setup-display-name"
            type="text"
            autoComplete="name"
            className={errors.displayName ? SETUP_INPUT_ERROR_CLASS : SETUP_INPUT_CLASS}
            {...a11y}
            {...register('displayName')}
          />
        )}
      </SetupField>

      <SetupField
        id="setup-password"
        label="Password"
        error={errors.password?.message}
        hint="At least 8 characters, with an uppercase letter, a lowercase letter and a number."
      >
        {(a11y) => (
          <input
            id="setup-password"
            type="password"
            autoComplete="new-password"
            className={errors.password ? SETUP_INPUT_ERROR_CLASS : SETUP_INPUT_CLASS}
            {...a11y}
            {...register('password')}
          />
        )}
      </SetupField>

      <SetupField
        id="setup-confirm-password"
        label="Confirm password"
        error={errors.confirmPassword?.message}
      >
        {(a11y) => (
          <input
            id="setup-confirm-password"
            type="password"
            autoComplete="new-password"
            className={errors.confirmPassword ? SETUP_INPUT_ERROR_CLASS : SETUP_INPUT_CLASS}
            {...a11y}
            {...register('confirmPassword')}
          />
        )}
      </SetupField>

      <p className="text-xs text-gray-500">
        This account has no email address, so it cannot reset its password by email. Keep the
        password somewhere safe; an operator with database access can reset it with{' '}
        <code className="rounded bg-gray-100 px-1 font-mono text-gray-800">
          python -m serving.auth.reset_password
        </code>
        .
      </p>

      <Button type="submit" className="w-full" isLoading={isSubmitting}>
        Create administrator
      </Button>
    </form>
  );
}
