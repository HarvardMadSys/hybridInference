// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ConfigEntry, ConfigResponse } from '@/lib/api/config';
import type { RuntimeSettingItem } from '@/lib/api/admin';
import { APIError } from '@/lib/utils/errors';

import { openSettingsTab, renderSettings } from './settingsTabHarness';

const replaceMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: replaceMock, refresh: vi.fn() }),
}));

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
    one_time: false,
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

/** An alert webhook with no value: the channel picker edits the three together. */
function webhook(key: string): ConfigEntry {
  return entry({
    key,
    category: 'alerts',
    secret: true,
    one_time: true,
    is_set: false,
    value: null,
    source: 'default',
  });
}

/**
 * One setting or more in each category a sub-tab names, and one in a category
 * none names. Some are usually set once, in a category with others or alone.
 */
const CONFIG: ConfigResponse = {
  categories: [
    { id: 'general', label: 'General', description: 'Site identity.' },
    { id: 'security', label: 'Security', description: 'Secrets.' },
    { id: 'signup', label: 'Sign-up', description: 'Signup notices.' },
    { id: 'email', label: 'Email', description: 'Outgoing mail.' },
    { id: 'network', label: 'Network', description: 'Proxies.' },
    { id: 'providers', label: 'Providers', description: 'Model credentials.' },
    { id: 'routing', label: 'Routing', description: 'Route selection.' },
    { id: 'batch', label: 'Batch inference', description: 'Batch jobs.' },
    { id: 'alerts', label: 'Alerts', description: 'Backend alerts.' },
    { id: 'future', label: 'Future things', description: 'Added by a later backend.' },
  ],
  entries: [
    entry({ key: 'SITE_NAME', category: 'general', value: 'Gateway', one_time: true }),
    entry({ key: 'LOG_LEVEL', category: 'general', value: 'INFO' }),
    entry({ key: 'COOKIE_DOMAIN', category: 'security', one_time: true }),
    entry({ key: 'SESSION_HOURS', category: 'security', type: 'int', value: 12 }),
    entry({ key: 'SIGNUP_NOTIFY_EMAILS', category: 'signup', value: 'ops@example.test' }),
    entry({
      key: 'SMTP_HOST',
      category: 'email',
      required: true,
      missing: true,
      is_set: false,
      value: null,
      source: 'default',
      one_time: true,
    }),
    entry({
      key: 'TRUST_PROXY_HEADERS',
      category: 'network',
      type: 'bool',
      value: false,
      one_time: true,
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
    entry({ key: 'ROUTE_TIMEOUT', category: 'routing', type: 'int', value: 30 }),
    entry({ key: 'BATCH_RETENTION_DAYS', category: 'batch', type: 'int', value: 7 }),
    entry({ key: 'ALERTS_ENABLED', category: 'alerts', type: 'bool', value: true }),
    webhook('SLACK_ALERTS_WEBHOOK_URL'),
    webhook('WECOM_ALERTS_WEBHOOK_URL'),
    webhook('SLACK_WEBHOOK_URL'),
    entry({ key: 'DEPLOYMENT_ENV', category: 'alerts', value: 'staging', one_time: true }),
    entry({ key: 'FUTURE_KNOB', category: 'future', value: 'x' }),
  ],
  missing: ['SMTP_HOST', 'ZAI_API_KEY'],
  pending_restart: [],
  restart_supported: true,
};

function runtime(key: string, value: boolean | number): RuntimeSettingItem {
  return {
    key,
    value,
    value_type: typeof value === 'boolean' ? 'bool' : 'int',
    default_value: value,
    description: key,
    min: null,
    max: null,
  };
}

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  getConfig: vi.fn(async () => CONFIG),
  patchConfig: vi.fn(),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // Every section reads on mount, whatever sub-tab is open; unmocked they
    // would reach the real gateway (see vitest.setup.ts).
    getAlertSnooze: vi.fn(async () => ({
      snoozed: false,
      snooze_until: null,
      seconds_remaining: 0,
    })),
    listAlertMutes: vi.fn(async () => ({ types: [] })),
    getUsageInsightsSettings: vi.fn(async () => ({
      configured: false,
      api_key_hint: null,
      model: 'gpt-4o-mini',
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listModelVisibility: vi.fn(async () => ({ models: [] })),
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listBatchModelLimits: vi.fn(async () => ({ default_concurrency: 4, models: [] })),
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        runtime('log_full_payload', false),
        runtime('signup_enabled', true),
        runtime('auto_mode_enabled', false),
        runtime('user_concurrency_free', 3),
        runtime('user_daily_quota_free', 100),
        // A setting no group names, from a later backend.
        runtime('mystery_knob', 5),
      ],
    })),
    updateRuntimeSetting: vi.fn(),
  };
});

