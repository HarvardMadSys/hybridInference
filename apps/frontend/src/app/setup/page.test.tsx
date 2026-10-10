// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { ConfigEntry, ConfigResponse } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';

const replaceMock = vi.fn();
const navigateToMock = vi.fn();
const adoptSession = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/setup',
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { loading: false, isAuthenticated: false, user: null },
    adoptSession,
  }),
}));

vi.mock('@/lib/api/setup', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/setup')>()),
  getSetupStatus: vi.fn(),
  createSetupAdmin: vi.fn(),
}));

vi.mock('@/lib/api/config', () => ({
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  restartBackend: vi.fn(),
  waitForBackendRestart: vi.fn(),
}));

vi.mock('@/lib/api/admin', () => ({
  listRuntimeSettings: vi.fn(),
  updateRuntimeSetting: vi.fn(),
}));

vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

import { listRuntimeSettings, updateRuntimeSetting } from '@/lib/api/admin';
import { getConfig, patchConfig, restartBackend, waitForBackendRestart } from '@/lib/api/config';
import { createSetupAdmin, getSetupStatus } from '@/lib/api/setup';

import SetupPage from './page';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'general',
    description: '',
    type: 'str',
    secret: false,
    required: false,
    missing: false,
    is_set: false,
    value: null,
    default: null,
    source: 'default',
    restart_required: false,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: false,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

const CONFIG: ConfigResponse = {
  categories: [
    { id: 'general', label: 'General', description: 'The basics.' },
    { id: 'email', label: 'Email (SMTP)', description: 'Outgoing mail.' },
    { id: 'providers', label: 'Model providers', description: 'Credentials.' },
  ],
  entries: [
    entry({ key: 'PUBLIC_BASE_URL', category: 'general', setup: true }),
    entry({ key: 'SMTP_HOST', category: 'email', setup: true }),
    entry({ key: 'SMTP_PORT', category: 'email', type: 'int', setup: true, value: 587 }),
    entry({
      key: 'SMTP_PASSWORD',
      category: 'email',
      secret: true,
      setup: true,
      required: true,
      missing: true,
    }),
    entry({
      key: 'ZAI_API_KEY',
      category: 'providers',
      secret: true,
      required: true,
      missing: true,
      used_by: ['glm-4.6'],
    }),
    // Neither for setup nor missing: not on this step.
    entry({ key: 'LOG_LEVEL', category: 'general', value: 'INFO', is_set: true }),
  ],
  missing: ['SMTP_PASSWORD', 'ZAI_API_KEY'],
  pending_restart: [],
  restart_supported: true,
};

const RUNTIME_SETTINGS = {
  settings: [
    {
      key: 'signup_enabled',
      value: true,
      value_type: 'bool',
      default_value: true,
      description: 'Allow new user signups',
    },
    {
      key: 'signup_require_email_verification',
      value: true,
      value_type: 'bool',
      default_value: true,
      description: 'Require email verification for new signups',
    },
  ],
};

function renderPage(siteSaysRequired = true) {
  return render(
    <SiteConfigProvider
      initialConfig={{ ...buildTimeSiteConfig, setup: { required: siteSaysRequired } }}
    >
      <SetupPage />
    </SiteConfigProvider>,
  );
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label, { exact: false }), { target: { value } });
}

interface AdminInput {
  code?: string;
  username?: string;
  displayName?: string;
  password?: string;
  confirm?: string;
}

function fillAdmin({
  code = 'abcd-efgh-jklm',
  username = 'admin',
  displayName = '',
  password = 'Secret123',
  confirm = password,
}: AdminInput = {}) {
  type('Setup code', code);
  type('Username', username);
  if (displayName) type('Display name', displayName);
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } });
  fireEvent.change(screen.getByLabelText('Confirm password'), { target: { value: confirm } });
  fireEvent.click(screen.getByRole('button', { name: 'Create administrator' }));
}

