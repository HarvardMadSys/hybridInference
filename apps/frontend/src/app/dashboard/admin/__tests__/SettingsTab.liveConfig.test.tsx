// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ConfigEntry, ConfigResponse, LiveConfigStatus } from '@/lib/api/config';
import { APIError } from '@/lib/utils/errors';

import { openSettingsTab, renderSettings } from './settingsTabHarness';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  getConfig: vi.fn(),
  patchConfig: vi.fn(),
  getLiveConfigStatus: vi.fn(),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // Every section reads on mount, whatever sub-tab is open.
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
    listRuntimeSettings: vi.fn(async () => ({ settings: [] })),
    updateRuntimeSetting: vi.fn(),
  };
});

import { getConfig, getLiveConfigStatus, patchConfig } from '@/lib/api/config';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const v = (revision: number) => ({ epoch: 'e1', revision });

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

/** A setting on Setup, one on Providers & Routing, and the alert threshold in its live group. */
function config(overrides: Partial<ConfigResponse> = {}): ConfigResponse {
  return {
    categories: [
      { id: 'general', label: 'General', description: 'Site identity.' },
      { id: 'providers', label: 'Providers', description: 'Model credentials.' },
      { id: 'alerts', label: 'Alerts', description: 'Backend alerts.' },
    ],
    entries: [
      entry({ key: 'SITE_NAME', category: 'general', value: 'Gateway', one_time: true }),
      entry({ key: 'ZAI_BASE_URL', category: 'providers', value: 'https://zai.test' }),
      entry({
        key: 'FAILED_REQUEST_ALERT_THRESHOLD',
        category: 'alerts',
        type: 'int',
        value: 200,
        restart_required: true,
        group: 'alerts.runtime',
        apply_mode: 'managed',
      }),
    ],
    missing: [],
    pending_restart: [],
    restart_supported: true,
    source_version: v(1),
    ...overrides,
  };
}

function liveStatus(revision: number): LiveConfigStatus {
  return {
    database: true,
    source: 'app_config',
    version: v(revision),
    this_instance: 'p1',
    stale_after_seconds: 60,
    read_ok: true,
    unknown_groups: [],
    groups: [
      {
        name: 'alerts.runtime',
        keys: ['FAILED_REQUEST_ALERT_THRESHOLD'],
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
            status: { state: 'applied', epoch: 'e1', reconciled_revision: revision },
          },
        ],
        local: null,
      },
    ],
  };
}

describe('the live configuration on Admin → Settings sub-tabs', () => {
  it('keeps an Add variable draft and its version across sub-tabs, chains it on a save, and adds again after a conflict', async () => {
    vi.mocked(getConfig).mockResolvedValue(config());
    vi.mocked(getLiveConfigStatus).mockResolvedValue(liveStatus(1));
    await renderSettings('Providers & Routing');

    fireEvent.click(await screen.findByRole('button', { name: 'Add variable' }));
    const form = screen.getByRole('form', { name: 'Add variable' });
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'MY_REGION' } });
    fireEvent.change(within(form).getByLabelText('Value'), { target: { value: 'eu-west' } });

    // A save of another category, on another sub-tab, while the form is open.
    await openSettingsTab('Setup');
    const general = screen.getByRole('region', { name: 'General' });
    fireEvent.change(within(general).getByLabelText('SITE_NAME'), { target: { value: 'New' } });
    // Saved as revision 2; revision 3 is someone else's, which the form has not seen.
    vi.mocked(patchConfig).mockResolvedValueOnce(
      config({ source_version: v(3), saved_version: v(2) }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to General' }));
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1));
    expect(vi.mocked(patchConfig).mock.calls[0]).toEqual([
      { values: { SITE_NAME: 'New' } },
      { expectedVersion: v(1) },
    ]);
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save changes to General' })).toBeNull(),
    );

    // Back on its sub-tab, the form still holds what was typed. Its add is
    // conditional on what this page's own save committed, and meets a save
    // made elsewhere since.
    await openSettingsTab('Providers & Routing');
    expect(within(form).getByLabelText('Name')).toHaveValue('MY_REGION');
    expect(within(form).getByLabelText('Value')).toHaveValue('eu-west');
    vi.mocked(patchConfig).mockRejectedValueOnce(
      new APIError('CONFIG_VERSION_CONFLICT', 'The configuration changed.', 409),
    );
    vi.mocked(getConfig).mockResolvedValueOnce(config({ source_version: v(4) }));
    fireEvent.click(within(form).getByRole('button', { name: 'Add variable' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      /changed elsewhere since this form opened, so the variable was not added/,
    );
    expect(vi.mocked(patchConfig).mock.calls[1][1]).toEqual({ expectedVersion: v(2) });
    expect(within(form).getByLabelText('Value')).toHaveValue('eu-west');

    // Nothing is retried for the administrator: adding again uses the reloaded version.
    vi.mocked(patchConfig).mockResolvedValueOnce(
      config({ source_version: v(5), saved_version: v(5) }),
    );
    fireEvent.click(within(form).getByRole('button', { name: 'Add again' }));
    await waitFor(() => expect(screen.queryByRole('form', { name: 'Add variable' })).toBeNull());
    expect(vi.mocked(patchConfig).mock.calls[2]).toEqual([
      { values: { MY_REGION: 'eu-west' }, secrets: { MY_REGION: false } },
      { expectedVersion: v(4) },
    ]);
  });

  it('shows the live group status and badge in the Alerts sub-tab, and re-reads it after a save', async () => {
    vi.mocked(getConfig).mockResolvedValue(config({ source_version: v(7) }));
    vi.mocked(getLiveConfigStatus).mockResolvedValue(liveStatus(7));
    await renderSettings('Alerts');

    const alerts = screen.getByRole('region', { name: 'Alerts' });
    expect(within(alerts).getByText('Applies live')).toBeInTheDocument();
    expect(
      await within(alerts).findByText('1 of 1 registered process has applied revision 7', {
        exact: false,
      }),
    ).toBeInTheDocument();
    const reads = vi.mocked(getLiveConfigStatus).mock.calls.length;

    fireEvent.change(within(alerts).getByLabelText('FAILED_REQUEST_ALERT_THRESHOLD'), {
      target: { value: '300' },
    });
    vi.mocked(getLiveConfigStatus).mockResolvedValue(liveStatus(8));
    vi.mocked(patchConfig).mockResolvedValueOnce(
      config({ source_version: v(8), saved_version: v(8) }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Alerts' }));
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1));
    expect(vi.mocked(patchConfig).mock.calls[0]).toEqual([
      { values: { FAILED_REQUEST_ALERT_THRESHOLD: 300 } },
      { expectedVersion: v(7) },
    ]);
    // The saved revision changes the panel's key, so it reads at once rather than at its next poll.
    await waitFor(() =>
      expect(vi.mocked(getLiveConfigStatus).mock.calls.length).toBeGreaterThan(reads),
    );
    expect(
      await within(alerts).findByText('1 of 1 registered process has applied revision 8', {
        exact: false,
      }),
    ).toBeInTheDocument();
  });
});
