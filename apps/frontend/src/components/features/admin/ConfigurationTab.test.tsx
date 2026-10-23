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
  markConfigSecret: vi.fn(),
  resetConfigKey: vi.fn(),
  testAlertNotification: vi.fn(),
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
  markConfigSecret,
  patchConfig,
  resetConfigKey,
  testAlertNotification,
  restartBackend,
  waitForBackendRestart,
} from '@/lib/api/config';

import { ConfigurationTab } from './ConfigurationTab';

/** What a misbehaving backend might leak; the page must never show it. */
const LEAKED_SECRET = 'sk-live-should-never-render';

/** When a stored row was written. */
const STORED_AT = '2026-10-01T12:00:00Z';

function entry(overrides: Partial<ConfigEntry>): ConfigEntry {
  const source = overrides.source ?? 'database';
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
    source,
    restart_required: false,
    pending_restart: false,
    environment_ignored: false,
    immutable: false,
    setup: false,
    custom: false,
    invalid: null,
    used_by: [],
    // A value in force from the database is a row, written at some time.
    updated_at: source === 'database' ? STORED_AT : null,
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
        // Locked, and leaked by the same misbehaving backend.
        value: LEAKED_SECRET,
        default: LEAKED_SECRET,
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
  vi.mocked(testAlertNotification).mockResolvedValue({
    channel: 'slack',
    sent: true,
    test_id: 'test-123',
  });
  vi.mocked(patchConfig).mockImplementation(async () => makeConfig());
  vi.mocked(markConfigSecret).mockImplementation(async () => makeConfig());
  vi.mocked(resetConfigKey).mockImplementation(async () => makeConfig());
  vi.mocked(restartBackend).mockResolvedValue({ restarting: true, instance: 'old-process' });
  vi.mocked(waitForBackendRestart).mockResolvedValue('restarted');
});

const SLACK_WEBHOOK = 'https://hooks.slack.com/services/test/example/token';
const WECOM_WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-example';

function alertConfig(setKeys: string[] = ['SLACK_ALERTS_WEBHOOK_URL']): ConfigResponse {
  const base = makeConfig();
  return {
    ...base,
    categories: [
      ...base.categories,
      { id: 'alerts', label: 'Alerts', description: 'Backend alerting.' },
    ],
    entries: [
      ...base.entries,
      ...['SLACK_ALERTS_WEBHOOK_URL', 'SLACK_WEBHOOK_URL', 'WECOM_ALERTS_WEBHOOK_URL'].map((key) =>
        entry({
          key,
          category: 'alerts',
          secret: true,
          value: LEAKED_SECRET,
          is_set: setKeys.includes(key),
          restart_required: true,
        }),
      ),
      entry({ key: 'ALERTS_ENABLED', category: 'alerts', type: 'bool', value: true }),
    ],
  };
}

/** Confirm the "Save N changes?" dialog a category's Save opens. */
function confirmSave() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Save' }));
}

function chooseAlertChannel(label: 'None' | 'Slack' | 'WeCom') {
  fireEvent.click(screen.getByRole('combobox', { name: 'Notification channel' }));
  fireEvent.click(screen.getByRole('option', { name: label }));
}

