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
  exportConfigBackup: vi.fn(),
  restoreConfigBackup: vi.fn(),
  testAlertNotification: vi.fn(),
  restartBackend: vi.fn(),
  waitForBackendRestart: vi.fn(),
  getLiveConfigStatus: vi.fn(),
}));

vi.mock('@/lib/utils/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/utils/navigation')>()),
  navigateTo: (url: string) => navigateToMock(url),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import {
  exportConfigBackup,
  getConfig,
  getLiveConfigStatus,
  markConfigSecret,
  patchConfig,
  resetConfigKey,
  restoreConfigBackup,
  testAlertNotification,
  restartBackend,
  waitForBackendRestart,
} from '@/lib/api/config';
import toast from 'react-hot-toast';

import { ConfigurationTab } from './ConfigurationTab';
import { escapingScreenReaderText } from './__tests__/layoutContainment';

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
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }));
}

/** Save a category's unsaved changes, and confirm the list of them. */
function saveAndConfirm(name: string) {
  fireEvent.click(screen.getByRole('button', { name: `Save changes to ${name}` }));
  confirmSave();
}

/** Confirm the test-send dialog that Send test notification opens. */
function confirmTestSend() {
  const dialog = screen.getByRole('dialog', { name: /^Send a test notification to / });
  expect(testAlertNotification).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Send test' }));
}

/** Confirm the review dialog that Add variable's submit opens. */
function confirmAddVariable() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add variable' }));
}

function chooseAlertChannel(label: 'None' | 'Slack' | 'WeCom') {
  fireEvent.click(screen.getByRole('combobox', { name: 'Notification channel' }));
  fireEvent.click(screen.getByRole('option', { name: label }));
}

