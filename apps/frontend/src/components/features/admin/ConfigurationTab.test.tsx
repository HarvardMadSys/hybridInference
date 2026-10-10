// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ConfigEntry, ConfigResponse } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';

const refreshMock = vi.fn();
const navigateToMock = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ refresh: refreshMock, replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/dashboard/admin/configuration',
}));

vi.mock('@/lib/api/config', () => ({
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  resetConfigKey: vi.fn(),
  restartBackend: vi.fn(),
  waitForBackendRestart: vi.fn(),
}));

vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import {
  getConfig,
  patchConfig,
  resetConfigKey,
  restartBackend,
  waitForBackendRestart,
} from '@/lib/api/config';

import { ConfigurationTab } from './ConfigurationTab';

/** What a misbehaving backend might leak; the page must never show it. */
const LEAKED_SECRET = 'sk-live-should-never-render';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  return {
    key: 'SETTING',
    category: 'general',
    description: '',
    type: 'str',
    secret: false,
    required: false,
    missing: false,
    is_set: true,
    value: '',
    default: null,
    source: 'database',
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

function makeConfig(overrides: Partial<ConfigResponse> = {}): ConfigResponse {
  return {
    categories: [
      { id: 'email', label: 'Email (SMTP)', description: 'Outgoing mail.' },
      { id: 'network', label: 'Network', description: 'Proxies and CORS.' },
      { id: 'providers', label: 'Providers', description: 'Model credentials.' },
      { id: 'security', label: 'Security', description: 'Secrets.' },
    ],
    entries: [
      entry({
        key: 'SMTP_PASSWORD',
        category: 'email',
        description: 'SMTP password.',
        secret: true,
        value: LEAKED_SECRET,
        environment_ignored: true,
      }),
      entry({ key: 'SMTP_PORT', category: 'email', type: 'int', value: 25, default: 587 }),
      entry({
        key: 'SMTP_HOST',
        category: 'email',
        required: true,
        missing: true,
        is_set: false,
        value: null,
        source: 'default',
      }),
      entry({ key: 'TRUST_PROXY_HEADERS', category: 'network', type: 'bool', value: false }),
      entry({
        key: 'CORS_ALLOWED_ORIGINS',
        category: 'network',
        type: 'list',
        value: 'https://a.test',
        restart_required: true,
        pending_restart: true,
        invalid: 'not a valid origin: ftp://b.test',
      }),
      entry({
        key: 'ZAI_API_KEY',
        category: 'providers',
        secret: true,
        required: true,
        missing: true,
        is_set: false,
        value: null,
        source: 'default',
        used_by: ['glm-4.6'],
      }),
      entry({
        key: 'API_KEY_SECRET',
        category: 'security',
        secret: true,
        immutable: true,
        value: null,
        source: 'database',
      }),
    ],
    missing: ['SMTP_HOST', 'ZAI_API_KEY'],
    pending_restart: ['CORS_ALLOWED_ORIGINS'],
    restart_supported: true,
    ...overrides,
  };
}

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

async function renderTab(config = makeConfig(), props: { initialMissingOnly?: boolean } = {}) {
  vi.mocked(getConfig).mockResolvedValue(config);
  render(<ConfigurationTab {...props} />);
  await screen.findByRole('region', { name: 'Email (SMTP)' });
}

beforeEach(() => {
  vi.mocked(patchConfig).mockImplementation(async () => makeConfig());
  vi.mocked(resetConfigKey).mockImplementation(async () => makeConfig());
  vi.mocked(restartBackend).mockResolvedValue({ restarting: true });
  vi.mocked(waitForBackendRestart).mockResolvedValue('restarted');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('secrets', () => {
  it('never renders a secret value, even one the backend sent by mistake', async () => {
    await renderTab();

    expect(document.body.textContent).not.toContain(LEAKED_SECRET);
    expect(screen.queryByDisplayValue(LEAKED_SECRET)).not.toBeInTheDocument();
    const email = section('Email (SMTP)');
    expect(within(email).getByText('Set')).toBeInTheDocument();
    // A secret with no value is open for typing, with nothing to reveal.
    expect(within(section('Providers')).getByLabelText('ZAI_API_KEY')).toHaveAttribute(
      'placeholder',
      'Not set',
    );
  });

  it('replaces a secret write-only: the typed value is sent once and not shown again', async () => {
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.click(within(email).getByRole('button', { name: 'Replace SMTP_PASSWORD' }));
    const input = within(email).getByLabelText('SMTP_PASSWORD');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: 'new-smtp-secret' } });
    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PASSWORD: 'new-smtp-secret' } }),
    );
    await waitFor(() =>
      expect(screen.queryByDisplayValue('new-smtp-secret')).not.toBeInTheDocument(),
    );
    expect(refreshMock).toHaveBeenCalled();
  });

  it('clears a secret by sending the empty string', async () => {
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.click(within(email).getByRole('button', { name: 'Clear SMTP_PASSWORD' }));
    expect(within(email).getByText('Cleared when you save')).toBeInTheDocument();
    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PASSWORD: '' } }),
    );
  });

  it('offers no edit for an immutable secret that is set', async () => {
    await renderTab();
    const security = section('Security');

    expect(within(security).getByText(/cannot be changed once set/)).toBeInTheDocument();
    expect(within(security).queryByRole('button', { name: /API_KEY_SECRET/ })).toBeNull();
  });
});