describe('alert notification channel', () => {
  it('infers Slack and shows only its friendly, write-only webhook field', async () => {
    await renderTab(alertConfig());
    const alerts = within(section('Alerts'));
    expect(alerts.getByRole('combobox', { name: 'Notification channel' })).toHaveTextContent(
      'Slack',
    );
    expect(alerts.getByLabelText('Slack webhook URL')).toHaveTextContent('Set');
    expect(alerts.queryByLabelText('WeCom webhook URL')).not.toBeInTheDocument();
    expect(alerts.queryByText('SLACK_WEBHOOK_URL')).not.toBeInTheDocument();
    expect(alerts.queryByText('WECOM_ALERTS_WEBHOOK_URL')).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('switches Slack to WeCom with one write clearing both Slack sources', async () => {
    await renderTab(alertConfig(['SLACK_ALERTS_WEBHOOK_URL', 'SLACK_WEBHOOK_URL']));
    chooseAlertChannel('WeCom');
    expect(screen.queryByLabelText('Slack webhook URL')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('WeCom webhook URL'), {
      target: { value: WECOM_WEBHOOK },
    });
    vi.mocked(patchConfig).mockResolvedValueOnce(alertConfig(['WECOM_ALERTS_WEBHOOK_URL']));
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_ALERTS_WEBHOOK_URL: '',
          SLACK_WEBHOOK_URL: '',
          WECOM_ALERTS_WEBHOOK_URL: WECOM_WEBHOOK,
        },
      }),
    );
    expect(patchConfig).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByLabelText('WeCom webhook URL')).toHaveTextContent('Set'),
    );
    expect(screen.queryByDisplayValue(WECOM_WEBHOOK)).not.toBeInTheDocument();
  });

  it('does not clear the current channel when the chosen replacement is empty', async () => {
    await renderTab(alertConfig());
    chooseAlertChannel('WeCom');
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(screen.getByText('Enter a webhook URL before saving this channel.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send test notification' })).toBeDisabled();
  });

  it('keeps an unsaved webhook replacement when the current channel is reselected', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    chooseAlertChannel('Slack');
    expect(screen.getByLabelText('Slack webhook URL')).toHaveValue(SLACK_WEBHOOK);
    expect(screen.getByRole('button', { name: 'Save Alerts' })).toBeEnabled();
  });

  it('keeps environment, invalid and lock notices in the compact webhook field', async () => {
    const config = alertConfig();
    config.entries = config.entries.map((item) =>
      item.key === 'SLACK_ALERTS_WEBHOOK_URL'
        ? {
            ...item,
            environment_ignored: true,
            invalid: 'The saved webhook was rejected.',
            locked: true,
          }
        : item,
    );
    await renderTab(config);
    const alerts = within(section('Alerts'));
    expect(alerts.getByText(/The environment also sets this variable/)).toBeInTheDocument();
    expect(alerts.getByText(/The stored value could not be applied/)).toBeInTheDocument();
    expect(alerts.getByText(/cannot be changed once set/)).toBeInTheDocument();
    expect(
      alerts.queryByRole('button', { name: 'Replace Slack webhook URL' }),
    ).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('switches WeCom to Slack atomically using the URL that also enables failed-request alerts', async () => {
    await renderTab(alertConfig(['WECOM_ALERTS_WEBHOOK_URL']));
    expect(screen.getByLabelText('Notification channel')).toHaveTextContent('WeCom');
    expect(screen.queryByLabelText('Slack webhook URL')).not.toBeInTheDocument();
    chooseAlertChannel('Slack');
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_WEBHOOK_URL: SLACK_WEBHOOK,
          WECOM_ALERTS_WEBHOOK_URL: '',
        },
      }),
    );
  });

  it('turns notifications off by clearing every URL including the legacy fallback', async () => {
    await renderTab(alertConfig(['SLACK_WEBHOOK_URL']));
    chooseAlertChannel('None');
    expect(screen.queryByLabelText('Slack webhook URL')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Send test notification' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_ALERTS_WEBHOOK_URL: '',
          SLACK_WEBHOOK_URL: '',
          WECOM_ALERTS_WEBHOOK_URL: '',
        },
      }),
    );
  });

  it('keeps an existing legacy Slack URL on its original key when replacing it', async () => {
    await renderTab(alertConfig(['SLACK_WEBHOOK_URL']));
    expect(screen.getByLabelText('Notification channel')).toHaveTextContent('Slack');
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_WEBHOOK_URL: SLACK_WEBHOOK,
          WECOM_ALERTS_WEBHOOK_URL: '',
        },
      }),
    );
  });

  it('discards hidden credential drafts when switching and restores saved selection on Discard', async () => {
    await renderTab(alertConfig());
    chooseAlertChannel('WeCom');
    fireEvent.change(screen.getByLabelText('WeCom webhook URL'), {
      target: { value: WECOM_WEBHOOK },
    });
    chooseAlertChannel('Slack');
    chooseAlertChannel('WeCom');
    expect(screen.getByLabelText('WeCom webhook URL')).toHaveValue('');
    fireEvent.click(within(section('Alerts')).getByRole('button', { name: 'Discard' }));
    expect(screen.getByLabelText('Notification channel')).toHaveTextContent('Slack');
    expect(screen.getByLabelText('Slack webhook URL')).toHaveTextContent('Set');
    expect(screen.queryByRole('button', { name: 'Save Alerts' })).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('preserves channel drafts through filtering and saves hidden category changes together', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('switch', { name: 'ALERTS_ENABLED' }));
    chooseAlertChannel('WeCom');
    fireEvent.change(screen.getByLabelText('WeCom webhook URL'), {
      target: { value: WECOM_WEBHOOK },
    });
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'webhook' } });
    expect(screen.queryByRole('switch', { name: 'ALERTS_ENABLED' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          ALERTS_ENABLED: false,
          SLACK_ALERTS_WEBHOOK_URL: '',
          SLACK_WEBHOOK_URL: '',
          WECOM_ALERTS_WEBHOOK_URL: WECOM_WEBHOOK,
        },
      }),
    );
  });

  it('keeps generic fields for an older backend without WeCom support', async () => {
    const config = alertConfig();
    config.entries = config.entries.filter((item) => item.key !== 'WECOM_ALERTS_WEBHOOK_URL');
    await renderTab(config);
    expect(screen.queryByLabelText('Notification channel')).not.toBeInTheDocument();
    expect(screen.getByLabelText('SLACK_ALERTS_WEBHOOK_URL')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Send test notification' }),
    ).not.toBeInTheDocument();
  });

  it('starts unconfigured with None and no webhook editor', async () => {
    await renderTab(alertConfig([]));
    expect(screen.getByLabelText('Notification channel')).toHaveTextContent('None');
    expect(screen.queryByLabelText('Slack webhook URL')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('WeCom webhook URL')).not.toBeInTheDocument();
  });

  it('configures fresh Slack through the shared URL used by the engine and failed-request detector', async () => {
    await renderTab(alertConfig([]));
    chooseAlertChannel('Slack');
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    vi.mocked(patchConfig).mockResolvedValueOnce(alertConfig(['SLACK_WEBHOOK_URL']));
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_WEBHOOK_URL: SLACK_WEBHOOK,
          WECOM_ALERTS_WEBHOOK_URL: '',
        },
      }),
    );
    expect(patchConfig).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByLabelText('Slack webhook URL')).toHaveTextContent('Set'),
    );
    expect(screen.getByLabelText('Notification channel')).toHaveTextContent('Slack');
  });

  it('tests a saved URL pending restart without fetching or returning the secret', async () => {
    const config = alertConfig(['WECOM_ALERTS_WEBHOOK_URL']);
    config.entries = config.entries.map((item) =>
      item.key === 'WECOM_ALERTS_WEBHOOK_URL' ? { ...item, pending_restart: true } : item,
    );
    config.pending_restart = ['WECOM_ALERTS_WEBHOOK_URL'];
    vi.mocked(testAlertNotification).mockResolvedValueOnce({
      channel: 'wecom',
      sent: true,
      test_id: 'test-wecom',
    });
    await renderTab(config);
    expect(within(section('Alerts')).getAllByText('Pending restart').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    await waitFor(() => expect(testAlertNotification).toHaveBeenCalledWith({ channel: 'wecom' }));
    expect(
      await screen.findByText('Test sent. Check your WeCom channel for test-wecom.'),
    ).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('tests an unsaved replacement and removes the success result when it changes', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    await waitFor(() =>
      expect(testAlertNotification).toHaveBeenCalledWith({
        channel: 'slack',
        webhook_url: SLACK_WEBHOOK,
      }),
    );
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
    expect(patchConfig).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: `${SLACK_WEBHOOK}-new` },
    });
    expect(screen.queryByText(/Test sent/)).not.toBeInTheDocument();
  });

  it('prevents channel changes, saving and repeat sends during a test', async () => {
    let finish!: (value: { channel: 'slack'; sent: true; test_id: string }) => void;
    vi.mocked(testAlertNotification).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: SLACK_WEBHOOK },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    expect(screen.getByLabelText('Notification channel')).toBeDisabled();
    expect(screen.getByLabelText('Slack webhook URL')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save Alerts' })).toBeDisabled();
    expect(within(section('Alerts')).getByRole('button', { name: 'Discard' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Sending test…' }));
    expect(testAlertNotification).toHaveBeenCalledTimes(1);
    finish({ channel: 'slack', sent: true, test_id: 'test-123' });
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
  });

  it('uses the same trimmed webhook URL when testing and saving a replacement', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: `  ${SLACK_WEBHOOK}  ` },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
    expect(testAlertNotification).toHaveBeenCalledWith({
      channel: 'slack',
      webhook_url: SLACK_WEBHOOK,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Alerts' }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: {
          SLACK_ALERTS_WEBHOOK_URL: SLACK_WEBHOOK,
          WECOM_ALERTS_WEBHOOK_URL: '',
        },
      }),
    );
  });

  it('explains test rate limiting without echoing the backend response', async () => {
    vi.mocked(testAlertNotification).mockRejectedValueOnce(
      new APIError('RATE_LIMIT_EXCEEDED', LEAKED_SECRET, 429),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    expect(
      await screen.findByText(
        'Test notifications are temporarily limited. Wait a few seconds and try again.',
      ),
    ).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('shows a safe failure without echoing a rejected secret', async () => {
    vi.mocked(testAlertNotification).mockRejectedValueOnce(
      new APIError('DELIVERY_FAILED', `Failed: ${LEAKED_SECRET}`, 502),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    expect(
      await screen.findByText('The test could not be sent. Check the webhook URL and try again.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Test sent/)).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('tells the administrator to retry a 503 instead of blaming the webhook', async () => {
    vi.mocked(testAlertNotification).mockRejectedValueOnce(
      new APIError('HTTP_503', 'Could not read the saved webhook; retry the test', 503),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    expect(
      await screen.findByText(
        'The saved webhook could not be read. This is a backend problem, not the address; try again in a moment.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Check the webhook URL/)).not.toBeInTheDocument();
  });

  it('repeats the backend reason for a rejected draft address without echoing it', async () => {
    vi.mocked(testAlertNotification).mockRejectedValueOnce(
      new APIError('HTTP_400', 'Enter a valid WeCom incoming webhook URL', 400),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    expect(await screen.findByText('Enter a valid WeCom incoming webhook URL')).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('offers Reset for a channel an earlier save turned off, and calls DELETE', async () => {
    // Slack is None now, but both Slack rows still hold the empty value that a
    // previous "None" save wrote, and that value hides SLACK_WEBHOOK_URL in .env.
    const config = alertConfig([]);
    config.entries = config.entries.map((item) =>
      item.key === 'SLACK_WEBHOOK_URL'
        ? { ...item, is_set: false, value: null, updated_at: STORED_AT }
        : item,
    );
    await renderTab(config);
    const alerts = within(section('Alerts'));
    expect(alerts.getByText(/empty stored value also hides any webhook/)).toBeInTheDocument();

    fireEvent.click(alerts.getByRole('button', { name: 'Reset SLACK_WEBHOOK_URL' }));
    const dialog = screen.getByRole('dialog', { name: 'Reset SLACK_WEBHOOK_URL?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SLACK_WEBHOOK_URL'));
  });

  it('does not offer Reset for a channel that never had a stored row', async () => {
    // No row at all: the value comes from the environment or the default, so
    // there is nothing to remove. Only a database row earns a Reset.
    const config = alertConfig();
    config.entries = config.entries.map((item) =>
      item.key === 'WECOM_ALERTS_WEBHOOK_URL'
        ? { ...item, is_set: false, value: null, source: 'default', updated_at: null }
        : item,
    );
    await renderTab(config);
    const alerts = within(section('Alerts'));
    expect(alerts.queryByRole('button', { name: 'Reset WECOM_ALERTS_WEBHOOK_URL' })).toBeNull();
  });

  it('offers Reset for the rows a saved None left empty', async () => {
    // What the backend returns after "None" was saved: the rows exist but are
    // empty, which is the state that hides the environment.
    const config = alertConfig([]);
    config.entries = config.entries.map((item) =>
      ['SLACK_ALERTS_WEBHOOK_URL', 'SLACK_WEBHOOK_URL', 'WECOM_ALERTS_WEBHOOK_URL'].includes(
        item.key,
      )
        ? { ...item, is_set: false, value: null, updated_at: STORED_AT }
        : item,
    );
    await renderTab(config);
    const alerts = within(section('Alerts'));
    expect(
      alerts.getByRole('button', { name: 'Reset SLACK_ALERTS_WEBHOOK_URL' }),
    ).toBeInTheDocument();
    expect(alerts.getByRole('button', { name: 'Reset SLACK_WEBHOOK_URL' })).toBeInTheDocument();
    expect(
      alerts.getByRole('button', { name: 'Reset WECOM_ALERTS_WEBHOOK_URL' }),
    ).toBeInTheDocument();

    fireEvent.click(alerts.getByRole('button', { name: 'Reset SLACK_ALERTS_WEBHOOK_URL' }));
    const dialog = screen.getByRole('dialog', { name: 'Reset SLACK_ALERTS_WEBHOOK_URL?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SLACK_ALERTS_WEBHOOK_URL'));
  });

  it('does not offer Reset before the save that empties a channel', async () => {
    await renderTab(alertConfig(['SLACK_ALERTS_WEBHOOK_URL']));
    chooseAlertChannel('None');
    const alerts = within(section('Alerts'));
    expect(alerts.queryByRole('button', { name: 'Reset SLACK_ALERTS_WEBHOOK_URL' })).toBeNull();
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('secrets', () => {
  it('never renders a secret value, even one the backend sent by mistake', async () => {
    await renderTab();

    // Not as text, and not in an attribute (a value, a placeholder) either.
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
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
    confirmSave();

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
    confirmSave();

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

describe('marking a variable secret', () => {
  const HEADER_VALUE = 'plain-header-value';

  /**
   * The usual configuration, built in, plus two stored variables: one an
   * administrator added, one found in models.yaml.
   */
  function withVariables(): ConfigResponse {
    const base = makeConfig();
    return {
      ...base,
      entries: [
        ...base.entries.map((builtIn) => ({ ...builtIn, origin: 'static' as const })),
        entry({
          key: 'MY_ROUTE_HEADER',
          category: 'providers',
          description: 'Added by an administrator for a reference in the model registry.',
          origin: 'custom',
          custom: true,
          value: HEADER_VALUE,
          restart_required: true,
        }),
        entry({
          key: 'ROUTE_REGION',
          category: 'providers',
          description: 'Referenced by the model registry.',
          origin: 'discovered',
          value: 'eu-west',
          restart_required: true,
          used_by: ['glm-4.6'],
        }),
      ],
    };
  }

  /** `config` once `key` is secret; `leak` keeps its value, as a buggy backend might. */
  function markedIn(config: ConfigResponse, key: string, leak = false): ConfigResponse {
    return {
      ...config,
      entries: config.entries.map((candidate) =>
        candidate.key === key
          ? { ...candidate, secret: true, value: leak ? candidate.value : null }
          : candidate,
      ),
    };
  }

  function confirmMarking(key: string): HTMLElement {
    fireEvent.click(screen.getByRole('button', { name: `Mark ${key} as secret` }));
    const dialog = screen.getByRole('dialog', { name: `Mark ${key} as secret?` });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Mark as secret' }));
    return dialog;
  }

  it('is offered for stored variables, not for built-in settings or secrets', async () => {
    await renderTab(withVariables());

    expect(screen.getByRole('button', { name: 'Mark MY_ROUTE_HEADER as secret' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Mark ROUTE_REGION as secret' })).toBeVisible();
    // SMTP_PORT is stored too, but a built-in setting's secret flag is fixed.
    expect(screen.getAllByRole('button', { name: /^Mark .* as secret$/ })).toHaveLength(2);
  });

  it('is not offered by a backend that does not say where entries come from', async () => {
    const config = withVariables();
    await renderTab({
      ...config,
      entries: config.entries.map(({ origin: _origin, ...older }) => older),
    });

    expect(screen.queryByRole('button', { name: /^Mark .* as secret$/ })).toBeNull();
  });

  it('is offered on a reset row, which keeps the flag but has nothing to Reset', async () => {
    await renderTab(
      makeConfig({
        entries: [
          entry({ key: 'SMTP_HOST', category: 'email', value: 'mx.test', origin: 'static' }),
          entry({
            key: 'ROUTE_ZONE',
            category: 'providers',
            description: 'Referenced by the model registry.',
            origin: 'discovered',
            // Reset: the environment's value applies, and the row remains.
            source: 'environment',
            value: 'zone-a',
            updated_at: STORED_AT,
          }),
        ],
        missing: [],
        pending_restart: [],
      }),
    );
    const providers = section('Providers');

    expect(
      within(providers).getByRole('button', { name: 'Mark ROUTE_ZONE as secret' }),
    ).toBeVisible();
    expect(within(providers).queryByRole('button', { name: 'Reset ROUTE_ZONE' })).toBeNull();
  });

  it('marks it after a warning, and drops its plaintext with any unsaved edit', async () => {
    const config = withVariables();
    vi.mocked(markConfigSecret).mockResolvedValueOnce(markedIn(config, 'MY_ROUTE_HEADER', true));
    await renderTab(config);
    const providers = section('Providers');
    const field = within(providers).getByLabelText('MY_ROUTE_HEADER');
    expect(field).toHaveValue(HEADER_VALUE);
    fireEvent.change(field, { target: { value: 'typed-but-unsaved' } });

    fireEvent.click(
      within(providers).getByRole('button', { name: 'Mark MY_ROUTE_HEADER as secret' }),
    );
    const dialog = screen.getByRole('dialog', { name: 'Mark MY_ROUTE_HEADER as secret?' });
    expect(dialog).toHaveTextContent('This cannot be undone from the console.');
    expect(dialog).toHaveTextContent('Your unsaved change to it is discarded.');
    expect(markConfigSecret).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Mark as secret' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(markConfigSecret).toHaveBeenCalledWith('MY_ROUTE_HEADER');
    expect(patchConfig).not.toHaveBeenCalled();
    // Neither the stored value (still in the buggy response) nor the edit.
    for (const plaintext of [HEADER_VALUE, 'typed-but-unsaved']) {
      expect(screen.queryByDisplayValue(plaintext)).not.toBeInTheDocument();
      expect(document.body.innerHTML).not.toContain(plaintext);
    }
    const after = section('Providers');
    expect(within(after).getByRole('button', { name: 'Replace MY_ROUTE_HEADER' })).toBeVisible();
    expect(within(after).queryByRole('button', { name: /^Mark MY_ROUTE_HEADER/ })).toBeNull();
    expect(within(after).queryByRole('button', { name: 'Save Providers' })).toBeNull();
    expect(refreshMock).toHaveBeenCalled();
  });

  it('does not claim success when the variable comes back unchanged', async () => {
    const config = withVariables();
    vi.mocked(markConfigSecret).mockResolvedValueOnce(config);
    await renderTab(config);

    const dialog = confirmMarking('ROUTE_REGION');

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The backend answered, but ROUTE_REGION is still not secret.',
    );
    expect(screen.getByLabelText('ROUTE_REGION')).toHaveValue('eu-west');
    expect(refreshMock).not.toHaveBeenCalled();
  });

  // The backend's 400s for a flag-only change, shown as they are.
  it.each([
    'ROUTE_REGION has no stored value to protect yet: set a value and mark it secret in the same save.',
    "ROUTE_REGION: a built-in setting keeps the registry's secret flag.",
  ])('keeps the dialog open with the reason the backend gave: %s', async (message) => {
    vi.mocked(markConfigSecret).mockRejectedValueOnce(new APIError('HTTP_400', message, 400));
    await renderTab(withVariables());

    const dialog = confirmMarking('ROUTE_REGION');

    expect((await within(dialog).findByRole('alert')).textContent).toBe(message);
    // Nothing changed on the page.
    expect(screen.getByLabelText('ROUTE_REGION')).toHaveValue('eu-west');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('locked settings', () => {
  function securityOnly(entries: ConfigEntry[]): ConfigResponse {
    return makeConfig({ entries, missing: [], pending_restart: [] });
  }

  it('shows a locked setting without an editor or Reset, even an empty one', async () => {
    vi.mocked(getConfig).mockResolvedValue(
      securityOnly([
        // The erasure fence already runs on API_KEY_SECRET.
        entry({
          key: 'ERASURE_FENCE_SECRET',
          category: 'security',
          description: 'Empty uses API_KEY_SECRET.',
          secret: true,
          immutable: true,
          locked: true,
          is_set: false,
          value: null,
          source: 'default',
        }),
        entry({ key: 'PINNED', category: 'security', value: 'v1', locked: true }),
      ]),
    );
    render(<ConfigurationTab />);
    const security = await screen.findByRole('region', { name: 'Security' });

    const fence = within(security).getByLabelText('ERASURE_FENCE_SECRET');
    expect(fence.tagName).toBe('OUTPUT');
    expect(fence).toHaveTextContent('Not set · can no longer be set');
    expect(within(security).getByLabelText('PINNED')).toHaveTextContent(
      'v1 · cannot be changed once set',
    );
    // No Replace, Clear, Reset or Mark as secret, and nothing to type into.
    expect(within(security).queryByRole('button')).toBeNull();
    expect(within(security).queryByRole('textbox')).toBeNull();
    expect(within(security).queryByText('Cannot be changed once it is set.')).toBeNull();
  });

  it('takes the backend at its word when an immutable setting is not locked yet', async () => {
    vi.mocked(getConfig).mockResolvedValue(
      securityOnly([
        entry({
          key: 'ERASURE_FENCE_SECRET',
          category: 'security',
          secret: true,
          immutable: true,
          locked: false,
          is_set: false,
          value: null,
          source: 'default',
        }),
      ]),
    );
    render(<ConfigurationTab />);
    const security = await screen.findByRole('region', { name: 'Security' });

    expect(within(security).getByLabelText('ERASURE_FENCE_SECRET')).toHaveAttribute(
      'type',
      'password',
    );
    expect(within(security).getByText('Cannot be changed once it is set.')).toBeVisible();
  });
});

describe('forms', () => {
  it('keeps what is typed out of password managers', async () => {
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.click(within(email).getByRole('button', { name: 'Replace SMTP_PASSWORD' }));
    const secret = within(email).getByLabelText('SMTP_PASSWORD');
    expect(secret.closest('form')).toHaveAttribute('autocomplete', 'off');
    expect(secret).toHaveAttribute('autocomplete', 'new-password');
    expect(secret).toHaveAttribute('data-1p-ignore', 'true');
    expect(secret).toHaveAttribute('data-lpignore', 'true');
    expect(secret).toHaveAttribute('data-bwignore', 'true');
    expect(secret).toHaveAttribute('data-form-type', 'other');
    // Nor is the field before it a username.
    expect(within(email).getByLabelText('SMTP_HOST')).toHaveAttribute('autocomplete', 'off');

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    expect(form).toHaveAttribute('autocomplete', 'off');
    expect(within(form).getByLabelText('Name')).toHaveAttribute('autocomplete', 'off');
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'MY_API_KEY' } });
    const value = within(form).getByLabelText('Value');
    expect(value).toHaveAttribute('type', 'password');
    expect(value).toHaveAttribute('autocomplete', 'new-password');
    expect(value).toHaveAttribute('data-1p-ignore', 'true');
  });

  it('asks for confirmation, listing each change, before anything is saved', async () => {
    await renderTab();
    const email = section('Email (SMTP)');
    fireEvent.change(within(email).getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));

    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Email (SMTP)?' });
    expect(within(dialog).getByText('SMTP_PORT')).toBeInTheDocument();
    expect(within(dialog).getByText(/→ 587$/)).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();

    // Cancel writes nothing and keeps the edit, so it can still be saved.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(within(email).getByLabelText('SMTP_PORT')).toHaveValue(587);

    fireEvent.click(within(email).getByRole('button', { name: 'Save Email (SMTP)' }));
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } }));
  });

  it('saves a category on Enter, after confirmation', async () => {
    await renderTab();
    const email = section('Email (SMTP)');
    const port = within(email).getByLabelText('SMTP_PORT');
    fireEvent.change(port, { target: { value: '587' } });

    // Enter in a field submits its form: the Save button is that form's.
    const save = within(email).getByRole('button', { name: 'Save Email (SMTP)' });
    expect(save).toHaveAttribute('type', 'submit');
    expect(save.closest('form')).toBe(port.closest('form'));
    fireEvent.submit(port.closest('form') as HTMLFormElement);
    confirmSave();

    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } }));
  });

  it('adds a variable on Enter', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'MY_REGION' } });
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'eu' } });

    fireEvent.submit(form);

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_REGION: 'eu' },
        secrets: { MY_REGION: false },
      }),
    );
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
    confirmSave();
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
    confirmSave();
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
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
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
    confirmSave();

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

  it('resets a stored value the backend refused', async () => {
    // The refused row: the default applies in its place, and says so.
    await renderTab(
      makeConfig({
        entries: [
          entry({
            key: 'SMTP_PORT',
            category: 'email',
            type: 'int',
            value: 587,
            default: 587,
            source: 'default',
            invalid: 'must be between 1 and 65535',
            updated_at: STORED_AT,
          }),
        ],
        missing: [],
        pending_restart: [],
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));
    fireEvent.click(
      within(screen.getByRole('dialog', { name: 'Reset SMTP_PORT?' })).getByRole('button', {
        name: 'Reset',
      }),
    );

    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SMTP_PORT'));
  });

  it('offers no Reset where nothing is stored, or only a reset row', async () => {
    await renderTab(
      makeConfig({
        entries: [
          entry({ key: 'SMTP_HOST', category: 'email', value: 'mx.test', source: 'environment' }),
          // Already reset: the environment applies, and the row stores nothing.
          entry({
            key: 'SMTP_FROM_NAME',
            category: 'email',
            value: 'Ops',
            source: 'environment',
            updated_at: STORED_AT,
          }),
        ],
        missing: [],
        pending_restart: [],
      }),
    );

    expect(screen.queryByRole('button', { name: /^Reset / })).toBeNull();
  });

  it("adds a custom variable, always secret when its name looks like a credential's", async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), {
      target: { value: 'my_provider_api_key' },
    });
    expect(within(form).getByLabelText('Name')).toHaveValue('MY_PROVIDER_API_KEY');
    // The backend makes such a name secret whatever the request says.
    const box = within(form).getByRole('checkbox');
    expect(box).toBeChecked();
    expect(box).toBeDisabled();
    expect(box).toHaveAccessibleDescription('A name like this one is always secret.');
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'k-123' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_PROVIDER_API_KEY: 'k-123' },
        secrets: { MY_PROVIDER_API_KEY: true },
      }),
    );
  });

  it.each(['sentry_dsn', 'vertex_sa_json', 'upstream_bearer', 'ftp_pass', 'basic_auth'])(
    "treats %s as secret, as the backend's word list does",
    async (name) => {
      await renderTab();

      fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
      const form = screen.getByRole('form', { name: 'Add variable' });
      fireEvent.change(within(form).getByLabelText('Name'), { target: { value: name } });

      expect(within(form).getByRole('checkbox')).toBeChecked();
      expect(within(form).getByRole('checkbox')).toBeDisabled();
      expect(within(form).getByLabelText('Value')).toHaveAttribute('type', 'password');
    },
  );

  it('lets the administrator make any other variable secret', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'MY_REGION' } });
    const box = within(form).getByRole('checkbox');
    expect(box).not.toBeChecked();
    expect(box).toBeEnabled();
    expect(within(form).getByLabelText('Value')).toHaveAttribute('type', 'text');

    fireEvent.click(box);
    expect(within(form).getByLabelText('Value')).toHaveAttribute('type', 'password');
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'eu' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_REGION: 'eu' },
        secrets: { MY_REGION: true },
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

  it('keeps waiting for the same restart after a timeout', async () => {
    // The backend went down during the first wait and was not back in time.
    vi.mocked(waitForBackendRestart).mockImplementationOnce(async (options) => {
      if (options?.progress) options.progress.sawDown = true;
      return 'timeout';
    });
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Restart backend' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Restart backend' }),
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Keep waiting' }));

    await waitFor(() =>
      expect(navigateToMock).toHaveBeenCalledWith('/dashboard/admin/configuration'),
    );
    const [first, second] = vi.mocked(waitForBackendRestart).mock.calls;
    expect(second[0]?.progress).toBe(first[0]?.progress);
    // The process asked to restart, which only a different one ends.
    expect(second[0]?.progress).toEqual({ sawDown: true, from: 'old-process' });
    expect(restartBackend).toHaveBeenCalledTimes(1);
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