import { getConfig, patchConfig } from '@/lib/api/config';
import { listRuntimeSettings, updateRuntimeSetting } from '@/lib/api/admin';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** The sub-tabs are these, in this order, named as a screen reader hears them. */
function expectTabs(names: string[]) {
  const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
  const tabs = within(tablist).getAllByRole('tab');
  expect(tabs).toHaveLength(names.length);
  names.forEach((name, index) =>
    expect(within(tablist).getByRole('tab', { name })).toBe(tabs[index]),
  );
}

/** The sections of the sub-tab on screen, top to bottom. */
function sectionsShown() {
  return within(screen.getByRole('tabpanel'))
    .getAllByRole('region')
    .map((region) => region.getAttribute('aria-labelledby'))
    .map((id) => document.getElementById(id!)?.textContent);
}

/** A category's rows, top to bottom: each setting by its name, and the heading of those set once. */
function rowsOf(category: string) {
  return Array.from(
    screen
      .getByRole('region', { name: category })
      .querySelectorAll<HTMLElement>('[data-config-key], h3'),
  ).map((row) => row.dataset.configKey ?? row.textContent);
}

/** Render, and wait for the runtime settings, which arrive after the configuration. */
async function renderLoaded(...args: Parameters<typeof renderSettings>) {
  await renderSettings(...args);
  await screen.findByLabelText('mystery_knob value');
}