describe('editors', () => {
  it('sends numbers and booleans as their JSON types, one batch per category', async () => {
    await renderTab();

    fireEvent.change(within(section('Email (SMTP)')).getByLabelText('SMTP_PORT'), {
      target: { value: '587' },
    });
    fireEvent.change(within(section('Email (SMTP)')).getByLabelText('SMTP_HOST'), {
      target: { value: 'smtp.example.test' },
    });
    fireEvent.click(within(section('Email (SMTP)')).getByRole('button', { name: /^Save/ }));
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { SMTP_PORT: 587, SMTP_HOST: 'smtp.example.test' },
      }),
    );

    fireEvent.click(
      within(section('Network')).getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }),
    );
    fireEvent.change(within(section('Network')).getByLabelText('CORS_ALLOWED_ORIGINS'), {
      target: { value: 'https://a.test,https://b.test' },
    });
    fireEvent.click(within(section('Network')).getByRole('button', { name: 'Save Network' }));
    await waitFor(() =>
      expect(patchConfig).toHaveBeenLastCalledWith({
        values: {
          TRUST_PROXY_HEADERS: true,
          CORS_ALLOWED_ORIGINS: 'https://a.test,https://b.test',
        },
      }),
    );
    expect(refreshMock).toHaveBeenCalledTimes(2);
  });

  it('refuses a value that is not a whole number without calling the API', async () => {
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.change(within(email).getByLabelText('SMTP_PORT'), { target: { value: '25.5' } });

    expect(within(email).getByText('Enter a whole number.')).toBeInTheDocument();
    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('shows the reason the server gave, on the setting it names', async () => {
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('HTTP_400', 'SMTP_PORT: must be between 1 and 65535', 400),
    );
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.change(within(email).getByLabelText('SMTP_PORT'), { target: { value: '70000' } });
    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));

    expect(await within(email).findByText('SMTP_PORT: must be between 1 and 65535')).toBeVisible();
    expect(within(email).getByText('must be between 1 and 65535')).toBeVisible();
    // Nothing was saved, so the edit is still there to correct.
    expect(within(email).getByLabelText('SMTP_PORT')).toHaveValue(70000);
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it('explains environment overrides, model references and invalid values', async () => {
    await renderTab();

    expect(
      screen.getByText(
        'The environment also sets this variable; the database value wins. Remove it from .env.',
      ),
    ).toBeInTheDocument();
    expect(within(section('Providers')).getByText('glm-4.6')).toBeInTheDocument();
    expect(
      screen.getByText('The stored value could not be applied: not a valid origin: ftp://b.test'),
    ).toBeInTheDocument();
    for (const badge of [
      'Missing',
      'Required',
      'Secret',
      'Pending restart',
      'Environment ignored',
      'Invalid',
      'Immutable',
    ]) {
      expect(screen.getAllByText(badge).length).toBeGreaterThan(0);
    }
  });
});

describe('badges', () => {
  it('says a value comes from the environment only when there is one', async () => {
    await renderTab(
      makeConfig({
        entries: [
          entry({
            key: 'OPENROUTER_API_KEY',
            category: 'providers',
            secret: true,
            required: true,
            missing: true,
            is_set: false,
            value: null,
            // The variable exists but is empty.
            source: 'environment',
          }),
          entry({
            key: 'SMTP_HOST',
            category: 'email',
            value: 'smtp.example.org',
            source: 'environment',
          }),
        ],
        missing: ['OPENROUTER_API_KEY'],
        pending_restart: [],
      }),
    );

    const providers = section('Providers');
    expect(within(providers).getByText('Missing')).toBeInTheDocument();
    expect(within(providers).queryByText('From environment')).not.toBeInTheDocument();
    expect(within(section('Email (SMTP)')).getByText('From environment')).toBeInTheDocument();
  });
});