describe('alert notification channel', () => {
  it('infers Slack and shows only its friendly, write-only webhook field', async () => {
    await renderTab(alertConfig());
    const alerts = within(section('Alerts'));
    // Editable in place: the channel is offered, and the saved webhook says only that it is set.
    expect(alerts.getByRole('combobox', { name: 'Notification channel' })).toHaveTextContent(
      'Slack',
    );
    expect(alerts.getByLabelText('Slack webhook URL')).toHaveTextContent('Set');
    expect(alerts.getByRole('button', { name: 'Replace Slack webhook URL' })).toBeEnabled();
    expect(alerts.queryByRole('button', { name: /changes to Alerts$/ })).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    expect(screen.getByRole('button', { name: 'Save changes to Alerts' })).toBeEnabled();
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    fireEvent.click(
      within(section('Alerts')).getByRole('button', { name: 'Discard changes to Alerts' }),
    );
    expect(screen.getByRole('combobox', { name: 'Notification channel' })).toHaveTextContent(
      'Slack',
    );
    expect(screen.getByLabelText('Slack webhook URL')).toHaveTextContent('Set');
    expect(
      screen.queryByRole('button', { name: 'Save changes to Alerts' }),
    ).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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
    confirmTestSend();
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
    confirmTestSend();
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
    confirmTestSend();
    expect(screen.getByLabelText('Notification channel')).toBeDisabled();
    expect(screen.getByLabelText('Slack webhook URL')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save changes to Alerts' })).toBeDisabled();
    expect(
      within(section('Alerts')).getByRole('button', { name: 'Discard changes to Alerts' }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Sending test…' }));
    expect(testAlertNotification).toHaveBeenCalledTimes(1);
    finish({ channel: 'slack', sent: true, test_id: 'test-123' });
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
  });

  it('shows no save bar while the saved webhook is tested: a test changes nothing', async () => {
    let finish!: (value: { channel: 'slack'; sent: true; test_id: string }) => void;
    vi.mocked(testAlertNotification).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    confirmTestSend();
    expect(screen.getByRole('button', { name: 'Sending test…' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /changes to Alerts$/ })).not.toBeInTheDocument();
    finish({ channel: 'slack', sent: true, test_id: 'test-123' });
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
    expect(screen.queryByRole('button', { name: /changes to Alerts$/ })).not.toBeInTheDocument();
  });

  it('uses the same trimmed webhook URL when testing and saving a replacement', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Replace Slack webhook URL' }));
    fireEvent.change(screen.getByLabelText('Slack webhook URL'), {
      target: { value: `  ${SLACK_WEBHOOK}  ` },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    confirmTestSend();
    await screen.findByText('Test sent. Check your Slack channel for test-123.');
    expect(testAlertNotification).toHaveBeenCalledWith({
      channel: 'slack',
      webhook_url: SLACK_WEBHOOK,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
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

  it('sends no test notification when its confirmation is cancelled', async () => {
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    const dialog = screen.getByRole('dialog', { name: 'Send a test notification to Slack?' });
    expect(dialog).toHaveTextContent('behind the saved webhook');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(testAlertNotification).not.toHaveBeenCalled();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('explains test rate limiting without echoing the backend response', async () => {
    vi.mocked(testAlertNotification).mockRejectedValueOnce(
      new APIError('RATE_LIMIT_EXCEEDED', LEAKED_SECRET, 429),
    );
    await renderTab(alertConfig());
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    confirmTestSend();
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
    confirmTestSend();
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
    confirmTestSend();
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
    confirmTestSend();
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
    // Staged like any edit: nothing is removed before the category is saved.
    expect(alerts.getByRole('button', { name: 'Undo reset of SLACK_WEBHOOK_URL' })).toBeVisible();
    expect(resetConfigKey).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
    expect(screen.getByRole('dialog', { name: 'Save 1 change to Alerts?' })).toHaveTextContent(
      'SLACK_WEBHOOK_URLReset to the environment or default',
    );
    confirmSave();
    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SLACK_WEBHOOK_URL'));
    expect(patchConfig).not.toHaveBeenCalled();
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
    saveAndConfirm('Alerts');
    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SLACK_ALERTS_WEBHOOK_URL'));
    expect(resetConfigKey).toHaveBeenCalledTimes(1);
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

describe('editing in place', () => {
  it('offers every category for editing at once, with no Edit step and no save bar until a change', async () => {
    await renderTab(
      makeConfig({
        entries: [
          entry({ key: 'SMTP_HOST', category: 'email', value: 'smtp.example.test' }),
          entry({ key: 'SMTP_PORT', category: 'email', type: 'int', value: 25 }),
          entry({ key: 'SMTP_USERNAME', category: 'email', value: 'mailer' }),
          entry({ key: 'SMTP_PASSWORD', category: 'email', secret: true, value: LEAKED_SECRET }),
          entry({ key: 'TRUST_PROXY_HEADERS', category: 'network', type: 'bool', value: true }),
          entry({ key: 'FEATHERLESS_BASE_URL', category: 'providers', value: 'https://a.test' }),
        ],
        missing: [],
        pending_restart: [],
      }),
    );

    expect(screen.getByLabelText('SMTP_HOST')).toHaveValue('smtp.example.test');
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(25);
    expect(screen.getByLabelText('SMTP_USERNAME')).toHaveValue('mailer');
    expect(screen.getByLabelText('SMTP_PASSWORD')).toHaveTextContent('Set');
    expect(screen.getByRole('button', { name: 'Replace SMTP_PASSWORD' })).toBeEnabled();
    expect(screen.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' })).toBeChecked();
    expect(screen.getByLabelText('FEATHERLESS_BASE_URL')).toHaveValue('https://a.test');
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(screen.queryByText('Editing')).toBeNull();
    expect(screen.queryByRole('button', { name: /^(Discard|Save) changes to / })).toBeNull();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);

    fireEvent.change(screen.getByLabelText('SMTP_HOST'), { target: { value: 'smtp.new.test' } });
    // A bar for the changed category only, and nothing written yet.
    const email = within(section('Email (SMTP)'));
    expect(email.getByText('1 unsaved change')).toBeVisible();
    expect(email.getByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeEnabled();
    expect(email.getByRole('button', { name: 'Discard changes to Email (SMTP)' })).toBeEnabled();
    expect(screen.getAllByRole('button', { name: /^Save changes to / })).toHaveLength(1);
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('shows no save bar without a change, and counts the unsaved ones', async () => {
    await renderTab();
    const email = within(section('Email (SMTP)'));
    const port = email.getByLabelText('SMTP_PORT');

    expect(email.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
    // Enter with nothing changed asks nothing.
    fireEvent.submit(port.closest('form') as HTMLFormElement);
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.change(port, { target: { value: '587' } });
    fireEvent.change(email.getByLabelText('SMTP_HOST'), { target: { value: 'smtp.test' } });
    expect(email.getByText('2 unsaved changes')).toBeVisible();
    expect(email.getByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeEnabled();

    // Typed back to what is saved: not a change, so the bar goes again.
    fireEvent.change(port, { target: { value: '25' } });
    fireEvent.change(email.getByLabelText('SMTP_HOST'), { target: { value: '' } });
    expect(email.queryByText(/unsaved change/)).toBeNull();
    expect(email.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it("discards without writing: the category's drafts go, and another category's stay", async () => {
    await renderTab();
    const email = within(section('Email (SMTP)'));
    const network = within(section('Network'));
    fireEvent.change(email.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.click(email.getByRole('button', { name: 'Clear SMTP_PASSWORD' }));
    fireEvent.click(email.getByRole('button', { name: 'Reset SMTP_PORT' }));
    fireEvent.click(network.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }));

    fireEvent.click(email.getByRole('button', { name: 'Discard changes to Email (SMTP)' }));

    expect(email.getByLabelText('SMTP_PORT')).toHaveValue(25);
    expect(email.getByLabelText('SMTP_PASSWORD')).toHaveTextContent('Set');
    expect(email.queryByRole('button', { name: /changes to Email \(SMTP\)$/ })).toBeNull();
    expect(network.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' })).toBeChecked();
    expect(network.getByText('1 unsaved change')).toBeVisible();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(resetConfigKey).not.toHaveBeenCalled();
  });

  it('holds drafts in several categories at once, and saves only the one asked', async () => {
    const saved = makeConfig();
    saved.entries = saved.entries.map((item) =>
      item.key === 'SMTP_PORT' ? { ...item, value: 587 } : item,
    );
    vi.mocked(patchConfig).mockResolvedValueOnce(saved);
    await renderTab();
    const email = within(section('Email (SMTP)'));
    const network = within(section('Network'));
    fireEvent.change(email.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.click(network.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }));
    fireEvent.change(network.getByLabelText('CORS_ALLOWED_ORIGINS'), {
      target: { value: 'https://b.test' },
    });
    expect(email.getByText('1 unsaved change')).toBeVisible();
    expect(network.getByText('2 unsaved changes')).toBeVisible();

    saveAndConfirm('Email (SMTP)');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(patchConfig).toHaveBeenCalledTimes(1);
    expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } });
    expect(email.getByLabelText('SMTP_PORT')).toHaveValue(587);
    expect(email.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
    // Network's drafts outlive the other category's save, still unsaved.
    expect(network.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' })).toBeChecked();
    expect(network.getByLabelText('CORS_ALLOWED_ORIGINS')).toHaveValue('https://b.test');
    expect(network.getByText('2 unsaved changes')).toBeVisible();

    saveAndConfirm('Network');
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(2));
    expect(patchConfig).toHaveBeenLastCalledWith({
      values: { TRUST_PROXY_HEADERS: true, CORS_ALLOWED_ORIGINS: 'https://b.test' },
    });
  });

  it('writes once, on Confirm save, and then shows the saved values', async () => {
    const saved = makeConfig();
    saved.entries = saved.entries.map((item) =>
      item.key === 'SMTP_PORT' ? { ...item, value: 587 } : item,
    );
    vi.mocked(patchConfig).mockResolvedValueOnce(saved);
    await renderTab();
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(587);
    expect(patchConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Email (SMTP)?' });
    expect(dialog).toHaveTextContent('SMTP_PORT25 → 587');
    const confirm = within(dialog).getByRole('button', { name: 'Confirm save' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(patchConfig).toHaveBeenCalledTimes(1);
    expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } });
    // The saved value, with nothing left unsaved.
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(587);
    expect(screen.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
  });

  it('blocks a second submission while the save is in flight', async () => {
    let finish!: (value: ConfigResponse) => void;
    vi.mocked(patchConfig).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderTab();
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    saveAndConfirm('Email (SMTP)');

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Saving…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Discard changes to Email (SMTP)' })).toBeDisabled();
    expect(patchConfig).toHaveBeenCalledTimes(1);

    finish(makeConfig());
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(patchConfig).toHaveBeenCalledTimes(1);
  });

  it('keeps the drafts when the save fails, until they are discarded', async () => {
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('HTTP_500', 'The database is unavailable.', 500),
    );
    await renderTab();
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.change(screen.getByLabelText('SMTP_HOST'), { target: { value: 'smtp.test' } });
    saveAndConfirm('Email (SMTP)');

    const email = within(section('Email (SMTP)'));
    expect(await email.findByRole('alert')).toHaveTextContent('The database is unavailable.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(587);
    expect(screen.getByLabelText('SMTP_HOST')).toHaveValue('smtp.test');
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    expect(refreshMock).not.toHaveBeenCalled();

    // Discard takes the reason with the drafts.
    fireEvent.click(email.getByRole('button', { name: 'Discard changes to Email (SMTP)' }));
    expect(email.queryByRole('alert')).toBeNull();
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(25);
    expect(email.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
    expect(patchConfig).toHaveBeenCalledTimes(1);
  });

  it('confirms a secret only as Replace or Clear, never its value', async () => {
    const config = makeConfig();
    config.entries.push(
      entry({ key: 'SMTP_API_TOKEN', category: 'email', secret: true, value: LEAKED_SECRET }),
    );
    await renderTab(config);
    fireEvent.click(screen.getByRole('button', { name: 'Replace SMTP_PASSWORD' }));
    fireEvent.change(screen.getByLabelText('SMTP_PASSWORD'), {
      target: { value: 'typed-new-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Clear SMTP_API_TOKEN' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));

    const dialog = screen.getByRole('dialog', { name: 'Save 2 changes to Email (SMTP)?' });
    expect(dialog).toHaveTextContent('SMTP_PASSWORDReplace');
    expect(dialog).toHaveTextContent('SMTP_API_TOKENClear');
    expect(dialog.innerHTML).not.toContain('typed-new-secret');
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('masks a typed secret that a refused save echoes back', async () => {
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('HTTP_400', 'SMTP_PASSWORD: typed-new-secret is not accepted', 400),
    );
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Replace SMTP_PASSWORD' }));
    fireEvent.change(screen.getByLabelText('SMTP_PASSWORD'), {
      target: { value: 'typed-new-secret' },
    });
    saveAndConfirm('Email (SMTP)');

    // Beside the save and on the setting it names, as the other reasons are.
    const email = within(section('Email (SMTP)'));
    expect(await email.findByText('SMTP_PASSWORD: •••• is not accepted')).toBeVisible();
    expect(email.getByText('•••• is not accepted')).toBeVisible();
    expect(document.body.textContent).not.toContain('typed-new-secret');
  });
});

describe('edit flow edge cases', () => {
  /** Email with three stored rows that Reset can remove. */
  function resettableEmail(): ConfigResponse {
    return makeConfig({
      entries: [
        entry({ key: 'SMTP_HOST', category: 'email', value: 'smtp.example.test' }),
        entry({ key: 'SMTP_PORT', category: 'email', type: 'int', value: 25, default: 587 }),
        entry({ key: 'SMTP_FROM_NAME', category: 'email', value: 'Ops' }),
        entry({ key: 'SMTP_FROM_EMAIL', category: 'email', value: 'ops@example.test' }),
      ],
      missing: [],
      pending_restart: [],
    });
  }

  it('keeps only the unwritten steps after a reset fails, and retries just those', async () => {
    const config = resettableEmail();
    await renderTab(config);
    fireEvent.change(screen.getByLabelText('SMTP_HOST'), { target: { value: 'smtp.new.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_FROM_NAME' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_FROM_EMAIL' }));
    const written = {
      ...config,
      entries: config.entries.map((item) =>
        item.key === 'SMTP_HOST' ? { ...item, value: 'smtp.new.test' } : item,
      ),
    };
    vi.mocked(patchConfig).mockResolvedValueOnce(written);
    vi.mocked(resetConfigKey)
      .mockResolvedValueOnce(written)
      .mockRejectedValueOnce(new APIError('HTTP_503', 'The database is unavailable.', 503));
    saveAndConfirm('Email (SMTP)');

    const email = within(section('Email (SMTP)'));
    expect(await email.findByRole('alert')).toHaveTextContent(
      'SMTP_FROM_NAME could not be reset: The database is unavailable.',
    );
    expect(email.getByRole('alert')).toHaveTextContent('2 changes were saved');
    // What was not written is still staged.
    expect(email.getByText('2 unsaved changes')).toBeVisible();
    expect(email.getByLabelText('SMTP_FROM_NAME')).toHaveTextContent('Reset when you save');
    expect(email.getByLabelText('SMTP_FROM_EMAIL')).toHaveTextContent('Reset when you save');

    // The retry lists, and sends, only what is left.
    vi.mocked(resetConfigKey).mockResolvedValue(written);
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    const dialog = screen.getByRole('dialog', { name: 'Save 2 changes to Email (SMTP)?' });
    expect(dialog).not.toHaveTextContent('SMTP_HOST');
    expect(dialog).not.toHaveTextContent('SMTP_PORT');
    confirmSave();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(patchConfig).toHaveBeenCalledTimes(1);
    expect(vi.mocked(resetConfigKey).mock.calls.map(([key]) => key)).toEqual([
      'SMTP_PORT',
      'SMTP_FROM_NAME',
      'SMTP_FROM_NAME',
      'SMTP_FROM_EMAIL',
    ]);
    expect(email.queryByRole('alert')).toBeNull();
    expect(email.queryByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeNull();
  });

  describe('a save that both changes and resets', () => {
    // TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS, so turning the second off
    // is valid only once the first is reset; the backend checks each request alone.
    function trustConfig(): ConfigResponse {
      return makeConfig({
        entries: [
          entry({ key: 'SMTP_HOST', category: 'email', value: 'smtp.example.test' }),
          entry({ key: 'TRUST_PROXY_HEADERS', category: 'network', type: 'bool', value: true }),
          entry({ key: 'TRUST_X_REAL_IP', category: 'network', type: 'bool', value: true }),
        ],
        missing: [],
        pending_restart: [],
      });
    }

    async function stageTrustChange() {
      await renderTab(trustConfig());
      fireEvent.click(
        within(section('Network')).getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }),
      );
      fireEvent.click(screen.getByRole('button', { name: 'Reset TRUST_X_REAL_IP' }));
      fireEvent.click(screen.getByRole('button', { name: 'Save changes to Network' }));
      return screen.getByRole('dialog', { name: 'Save 2 changes to Network?' });
    }

    it('says in the review that the resets follow the value changes as separate requests', async () => {
      const dialog = await stageTrustChange();

      expect(dialog).toHaveTextContent(
        'The value changes are sent first, in one request; then each reset, on its own. ' +
          'The backend checks each request by itself, so a change that is valid only together ' +
          'with one of these resets can be refused; if so, save that reset on its own first.',
      );
    });

    it('says the reset was not sent when the value change before it is refused', async () => {
      vi.mocked(patchConfig).mockRejectedValueOnce(
        new APIError(
          'HTTP_400',
          'TRUST_PROXY_HEADERS: TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS; ' +
            'change TRUST_X_REAL_IP in the same save',
          400,
        ),
      );
      await stageTrustChange();
      confirmSave();

      // The category's own alert, beside its Save; the setting it names shows the reason too.
      const alerts = await within(section('Network')).findAllByRole('alert');
      const alert = alerts.find((candidate) =>
        candidate.textContent?.startsWith('TRUST_PROXY_HEADERS: '),
      );
      expect(alert).toHaveTextContent(
        'TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS; change TRUST_X_REAL_IP in the same save. ' +
          'TRUST_X_REAL_IP was not reset: resets are sent after the value changes, one request ' +
          'each. If this change needs a reset, save the reset on its own first.',
      );
      expect(resetConfigKey).not.toHaveBeenCalled();
      expect(within(section('Network')).getByLabelText('TRUST_X_REAL_IP')).toHaveTextContent(
        'Reset when you save',
      );
    });
  });

  it('does not resend a channel switch that was written before a reset failed', async () => {
    const config = alertConfig(['SLACK_ALERTS_WEBHOOK_URL']);
    await renderTab(config);
    chooseAlertChannel('WeCom');
    fireEvent.change(screen.getByLabelText('WeCom webhook URL'), {
      target: { value: WECOM_WEBHOOK },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Reset ALERTS_ENABLED' }));
    vi.mocked(patchConfig).mockResolvedValueOnce(alertConfig(['WECOM_ALERTS_WEBHOOK_URL']));
    vi.mocked(resetConfigKey).mockRejectedValueOnce(
      new APIError('HTTP_503', 'The database is unavailable.', 503),
    );
    saveAndConfirm('Alerts');
    await within(section('Alerts')).findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Alerts?' });
    expect(dialog).toHaveTextContent('ALERTS_ENABLED');
    expect(dialog).not.toHaveTextContent('SLACK_ALERTS_WEBHOOK_URL');
  });

  it('drops a staged webhook reset when the channel changes, so the new URL is saved', async () => {
    // None is saved, and SLACK_WEBHOOK_URL keeps the empty row an earlier None wrote.
    const config = alertConfig([]);
    config.entries = config.entries.map((item) =>
      item.key === 'SLACK_WEBHOOK_URL'
        ? { ...item, is_set: false, value: null, updated_at: STORED_AT }
        : item,
    );
    await renderTab(config);
    fireEvent.click(screen.getByRole('button', { name: 'Reset SLACK_WEBHOOK_URL' }));
    chooseAlertChannel('Slack');

    const field = screen.getByLabelText('Slack webhook URL');
    expect(field.tagName).toBe('INPUT');
    fireEvent.change(field, { target: { value: SLACK_WEBHOOK } });
    saveAndConfirm('Alerts');
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { SLACK_WEBHOOK_URL: SLACK_WEBHOOK, WECOM_ALERTS_WEBHOOK_URL: '' },
      }),
    );
    expect(resetConfigKey).not.toHaveBeenCalled();
  });

  it('keeps a category with unsaved changes visible when a search would hide it', async () => {
    await renderTab();
    const search = screen.getByLabelText('Search settings');
    // With nothing changed, a search that matches nothing in Email hides it.
    fireEvent.change(search, { target: { value: 'glm' } });
    expect(screen.queryByRole('region', { name: 'Email (SMTP)' })).toBeNull();
    fireEvent.change(search, { target: { value: '' } });

    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.change(search, { target: { value: 'glm' } });
    const email = within(section('Email (SMTP)'));
    expect(email.queryByLabelText('SMTP_PORT')).toBeNull();
    expect(
      email.getByText(
        'Every setting in this category is hidden by the current filter. ' +
          'Unsaved changes to them are kept and saved with the rest.',
      ),
    ).toBeVisible();
    expect(email.getByRole('button', { name: 'Discard changes to Email (SMTP)' })).toBeEnabled();
    expect(email.getByRole('button', { name: 'Save changes to Email (SMTP)' })).toBeEnabled();
    const index = screen.getByRole('navigation', { name: 'Configuration categories' });
    expect(within(index).getByRole('link', { name: /Email \(SMTP\)/ })).toHaveTextContent(
      'unsaved changes',
    );
    // A category with nothing unsaved is still filtered out.
    expect(screen.queryByRole('region', { name: 'Network' })).toBeNull();

    // Once its changes are discarded, nothing keeps it there.
    fireEvent.click(email.getByRole('button', { name: 'Discard changes to Email (SMTP)' }));
    expect(screen.queryByRole('region', { name: 'Email (SMTP)' })).toBeNull();
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('saves changes a filter hides with the rest of their category', async () => {
    await renderTab();
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });

    fireEvent.click(screen.getByRole('checkbox', { name: 'Missing only' }));
    expect(screen.queryByLabelText('SMTP_PORT')).toBeNull();
    expect(
      within(section('Email (SMTP)')).getByText(
        '2 settings in this category are hidden by the current filter. ' +
          'Unsaved changes to them are kept and saved with the rest.',
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } }));
  });

  it('counts a variable that is both edited and marked secret once', async () => {
    await renderTab(
      makeConfig({
        entries: [
          entry({ key: 'SMTP_HOST', category: 'email', value: 'smtp.example.test' }),
          entry({
            key: 'ROUTE_REGION',
            category: 'providers',
            origin: 'discovered',
            value: 'eu-west',
          }),
        ],
        missing: [],
        pending_restart: [],
      }),
    );
    fireEvent.change(screen.getByLabelText('ROUTE_REGION'), { target: { value: 'us-east' } });
    fireEvent.click(screen.getByRole('button', { name: 'Mark ROUTE_REGION as secret' }));

    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Providers' }));
    expect(screen.getByRole('dialog', { name: 'Save 1 change to Providers?' })).toBeVisible();
  });
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
    const unset = within(section('Providers')).getByLabelText('ZAI_API_KEY');
    expect(unset).toHaveAttribute('placeholder', 'Not set');
    expect(unset).toHaveValue('');
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });

  it('replaces a secret write-only: the typed value is sent once and not shown again', async () => {
    await renderTab();
    const email = section('Email (SMTP)');

    fireEvent.click(within(email).getByRole('button', { name: 'Replace SMTP_PASSWORD' }));
    const input = within(email).getByLabelText('SMTP_PASSWORD');
    expect(input).toHaveAttribute('type', 'password');
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: 'new-smtp-secret' } });
    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));
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
    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));
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

  /** Stage the flag on `key` and save Providers with it. */
  function saveMarking(key: string) {
    fireEvent.click(screen.getByRole('button', { name: `Mark ${key} as secret` }));
    saveAndConfirm('Providers');
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

  it('marks it with the save, after a warning, and never shows the plaintext again', async () => {
    const config = withVariables();
    vi.mocked(patchConfig).mockResolvedValueOnce(markedIn(config, 'MY_ROUTE_HEADER', true));
    await renderTab(config);
    const providers = section('Providers');
    const field = within(providers).getByLabelText('MY_ROUTE_HEADER');
    expect(field).toHaveValue(HEADER_VALUE);
    fireEvent.change(field, { target: { value: 'typed-value' } });

    fireEvent.click(
      within(providers).getByRole('button', { name: 'Mark MY_ROUTE_HEADER as secret' }),
    );
    // Staged: a note and an undo, and nothing sent.
    expect(providers).toHaveTextContent('Becomes secret when you save.');
    expect(
      within(providers).getByRole('button', { name: 'Keep MY_ROUTE_HEADER visible' }),
    ).toBeVisible();
    expect(patchConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Providers' }));
    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Providers?' });
    expect(dialog).toHaveTextContent('Mark as secret');
    expect(dialog).toHaveTextContent(
      'Marking a variable secret cannot be undone from the console.',
    );
    confirmSave();

    // One PATCH carries the value and the flag.
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_ROUTE_HEADER: 'typed-value' },
        secrets: { MY_ROUTE_HEADER: true },
      }),
    );
    expect(patchConfig).toHaveBeenCalledTimes(1);
    expect(markConfigSecret).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // Neither the stored value (still in the buggy response) nor the edit.
    for (const plaintext of [HEADER_VALUE, 'typed-value']) {
      expect(screen.queryByDisplayValue(plaintext)).not.toBeInTheDocument();
      expect(document.body.innerHTML).not.toContain(plaintext);
    }
    const after = section('Providers');
    expect(within(after).getByLabelText('MY_ROUTE_HEADER')).toHaveTextContent('Set');
    expect(within(after).queryByRole('button', { name: 'Save changes to Providers' })).toBeNull();
    expect(refreshMock).toHaveBeenCalled();
  });

  it('does not claim success when the variable comes back unchanged', async () => {
    const config = withVariables();
    vi.mocked(patchConfig).mockResolvedValueOnce(config);
    await renderTab(config);

    saveMarking('ROUTE_REGION');

    expect(await within(section('Providers')).findByRole('alert')).toHaveTextContent(
      'The backend answered, but ROUTE_REGION is still not secret.',
    );
    // The flag is still staged.
    expect(screen.getByRole('button', { name: 'Keep ROUTE_REGION visible' })).toBeVisible();
    expect(screen.getByLabelText('ROUTE_REGION')).toHaveValue('eu-west');
    expect(refreshMock).not.toHaveBeenCalled();
  });

  // The backend's 400s for a flag-only change, shown as they are.
  it.each([
    'ROUTE_REGION has no stored value to protect yet: set a value and mark it secret in the same save.',
    "ROUTE_REGION: a built-in setting keeps the registry's secret flag.",
  ])('keeps the staged flag with the reason the backend gave: %s', async (message) => {
    vi.mocked(patchConfig).mockRejectedValueOnce(new APIError('HTTP_400', message, 400));
    await renderTab(withVariables());

    saveMarking('ROUTE_REGION');

    expect(
      (await within(section('Providers')).findByText(message, { exact: false })).textContent,
    ).toContain(message.replace(/^ROUTE_REGION: /, ''));
    // Nothing changed on the page, and the staged flag is still there to retry.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('ROUTE_REGION')).toHaveValue('eu-west');
    expect(screen.getByRole('button', { name: 'Keep ROUTE_REGION visible' })).toBeVisible();
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
    // No Replace, Clear, Reset or Mark as secret, nothing to type into, and so nothing to save.
    expect(within(security).queryAllByRole('button')).toEqual([]);
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
    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));

    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Email (SMTP)?' });
    expect(within(dialog).getByText('SMTP_PORT')).toBeInTheDocument();
    expect(within(dialog).getByText(/→ 587$/)).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();

    // Back to editing writes nothing and keeps the edit, so it can still be saved.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(within(email).getByLabelText('SMTP_PORT')).toHaveValue(587);

    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith({ values: { SMTP_PORT: 587 } }));
  });

  it('saves a category on Enter, after confirmation', async () => {
    await renderTab();
    const email = section('Email (SMTP)');
    const port = within(email).getByLabelText('SMTP_PORT');
    fireEvent.change(port, { target: { value: '587' } });

    // Enter in a field submits its form: the Save button is that form's.
    const save = within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' });
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
    // Enter reviews; the write waits for the confirmation.
    const dialog = screen.getByRole('dialog', { name: 'Add MY_REGION?' });
    expect(dialog).toHaveTextContent('MY_REGIONeu');
    expect(patchConfig).not.toHaveBeenCalled();
    confirmAddVariable();

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
    fireEvent.click(within(section('Email (SMTP)')).getByRole('button', { name: /^Save changes/ }));
    confirmSave();
    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { SMTP_PORT: 587, SMTP_HOST: 'smtp.example.test' },
      }),
    );

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(
      within(section('Network')).getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }),
    );
    fireEvent.change(within(section('Network')).getByLabelText('CORS_ALLOWED_ORIGINS'), {
      target: { value: 'https://a.test,https://b.test' },
    });
    fireEvent.click(
      within(section('Network')).getByRole('button', { name: 'Save changes to Network' }),
    );
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
    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));
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
    fireEvent.click(within(email).getByRole('button', { name: 'Save changes to Email (SMTP)' }));
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
    // Staged: the field says what the save will do, and Undo takes it back.
    expect(screen.getByLabelText('SMTP_PORT')).toHaveTextContent('Reset when you save');
    expect(resetConfigKey).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Undo reset of SMTP_PORT' }));
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(25);
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    const dialog = screen.getByRole('dialog', { name: 'Save 1 change to Email (SMTP)?' });
    expect(dialog).toHaveTextContent('25 → environment or default');
    expect(dialog).toHaveTextContent('falls back to the environment, then to its default');
    expect(resetConfigKey).not.toHaveBeenCalled();
    confirmSave();

    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledWith('SMTP_PORT'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(patchConfig).not.toHaveBeenCalled();
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
    saveAndConfirm('Email (SMTP)');

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
    fireEvent.click(within(form).getByRole('button', { name: 'Review and add' }));
    expect(patchConfig).not.toHaveBeenCalled();
    confirmAddVariable();

    await waitFor(() =>
      expect(patchConfig).toHaveBeenCalledWith({
        values: { MY_PROVIDER_API_KEY: 'k-123' },
        secrets: { MY_PROVIDER_API_KEY: true },
      }),
    );
  });

  it('masks the secret value of a refused new variable in its review', async () => {
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('HTTP_400', 'MY_PROVIDER_API_KEY: k-123-typed is not accepted', 400),
    );
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), {
      target: { value: 'my_provider_api_key' },
    });
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'k-123-typed' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Review and add' }));
    confirmAddVariable();

    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent(
      'MY_PROVIDER_API_KEY: •••• is not accepted',
    );
    expect(document.body.textContent).not.toContain('k-123-typed');
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
    fireEvent.click(within(form).getByRole('button', { name: 'Review and add' }));
    expect(patchConfig).not.toHaveBeenCalled();
    confirmAddVariable();

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
    fireEvent.click(within(form).getByRole('button', { name: 'Review and add' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent(/environment-variable name/);

    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'SMTP_PORT' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Review and add' }));
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

  it('indexes the categories the filters leave, and keeps one marked', async () => {
    await renderTab();
    const index = screen.getByRole('navigation', { name: 'Configuration categories' });
    const names = () =>
      within(index)
        .getAllByRole('link')
        .map((link) => link.textContent);
    expect(names()).toEqual(['Email (SMTP)1 missing', 'Network', 'Providers1 missing', 'Security']);
    expect(within(index).getByRole('link', { name: 'Network' })).toHaveAttribute(
      'href',
      '#config-network',
    );
    expect(document.getElementById('config-network')).toBe(
      screen.getByRole('region', { name: 'Network' }),
    );

    fireEvent.click(within(index).getByRole('link', { name: 'Network' }));
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'glm' } });

    expect(names()).toEqual(['Providers1 missing']);
    expect(within(index).getByRole('link', { name: /Providers/ })).toHaveAttribute(
      'aria-current',
      'location',
    );
  });

  it('flags a category with unsaved changes in the index, until they are discarded', async () => {
    await renderTab();
    const index = screen.getByRole('navigation', { name: 'Configuration categories' });
    const link = within(index).getByRole('link', { name: /Email \(SMTP\)/ });
    expect(link).not.toHaveTextContent('unsaved changes');

    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    expect(link).toHaveTextContent('unsaved changes');
    // Those notes are screen-reader text inside a row that scrolls sideways
    // on a phone: they must not widen the page.
    expect(escapingScreenReaderText(index)).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Email (SMTP)' }));
    expect(link).not.toHaveTextContent('unsaved changes');
    expect(patchConfig).not.toHaveBeenCalled();
  });

  it('counts a staged reset in the index like the edit bar does', async () => {
    await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));
    const index = screen.getByRole('navigation', { name: 'Configuration categories' });
    expect(within(index).getByRole('link', { name: /Email \(SMTP\)/ })).toHaveTextContent(
      'unsaved changes',
    );
    expect(within(section('Email (SMTP)')).getByText('1 unsaved change')).toBeVisible();
    expect(resetConfigKey).not.toHaveBeenCalled();
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

describe('backup and restore', () => {
  it('warns about plaintext secrets before downloading a configuration backup', async () => {
    vi.mocked(exportConfigBackup).mockResolvedValueOnce({
      schema_version: 1,
      exported_at: '2026-10-25T15:00:00Z',
      app_config: [{ key: 'SMTP_PORT', value: '587', secret: false, source: 'admin' }],
    });
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Backup' }));
    const dialog = screen.getByRole('dialog', { name: 'Download configuration backup?' });
    expect(dialog).toHaveTextContent('includes API keys and secrets in plain text');
    expect(exportConfigBackup).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Download backup' }));

    await waitFor(() => expect(exportConfigBackup).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(toast.success).toHaveBeenCalledWith('Downloaded configuration backup.');
  });

  it('validates a selected backup JSON, confirms summary, and restores state', async () => {
    const restoredConfig = makeConfig({
      entries: [
        entry({ key: 'SMTP_PORT', category: 'email', type: 'int', value: 2525, default: 587 }),
      ],
      missing: [],
      pending_restart: [],
    });
    vi.mocked(restoreConfigBackup).mockResolvedValueOnce({
      config: restoredConfig,
      immutable_keys_preserved: ['API_KEY_SECRET'],
    });
    await renderTab();
    // Unsaved drafts in two categories, which the restored values replace.
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '587' } });
    fireEvent.click(screen.getByRole('switch', { name: 'TRUST_PROXY_HEADERS' }));

    const payload = {
      schema_version: 1,
      exported_at: '2026-10-25T16:00:00Z',
      app_config: [{ key: 'SMTP_PORT', value: '2525', secret: false, source: 'admin' }],
      site_settings: [{ key: 'signup_enabled', value: 'true', value_type: 'bool' }],
      provider_definitions: [
        { provider: 'acme', display_name: 'Acme', default_base_url: 'https://api.acme/v1' },
      ],
      provider_keys: [{ id: 'k1', provider: 'acme', api_key: 'sk-1234' }],
      provider_route_candidates: [],
      provider_route_configs: [],
    };
    const file = new File([JSON.stringify(payload)], 'backup.json', { type: 'application/json' });
    Object.defineProperty(file, 'text', {
      value: async () => JSON.stringify(payload),
    });

    const fileInput = screen.getByLabelText('Select configuration backup file');
    fireEvent.change(fileInput, { target: { files: [file] } });

    const dialog = await screen.findByRole('dialog', {
      name: 'Restore configuration from backup?',
    });
    expect(dialog).toHaveTextContent('backup.json');
    expect(dialog).toHaveTextContent('1 stored row');
    expect(dialog).toHaveTextContent('1 provider, 1 API key');
    expect(restoreConfigBackup).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore configuration' }));

    await waitFor(() => expect(restoreConfigBackup).toHaveBeenCalledWith(payload));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(2525);
    expect(screen.queryByRole('button', { name: /^(Discard|Save) changes to / })).toBeNull();
    expect(refreshMock).toHaveBeenCalled();
    expect(toast.success).toHaveBeenCalledWith(
      'Restored configuration (preserved existing API_KEY_SECRET).',
    );
  });

  it('rejects a malformed backup file before opening the confirmation dialog', async () => {
    await renderTab();

    const badFile = new File(['not-json'], 'broken.json', { type: 'application/json' });
    Object.defineProperty(badFile, 'text', {
      value: async () => 'not-json',
    });

    const fileInput = screen.getByLabelText('Select configuration backup file');
    fireEvent.change(fileInput, { target: { files: [badFile] } });

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'The selected file is not a valid configuration backup JSON.',
      ),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps the drafts and reloads when the configuration changed elsewhere', async () => {
    await renderTab();
    const email = section('Email (SMTP)');
    fireEvent.change(within(email).getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError(
        'CONFIG_VERSION_CONFLICT',
        'The configuration changed since it was loaded.',
        409,
      ),
    );
    vi.mocked(getConfig).mockClear();
    saveAndConfirm('Email (SMTP)');
    expect(
      await screen.findByText(/changed elsewhere since this page loaded, so this was not saved/),
    ).toBeInTheDocument();
    await waitFor(() => expect(getConfig).toHaveBeenCalledTimes(1));
    // The change the administrator typed is still there to review and save again.
    expect(within(email).getByLabelText('SMTP_PORT')).toHaveValue(2525);
  });

  it('shows how each process applied a live group beside its settings', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue({
      database: true,
      source: 'app_config',
      version: { epoch: 'e1', revision: 7 },
      this_instance: 'p1',
      stale_after_seconds: 60,
      read_ok: true,
      unknown_groups: [],
      groups: [
        {
          name: 'alerts.runtime',
          keys: ['SLACK_ALERTS_WEBHOOK_URL'],
          schema_version: 1,
          contract: { active: true, schema_version: 1 },
          coverage: {
            registered: 1,
            live: 1,
            converged: 1,
            applying: 0,
            failed: 0,
            unsupported: 0,
            dormant: 0,
            unknown: 0,
            stopped: 0,
            verified: false,
          },
          instances: [
            {
              instance_id: 'p1',
              this_process: true,
              build: 'b',
              host: 'h',
              lifecycle: 'running',
              started_at: null,
              heartbeat_age_seconds: 2,
              stale: false,
              supports: true,
              status: { state: 'applied', epoch: 'e1', reconciled_revision: 7 },
            },
          ],
          local: null,
        },
      ],
    });
    const config = alertConfig();
    config.entries = config.entries.map((item) =>
      item.category === 'alerts' && item.key !== 'ALERTS_ENABLED'
        ? { ...item, group: 'alerts.runtime', apply_mode: 'managed' as const }
        : item,
    );
    config.source_version = { epoch: 'e1', revision: 7 };
    await renderTab(config);
    const alerts = section('Alerts');
    expect(
      await within(alerts).findByText('1 of 1 registered process has applied revision 7', {
        exact: false,
      }),
    ).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(LEAKED_SECRET);
  });
});

describe('configuration versions', () => {
  const v = (revision: number) => ({ epoch: 'e1', revision });

  /** Email and General, both editable, read at revision 41. */
  function versionedConfig(overrides: Partial<ConfigResponse> = {}): ConfigResponse {
    return makeConfig({
      categories: [
        { id: 'email', label: 'Email (SMTP)', description: 'Outgoing mail.' },
        { id: 'general', label: 'General', description: 'Site.' },
      ],
      entries: [
        entry({ key: 'SMTP_HOST', category: 'email', value: 'smtp.example.test' }),
        entry({ key: 'SMTP_PORT', category: 'email', type: 'int', value: 25, default: 587 }),
        entry({ key: 'SMTP_FROM_NAME', category: 'email', value: 'Ops' }),
        entry({ key: 'SITE_NAME', category: 'general', value: 'Gateway' }),
      ],
      missing: [],
      pending_restart: [],
      source_version: v(41),
      ...overrides,
    });
  }

  it('chains each request of one save on the version the one before it committed', async () => {
    await renderTab(versionedConfig());
    fireEvent.change(screen.getByLabelText('SMTP_HOST'), { target: { value: 'smtp.new.test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_PORT' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reset SMTP_FROM_NAME' }));
    // Each response also reports a newer source_version: a save made elsewhere
    // in between, which the next request must not silently build on.
    vi.mocked(patchConfig).mockResolvedValueOnce(
      versionedConfig({ saved_version: v(42), source_version: v(44) }),
    );
    vi.mocked(resetConfigKey)
      .mockResolvedValueOnce(versionedConfig({ saved_version: v(45), source_version: v(47) }))
      .mockResolvedValueOnce(versionedConfig({ saved_version: v(48), source_version: v(48) }));
    saveAndConfirm('Email (SMTP)');

    await waitFor(() => expect(resetConfigKey).toHaveBeenCalledTimes(2));
    expect(patchConfig).toHaveBeenCalledWith(
      { values: { SMTP_HOST: 'smtp.new.test' } },
      { expectedVersion: v(41) },
    );
    expect(vi.mocked(resetConfigKey).mock.calls).toEqual([
      ['SMTP_PORT', { expectedVersion: v(42) }],
      ['SMTP_FROM_NAME', { expectedVersion: v(45) }],
    ]);
  });

  it('carries a draft across this page’s own save in another category, and no further', async () => {
    await renderTab(versionedConfig());
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    fireEvent.change(screen.getByLabelText('SITE_NAME'), { target: { value: 'Renamed' } });
    // General's save, conditional on 41, committed 42; by the time it answered
    // someone else had saved 43.
    vi.mocked(patchConfig).mockResolvedValueOnce(
      versionedConfig({ saved_version: v(42), source_version: v(43) }),
    );
    saveAndConfirm('General');
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('SMTP_PORT')).toHaveValue(2525);

    saveAndConfirm('Email (SMTP)');
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(2));
    // 41 → 42 was this page's own write; 43 is not, so it is a conflict to review.
    expect(vi.mocked(patchConfig).mock.calls[1]).toEqual([
      { values: { SMTP_PORT: 2525 } },
      { expectedVersion: v(42) },
    ]);
  });

  it('never moves an older draft onto a version a later read brought in', async () => {
    await renderTab(versionedConfig());
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    fireEvent.change(screen.getByLabelText('SITE_NAME'), { target: { value: 'Renamed' } });
    // General's save conflicts, and the page reads revision 50 to show it.
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError(
        'CONFIG_VERSION_CONFLICT',
        'The configuration changed since it was loaded.',
        409,
      ),
    );
    vi.mocked(getConfig).mockClear();
    vi.mocked(getConfig).mockResolvedValueOnce(versionedConfig({ source_version: v(50) }));
    saveAndConfirm('General');
    await screen.findByText(/changed elsewhere since this page loaded/);
    await waitFor(() => expect(getConfig).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByLabelText('SITE_NAME')).toHaveValue('Renamed'));

    // Email's draft was made on 41 and nobody showed its owner 50.
    vi.mocked(patchConfig).mockResolvedValueOnce(
      versionedConfig({ saved_version: v(51), source_version: v(51) }),
    );
    saveAndConfirm('Email (SMTP)');
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(2));
    expect(vi.mocked(patchConfig).mock.calls[1][1]).toEqual({ expectedVersion: v(41) });

    // General's owner was shown 50 with the conflict, and saves on it.
    saveAndConfirm('General');
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(3));
    expect(vi.mocked(patchConfig).mock.calls[2]).toEqual([
      { values: { SITE_NAME: 'Renamed' } },
      { expectedVersion: v(50) },
    ]);
  });

  it('saves on the version under review when a read completes while it is open', async () => {
    await renderTab(versionedConfig());
    fireEvent.change(screen.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    fireEvent.change(screen.getByLabelText('SITE_NAME'), { target: { value: 'Renamed' } });
    // General's conflict starts a reload that is still in flight...
    let finishRead: (config: ConfigResponse) => void = () => undefined;
    vi.mocked(getConfig).mockClear();
    vi.mocked(getConfig).mockImplementationOnce(
      () => new Promise<ConfigResponse>((resolve) => (finishRead = resolve)),
    );
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError(
        'CONFIG_VERSION_CONFLICT',
        'The configuration changed since it was loaded.',
        409,
      ),
    );
    saveAndConfirm('General');
    await screen.findByText(/changed elsewhere since this page loaded/);
    await waitFor(() => expect(getConfig).toHaveBeenCalledTimes(1));
    // ...while Email's changes are under review.
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('25 → 2525');
    finishRead(
      versionedConfig({
        source_version: v(50),
        entries: versionedConfig().entries.map((item) =>
          item.key === 'SMTP_PORT' ? { ...item, value: 26 } : item,
        ),
      }),
    );
    await waitFor(() => expect(screen.getByLabelText('SITE_NAME')).toHaveValue('Renamed'));
    vi.mocked(patchConfig).mockResolvedValueOnce(
      versionedConfig({ saved_version: v(51), source_version: v(51) }),
    );
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(2));
    // The review showed 25 → 2525 on revision 41; the save must not accept 50's 26 unseen.
    expect(vi.mocked(patchConfig).mock.calls[1]).toEqual([
      { values: { SMTP_PORT: 2525 } },
      { expectedVersion: v(41) },
    ]);
  });

  it('keeps two loaded editing views on their own versions', async () => {
    vi.mocked(getConfig)
      .mockResolvedValueOnce(versionedConfig({ source_version: v(41) }))
      .mockResolvedValueOnce(versionedConfig({ source_version: v(42) }));
    const first = render(<ConfigurationTab />);
    const firstView = within(first.container);
    await firstView.findByRole('region', { name: 'Email (SMTP)' });
    const second = render(<ConfigurationTab />);
    const secondView = within(second.container);
    await secondView.findByRole('region', { name: 'Email (SMTP)' });

    fireEvent.change(firstView.getByLabelText('SMTP_PORT'), { target: { value: '2525' } });
    fireEvent.change(secondView.getByLabelText('SMTP_PORT'), { target: { value: '2626' } });
    fireEvent.click(secondView.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    fireEvent.click(firstView.getByRole('button', { name: 'Save changes to Email (SMTP)' }));
    confirmSave();
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(2));
    expect(vi.mocked(patchConfig).mock.calls.map((call) => call[1])).toEqual([
      { expectedVersion: v(42) },
      { expectedVersion: v(41) },
    ]);
  });
});

