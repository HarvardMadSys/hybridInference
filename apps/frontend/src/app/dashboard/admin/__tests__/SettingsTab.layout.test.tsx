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
    custom: false,
    invalid: null,
    used_by: [],
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

/** One setting in each category a sub-tab names, and one in a category none names. */
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
    entry({ key: 'SITE_NAME', category: 'general', value: 'Gateway' }),
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
    }),
    entry({ key: 'TRUST_PROXY_HEADERS', category: 'network', type: 'bool', value: false }),
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

/** Render, and wait for the runtime settings, which arrive after the configuration. */
async function renderLoaded(...args: Parameters<typeof renderSettings>) {
  await renderSettings(...args);
  await screen.findByLabelText('mystery_knob value');
}

describe('Admin → Settings sub-tabs', () => {
  it('groups every section by topic, whatever stores it', async () => {
    await renderLoaded();

    expectTabs([
      'General',
      'Access, 1 missing',
      'Providers & Routing, 1 missing',
      'Models',
      'Limits',
      'Batch',
      'Alerts',
      'Backup',
    ]);
    expect(screen.getByRole('tab', { name: 'General' })).toHaveAttribute('aria-selected', 'true');

    const expected: Record<string, (string | undefined)[]> = {
      General: [
        'General',
        'Request Logging',
        'Network',
        'Usage Insights',
        // A category and a runtime setting no sub-tab names still have a place.
        'Future things',
        'Other Runtime Settings',
      ],
      Access: ['Access Controls', 'Sign-up', 'Signup Policy', 'Email', 'Security'],
      'Providers & Routing': ['Providers', 'Routing'],
      Models: ['Request Handling', 'Model Visibility'],
      Limits: ['Per-User Concurrency', 'Model Concurrency Limit', 'Daily Quotas'],
      Batch: ['Batch inference', 'Batch Concurrency'],
      Alerts: ['Alerts', 'Alert Notifications', 'Alert Types'],
      Backup: ['Backup & Restore'],
    };
    for (const [tab, sections] of Object.entries(expected)) {
      await openSettingsTab(new RegExp(`^${tab.replace(/[&]/g, '\\$&')}`));
      expect(sectionsShown(), tab).toEqual(sections);
    }
    // Each runtime setting is in its topic's group.
    await openSettingsTab('Limits');
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

  it('opens the sub-tab a link names, and puts the one chosen in the address', async () => {
    await renderLoaded(undefined, { initialTab: 'alerts' });
    expect(screen.getByRole('tab', { name: 'Alerts' })).toHaveAttribute('aria-selected', 'true');
    expect(sectionsShown()).toEqual(['Alerts', 'Alert Notifications', 'Alert Types']);

    await openSettingsTab(/^Access/);
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings?tab=access', {
      scroll: false,
    });
    await openSettingsTab('General');
    expect(replaceMock).toHaveBeenLastCalledWith('/dashboard/admin/settings', { scroll: false });
  });

  it('keeps unsaved changes while another sub-tab is open, and says where they are', async () => {
    await renderLoaded('Access, 1 missing');
    expect(screen.getByRole('tab', { name: 'Access, 1 missing' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    fireEvent.change(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS'), {
      target: { value: 'admins@example.test' },
    });
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle signup enabled' }));
    expect(
      screen.getByRole('tab', { name: 'Access, unsaved changes, 1 missing' }),
    ).toBeInTheDocument();

    await openSettingsTab('Models');
    expect(screen.queryByLabelText('SIGNUP_NOTIFY_EMAILS')).not.toBeVisible();
    // Still said on the tab that is not on screen.
    expect(screen.getByRole('tab', { name: /^Access/ })).toHaveTextContent('unsaved changes');

    await openSettingsTab(/^Access/);
    expect(screen.getByLabelText('SIGNUP_NOTIFY_EMAILS')).toHaveValue('admins@example.test');
    expect(screen.getByRole('switch', { name: 'Toggle signup enabled' })).toHaveAttribute(
      'aria-checked',
      'false',
    );

    // The marker stays while either change is unsaved.
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Sign-up' }));
    expect(screen.getByRole('tab', { name: /^Access/ })).toHaveTextContent('unsaved changes');
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Access Controls' }));
    expect(screen.getByRole('tab', { name: 'Access, 1 missing' })).toBeInTheDocument();
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
    expect(screen.queryByRole('region', { name: 'Usage Insights' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByLabelText('Search settings')).toHaveValue('');
    expect(screen.getByRole('tab', { name: 'General' })).toHaveAttribute('aria-selected', 'true');
    expect(sectionsShown()).toContain('Usage Insights');
  });

  it('opens on what is missing, from every sub-tab, when linked that way', async () => {
    await renderLoaded(undefined, { initialMissingOnly: true });

    expect(screen.getByRole('checkbox', { name: 'Missing only' })).toBeChecked();
    const email = screen.getByRole('region', { name: 'Email' });
    expect(email).toHaveTextContent('Access');
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
    expectTabs(['General', 'Access', 'Models', 'Limits', 'Batch', 'Alerts']);
    expect(sectionsShown()).toEqual([
      'Request Logging',
      'Usage Insights',
      'Other Runtime Settings',
    ]);
    expect(screen.queryByLabelText('Search settings')).not.toBeInTheDocument();
    await waitFor(() => expect(listRuntimeSettings).toHaveBeenCalledTimes(1));
  });
});