describe('Admin → Settings sub-tabs', () => {
  it('opens on the common settings, then groups the rest by topic, what is set once last', async () => {
    await renderLoaded();

    expectTabs([
      'Common',
      'Access',
      'Providers & Routing, 1 missing',
      'Models',
      'Batch',
      'Alerts',
      'Setup, 1 missing',
      'Backup',
    ]);
    expect(screen.getByRole('tab', { name: 'Common' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText(/^The settings changed most often\./)).toBeVisible();

    const expected: Record<string, (string | undefined)[]> = {
      // Every runtime setting, the ones the Settings tab held before the
      // configuration moved in; one no group names still has a place.
      Common: [
        'Access Controls',
        'Per-User Concurrency',
        'Daily Quotas',
        'Request Handling',
        'Request Logging',
        'Other Runtime Settings',
      ],
      Access: ['Signup Policy', 'Sign-up', 'Security'],
      'Providers & Routing': ['Providers', 'Routing'],
      Models: ['Model Visibility', 'Model Concurrency Limit'],
      Batch: ['Batch inference', 'Batch Concurrency'],
      Alerts: ['Alert Notifications', 'Alert Types', 'Alerts'],
      // A category no sub-tab names still has a place.
      Setup: ['General', 'Email', 'Network', 'Usage Insights', 'Future things'],
      Backup: ['Backup & Restore'],
    };
    for (const [tab, sections] of Object.entries(expected)) {
      await openSettingsTab(new RegExp(`^${tab.replace(/[&]/g, '\\$&')}`));
      expect(sectionsShown(), tab).toEqual(sections);
    }

    // Each runtime setting is in its topic's group.
    await openSettingsTab('Common');
    expect(
      within(screen.getByRole('region', { name: 'Daily Quotas' })).getByRole('button', {
        name: 'Apply to existing users',
      }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Per-User Concurrency' })).getByLabelText(
        'user_concurrency_free value',
      ),
    ).toBeInTheDocument();
  });

  it('lists the settings usually set once after the others of their category', async () => {
    await renderLoaded('Setup, 1 missing');
    expect(
      screen.getByText(/^Settings usually set once, when the gateway is deployed/),
    ).toBeVisible();
    expect(rowsOf('General')).toEqual(['LOG_LEVEL', 'Usually set once', 'SITE_NAME']);
    // A category of nothing else needs no heading for them.
    expect(rowsOf('Network')).toEqual(['TRUST_PROXY_HEADERS']);
    expect(rowsOf('Email')).toEqual(['SMTP_HOST']);

    await openSettingsTab('Access');
    expect(rowsOf('Security')).toEqual(['SESSION_HOURS', 'Usually set once', 'COOKIE_DOMAIN']);
    expect(rowsOf('Sign-up')).toEqual(['SIGNUP_NOTIFY_EMAILS']);

    // The alert channel goes with its webhooks, which are set once.
    await openSettingsTab('Alerts');
    expect(rowsOf('Alerts')).toEqual(['ALERTS_ENABLED', 'Usually set once', 'DEPLOYMENT_ENV']);
    const alerts = screen.getByRole('region', { name: 'Alerts' });
    const heading = within(alerts).getByRole('heading', { name: 'Usually set once' });
    const channel = within(alerts).getByText('Notification channel');
    expect(heading.compareDocumentPosition(channel)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(channel.compareDocumentPosition(within(alerts).getByLabelText('DEPLOYMENT_ENV'))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('keeps the order of a backend that does not say what is set once', async () => {
    vi.mocked(getConfig).mockResolvedValueOnce({
      ...CONFIG,
      entries: CONFIG.entries.map((item) => {
        const { one_time: _oneTime, ...rest } = item;
        return rest;
      }),
    });
    await renderLoaded('Setup, 1 missing');

    expect(rowsOf('General')).toEqual(['SITE_NAME', 'LOG_LEVEL']);
    await openSettingsTab('Alerts');
    expect(rowsOf('Alerts')).toEqual(['ALERTS_ENABLED', 'DEPLOYMENT_ENV']);
    const alerts = screen.getByRole('region', { name: 'Alerts' });
    expect(
      within(alerts)
        .getByText('Notification channel')
        .compareDocumentPosition(within(alerts).getByLabelText('ALERTS_ENABLED')),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('opens the sub-tab a link names, and puts the one chosen in the address', async () => {
    await renderLoaded(undefined, { initialTab: 'alerts' });
    expect(screen.getByRole('tab', { name: 'Alerts' })).toHaveAttribute('aria-selected', 'true');
    expect(sectionsShown()).toEqual(['Alert Notifications', 'Alert Types', 'Alerts']);

    await openSettingsTab('Access');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings?tab=access', {
      scroll: false,
    });
    await openSettingsTab('Common');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
  });

  it('keeps unsaved changes while another sub-tab is open, and says where they are', async () => {
    await renderLoaded('Access');
    expect(screen.getByRole('tab', { name: 'Access' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.change(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS'), {
      target: { value: 'admins@example.test' },
    });
    expect(screen.getByRole('tab', { name: 'Access, unsaved changes' })).toBeInTheDocument();

    await openSettingsTab('Common');
    expect(screen.queryByLabelText('SIGNUP_NOTIFY_EMAILS')).not.toBeVisible();
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle signup enabled' }));
    expect(screen.getByRole('tab', { name: 'Common, unsaved changes' })).toBeInTheDocument();

    await openSettingsTab('Models');
    // Still said on the tabs that are not on screen.
    expect(screen.getByRole('tab', { name: /^Access/ })).toHaveTextContent('unsaved changes');
    expect(screen.getByRole('tab', { name: /^Common/ })).toHaveTextContent('unsaved changes');

    await openSettingsTab(/^Access/);
    expect(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS')).toHaveValue('admins@example.test');
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
    expect(screen.getByRole('tab', { name: 'Access' })).toBeInTheDocument();

    await openSettingsTab(/^Common/);
    expect(screen.getByRole('switch', { name: 'Toggle signup enabled' })).toHaveAttribute(
      'aria-checked',
      'false',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Access Controls' }));
    expect(screen.getByRole('tab', { name: 'Common' })).toBeInTheDocument();
    expect(patchConfig).not.toHaveBeenCalled();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('searches the configuration on every sub-tab, and hides the rest until cleared', async () => {
    await renderLoaded();

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'glm' } });
    // No sub-tab is chosen while a filter lists every one of them.
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.getByText(/1 setting matches, from every sub-tab/)).toBeInTheDocument();
    const providers = screen.getByRole('region', { name: 'Providers' });
    expect(providers).toHaveTextContent('Providers & Routing');
    expect(within(providers).getByLabelText('ZAI_API_KEY')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'General' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Access Controls' })).not.toBeInTheDocument();
    expect(screen.queryByText(/^The settings changed most often\./)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Search settings')).toHaveValue('');
    expect(screen.getByRole('tab', { name: 'Common' })).toHaveAttribute('aria-selected', 'true');
    expect(sectionsShown()).toContain('Access Controls');
  });

  it('opens on what is missing, from every sub-tab, when linked that way', async () => {
    await renderLoaded(undefined, { initialMissingOnly: true });

    expect(screen.getByRole('checkbox', { name: 'Missing only' })).toBeChecked();
    const email = screen.getByRole('region', { name: 'Email' });
    expect(email).toHaveTextContent('Setup');
    expect(within(email).getByLabelText('SMTP_HOST')).toBeInTheDocument();
    expect(
      within(screen.getByRole('region', { name: 'Providers' })).getByLabelText('ZAI_API_KEY'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Security' })).not.toBeInTheDocument();

    // Clearing drops the `?missing=1` the page was opened with.
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
  });

  it('keeps the runtime settings when the backend has no configuration API', async () => {
    vi.mocked(getConfig).mockRejectedValueOnce(new APIError('HTTP_404', 'Not Found', 404));
    await renderLoaded();

    expect(screen.getByRole('alert')).toHaveTextContent(
      'does not support database-backed configuration',
    );
    // Sub-tabs that only edit the configuration have nothing to show.
    expectTabs(['Common', 'Access', 'Models', 'Batch', 'Alerts', 'Setup']);
    expect(sectionsShown()).toEqual([
      'Access Controls',
      'Per-User Concurrency',
      'Daily Quotas',
      'Request Handling',
      'Request Logging',
      'Other Runtime Settings',
    ]);
    expect(screen.queryByLabelText('Search settings')).not.toBeInTheDocument();
    await waitFor(() => expect(listRuntimeSettings).toHaveBeenCalledTimes(1));
  });
});