describe('reset and custom variables', () => {
  it('resets a setting with DELETE after confirming', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));
    const dialog = screen.getByRole('dialog', { name: 'Reset SMTP_PORT?' });
    expect(dialog).toHaveTextContent('falls back to the environment, then to its default');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' }));

    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SMTP_PORT'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(refreshMock).toHaveBeenCalled();
  });

  it('adds a custom variable, secret by its name unless told otherwise', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), {
      target: { value: 'my_provider_api_key' },
    });
    expect(within(form).getByLabelText('Name')).toHaveValue('MY_PROVIDER_API_KEY');
    expect(within(form).getByRole('checkbox')).toBeChecked();
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'k-123' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_PROVIDER_API_KEY: 'k-123' },
        secrets: { MY_PROVIDER_API_KEY: true },
      }),
    );
  });

  it('rejects a name that is not an environment variable, or one already listed', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: '1BAD' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent(/environment-variable name/);

    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'SMTP_PORT' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent('already listed');
    expect(patchConfig).not.toHaveBeenCalled();
  });
});

describe('filters', () => {
  it('narrows to missing settings and to a search', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Missing only' }));
    expect(screen.getByLabelText('SMTP_HOST')).toBeInTheDocument();
    expect(screen.getByLabelText('ZAI_API_KEY')).toBeInTheDocument();
    expect(screen.queryByLabelText('SMTP_PORT')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Network' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('checkbox', { name: 'Missing only' }));
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'glm' } });
    expect(screen.getByLabelText('ZAI_API_KEY')).toBeInTheDocument();
    expect(screen.queryByLabelText('SMTP_HOST')).not.toBeInTheDocument();
  });

  it('opens filtered to missing settings when linked that way', async () => {
    await renderTab(makeConfig(), { initialMissingOnly: true });

    expect(screen.getByRole('checkbox', { name: 'Missing only' })).toBeChecked();
    expect(screen.queryByLabelText('SMTP_PORT')).not.toBeInTheDocument();
  });
});

describe('restart', () => {
  it('restarts after a warning, waits for the backend, then reloads the tab', async () => {
    await renderTab();
    const banner = screen.getByRole('region', { name: 'Restart pending' });
    expect(banner).toHaveTextContent('CORS_ALLOWED_ORIGINS');

    fireEvent.click(within(banner).getByRole('button', { name: 'Restart backend' }));
    const dialog = screen.getByRole('dialog', { name: 'Restart the backend?' });
    expect(dialog).toHaveTextContent('Requests in flight');
    expect(restartBackend).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart backend' }));

    await waitFor(() => expect(restartBackend).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(navigateToMock).toHaveBeenCalledWith('/dashboard/admin/configuration'),
    );
    expect(waitForBackendRestart).toHaveBeenCalled();
  });

  it('says so when the backend does not come back', async () => {
    vi.mocked(waitForBackendRestart).mockResolvedValueOnce('timeout');
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Restart backend' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Restart backend' }),
    );

    expect(await screen.findByText(/has not come back after two minutes/)).toBeInTheDocument();
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it('keeps the dialog open with the reason when the restart is refused', async () => {
    vi.mocked(restartBackend).mockRejectedValueOnce(
      new APIError('HTTP_409', 'Restart is not supported here', 409),
    );
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Restart backend' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart backend' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Restart is not supported here',
    );
    expect(waitForBackendRestart).not.toHaveBeenCalled();
  });

  it('gives the manual command when the backend cannot restart itself', async () => {
    await renderTab(makeConfig({ restart_supported: false }));

    expect(screen.queryByRole('button', { name: 'Restart backend' })).not.toBeInTheDocument();
    expect(screen.getByText('docker restart hybridinference-backend')).toBeInTheDocument();
  });
});

describe('loading', () => {
  it('explains a backend without the configuration API', async () => {
    vi.mocked(getConfig).mockRejectedValueOnce(new APIError('HTTP_404', 'Not Found', 404));
    render(<ConfigurationTab />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'does not support database-backed configuration',
    );
  });
});