beforeEach(() => {
  vi.mocked(getSetupStatus).mockResolvedValue({ setup_required: true, database_enabled: true });
  vi.mocked(createSetupAdmin).mockResolvedValue({
    access_token: 'new-token',
    token_type: 'bearer',
    expires_in: 900,
    user: { id: 'u1', email: null, login_name: 'admin', role: 'admin', is_admin: true },
  });
  adoptSession.mockResolvedValue(undefined);
  vi.mocked(getConfig).mockResolvedValue(CONFIG);
  vi.mocked(patchConfig).mockResolvedValue(CONFIG);
  vi.mocked(listRuntimeSettings).mockResolvedValue(RUNTIME_SETTINGS);
  vi.mocked(updateRuntimeSetting).mockImplementation(async (key, value) => ({
    key,
    value,
    value_type: 'bool',
    default_value: true,
    description: '',
  }));
  vi.mocked(restartBackend).mockResolvedValue({ restarting: true });
  vi.mocked(waitForBackendRestart).mockResolvedValue('restarted');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('opening /setup', () => {
  it('sends an established deployment to the dashboard', async () => {
    vi.mocked(getSetupStatus).mockResolvedValue({ setup_required: false, database_enabled: true });
    renderPage(false);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith('/dashboard'));
    expect(screen.queryByLabelText('Setup code')).not.toBeInTheDocument();
  });

  it('leaves on a full page load when the page still believes setup is pending', async () => {
    vi.mocked(getSetupStatus).mockResolvedValue({ setup_required: false, database_enabled: true });
    renderPage(true);

    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    expect(navigateToMock).toHaveBeenCalledWith('/dashboard');
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it('offers a retry when the status cannot be read', async () => {
    vi.mocked(getSetupStatus).mockRejectedValueOnce(new APIError('NETWORK_ERROR', 'offline'));
    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not reach the backend/);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByLabelText('Setup code')).toBeInTheDocument();
  });

  it('says where the setup code is', async () => {
    renderPage();

    const code = await screen.findByLabelText('Setup code');
    expect(code).toHaveAccessibleDescription(
      /docker logs hybridinference-backend 2>&1 \| grep 'setup code'.*The code stays the same until setup is complete/,
    );
    expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent(
      'Create administrator',
    );
  });
});

describe('step 1: create administrator', () => {
  it('checks the form before spending a rate-limited attempt', async () => {
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin({
      code: 'ABCD-EFGH',
      username: '-x',
      displayName: 'O',
      password: 'weak',
      confirm: 'other',
    });

    expect(await screen.findByText(/A setup code has 12 letters and digits/)).toBeInTheDocument();
    expect(screen.getByText(/Use 3–32 letters/)).toBeInTheDocument();
    expect(screen.getByText('A display name has 2–50 characters.')).toBeInTheDocument();
    expect(screen.getByText('Password must be at least 8 characters')).toBeInTheDocument();
    expect(screen.getByText('Passwords do not match.')).toBeInTheDocument();
    expect(createSetupAdmin).not.toHaveBeenCalled();
  });

  it('creates the administrator, signs in and moves to the configuration step', async () => {
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin({ displayName: 'Ops' });

    await waitFor(() =>
      expect(createSetupAdmin).toHaveBeenCalledWith({
        setup_code: 'ABCD-EFGH-JKLM',
        login_name: 'admin',
        password: 'Secret123',
        display_name: 'Ops',
      }),
    );
    await waitFor(() => expect(adoptSession).toHaveBeenCalledWith('new-token'));
    expect(await screen.findByRole('region', { name: 'Email (SMTP)' })).toBeInTheDocument();
    expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent('Configure');
  });

  it('marks a wrong setup code on its field', async () => {
    vi.mocked(createSetupAdmin).mockRejectedValueOnce(
      new APIError('HTTP_403', 'Invalid setup code', 403),
    );
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin();

    expect(await screen.findByText(/That setup code is not correct/)).toBeInTheDocument();
    expect(screen.getByLabelText('Setup code')).toHaveAttribute('aria-invalid', 'true');
    expect(adoptSession).not.toHaveBeenCalled();
  });

  it('points to the login page when the deployment is already set up', async () => {
    vi.mocked(createSetupAdmin).mockRejectedValueOnce(
      new APIError('HTTP_409', 'Setup is already complete', 409),
    );
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin();

    fireEvent.click(await screen.findByRole('button', { name: 'Go to login' }));
    // A full load: this page's site configuration still says setup is pending.
    expect(navigateToMock).toHaveBeenCalledWith('/login');
  });

  it('puts a validation error on the field it names', async () => {
    vi.mocked(createSetupAdmin).mockRejectedValueOnce(
      new APIError('VALIDATION_ERROR', 'login_name: reserved', 422, {
        fields: { login_name: 'This username is reserved' },
      }),
    );
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin({ username: 'root' });

    expect(await screen.findByText('This username is reserved')).toBeInTheDocument();
    expect(screen.getByLabelText('Username', { exact: false })).toHaveAttribute(
      'aria-invalid',
      'true',
    );
  });

  it('explains the rate limit', async () => {
    vi.mocked(createSetupAdmin).mockRejectedValueOnce(
      new APIError('RATE_LIMIT_EXCEEDED', 'Too many requests', 429),
    );
    renderPage();
    await screen.findByLabelText('Setup code');

    fillAdmin();

    expect(await screen.findByRole('alert')).toHaveTextContent(/Too many attempts/);
  });
});

async function reachConfigureStep() {
  renderPage();
  await screen.findByLabelText('Setup code');
  fillAdmin();
  await screen.findByRole('region', { name: 'Email (SMTP)' });
}

describe('step 2: configure', () => {
  it('shows the setup and missing settings, grouped by category', async () => {
    await reachConfigureStep();

    expect(screen.getByRole('region', { name: 'General' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Model providers' })).toHaveTextContent('glm-4.6');
    expect(screen.getByLabelText('SMTP_HOST')).toBeInTheDocument();
    expect(screen.getByLabelText('ZAI_API_KEY')).toBeInTheDocument();
    expect(screen.queryByText('LOG_LEVEL')).not.toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Allow public sign-up' })).toBeChecked();
  });

  it('saves sign-up settings and the configuration batch, then finishes', async () => {
    await reachConfigureStep();

    fireEvent.change(screen.getByLabelText('SMTP_HOST'), {
      target: { value: 'smtp.example.test' },
    });
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    fireEvent.change(screen.getByLabelText('ZAI_API_KEY'), { target: { value: 'zai-key' } });
    fireEvent.click(screen.getByRole('switch', { name: 'Require email verification' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save and continue' }));

    await waitFor(() =>
      expect(updateRuntimeSetting).toHaveBeenCalledWith('signup_require_email_verification', false),
    );
    expect(updateRuntimeSetting).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { SMTP_HOST: 'smtp.example.test', SMTP_PORT: 2525, ZAI_API_KEY: 'zai-key' },
      }),
    );
    // Sign-up first: it decides whether SMTP is required.
    expect(vi.mocked(updateRuntimeSetting).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(patchConfig).mock.invocationCallOrder[0],
    );
    expect(await screen.findByRole('button', { name: 'Finish' })).toBeInTheDocument();
  });

  it('re-reads the configuration when nothing changed', async () => {
    await reachConfigureStep();
    vi.mocked(getConfig).mockClear();

    fireEvent.click(screen.getByRole('button', { name: 'Save and continue' }));

    expect(await screen.findByRole('button', { name: 'Finish' })).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
    expect(getConfig).toHaveBeenCalledTimes(1);
  });

  it('stays on the step with the reason when a value is refused', async () => {
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('HTTP_400', 'SMTP_PORT: must be between 1 and 65535', 400),
    );
    await reachConfigureStep();

    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '99999' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and continue' }));

    expect(await screen.findByText('must be between 1 and 65535')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Finish' })).not.toBeInTheDocument();
  });
});

describe('step 3: finish', () => {
  async function reachFinish(saved: Partial<ConfigResponse>) {
    vi.mocked(getConfig)
      .mockResolvedValueOnce(CONFIG)
      .mockResolvedValueOnce({
        ...CONFIG,
        ...saved,
      });
    await reachConfigureStep();
    fireEvent.click(screen.getByRole('button', { name: 'Save and continue' }));
    await screen.findByRole('button', { name: 'Finish' });
  }

  it('restarts the backend when a saved setting needs it, then opens Configuration', async () => {
    await reachFinish({ pending_restart: ['CORS_ALLOWED_ORIGINS'], missing: [] });

    expect(screen.getByRole('region', { name: 'Restart to apply' })).toHaveTextContent(
      'CORS_ALLOWED_ORIGINS',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Restart backend' }));
    const dialog = screen.getByRole('dialog', { name: 'Restart the backend?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart backend' }));

    await waitFor(() => expect(restartBackend).toHaveBeenCalled());
    await waitFor(() =>
      expect(navigateToMock).toHaveBeenCalledWith('/dashboard/admin/configuration'),
    );
  });

  it('gives the manual command when the backend cannot restart itself', async () => {
    await reachFinish({ pending_restart: ['CORS_ALLOWED_ORIGINS'], restart_supported: false });

    expect(screen.queryByRole('button', { name: 'Restart backend' })).not.toBeInTheDocument();
    expect(screen.getByText('docker restart hybridinference-backend')).toBeInTheDocument();
  });

  it('lists what is still missing and finishes with a full page load', async () => {
    await reachFinish({ missing: ['ZAI_API_KEY'] });

    expect(screen.getByText(/Still missing/).closest('[role="status"]')).toHaveTextContent(
      'ZAI_API_KEY',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }));
    expect(navigateToMock).toHaveBeenCalledWith('/dashboard/admin/configuration');
  });
});