describe('when a saved change applies', () => {
  /** The alert category with the detector threshold, its group in the given mode. */
  function groupConfig(applyMode: 'managed' | 'restart', pending: boolean = false): ConfigResponse {
    const config = alertConfig();
    config.entries = [
      ...config.entries,
      entry({
        key: 'FAILED_REQUEST_ALERT_THRESHOLD',
        category: 'alerts',
        type: 'int',
        value: 200,
        default: 200,
        restart_required: true,
      }),
    ].map((item) =>
      item.category === 'alerts' && item.key !== 'ALERTS_ENABLED'
        ? {
            ...item,
            group: 'alerts.runtime',
            apply_mode: applyMode,
            pending_restart: pending && item.key === 'FAILED_REQUEST_ALERT_THRESHOLD',
          }
        : item,
    );
    config.pending_restart = pending ? ['FAILED_REQUEST_ALERT_THRESHOLD'] : [];
    return config;
  }

  it('labels an active group live: no restart badge, note or confirmation', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue({
      database: true,
      source: 'app_config',
      version: null,
      this_instance: null,
      stale_after_seconds: 60,
      read_ok: true,
      unknown_groups: [],
      groups: [],
    });
    await renderTab(groupConfig('managed'));
    const alerts = within(section('Alerts'));
    expect(alerts.getByText('Applies live')).toBeInTheDocument();
    expect(alerts.queryByText('Restart required')).toBeNull();
    expect(alerts.queryByText('Pending restart')).toBeNull();
    expect(alerts.getByText(/without a restart/)).toBeInTheDocument();
    expect(alerts.queryByText('Changes apply after saving and restarting the backend.')).toBeNull();

    fireEvent.change(alerts.getByLabelText('FAILED_REQUEST_ALERT_THRESHOLD'), {
      target: { value: '50' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
    expect(screen.getByRole('dialog')).not.toHaveTextContent(
      'apply only after the backend restarts',
    );
  });

  it('labels a dormant group restart-required, and its saved-but-unapplied value pending', async () => {
    vi.mocked(getLiveConfigStatus).mockResolvedValue({
      database: true,
      source: 'app_config',
      version: null,
      this_instance: null,
      stale_after_seconds: 60,
      read_ok: true,
      unknown_groups: [],
      groups: [],
    });
    await renderTab(groupConfig('restart'));
    let alerts = within(section('Alerts'));
    expect(alerts.getAllByText('Restart required').length).toBeGreaterThan(0);
    expect(alerts.queryByText('Applies live')).toBeNull();
    expect(
      alerts.getByText('Changes apply after saving and restarting the backend.'),
    ).toBeVisible();
    fireEvent.change(alerts.getByLabelText('FAILED_REQUEST_ALERT_THRESHOLD'), {
      target: { value: '50' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('apply only after the backend restarts');
    cleanup();

    // Deactivated after it applied (dormant-frozen): a later save waits for the restart.
    await renderTab(groupConfig('restart', true));
    alerts = within(section('Alerts'));
    expect(alerts.getAllByText('Pending restart').length).toBeGreaterThan(0);
    expect(alerts.queryByText('Applies live')).toBeNull();
  });

  it('says a test send tests the saved webhook, not the channel in use', async () => {
    await renderTab(groupConfig('managed'));
    fireEvent.click(screen.getByRole('button', { name: 'Send test notification' }));
    const dialog = screen.getByRole('dialog', { name: 'Send a test notification to Slack?' });
    expect(dialog).toHaveTextContent('This tests the saved setting');
    expect(dialog).toHaveTextContent('not necessarily the channel alerts are delivered to');
  });
});
