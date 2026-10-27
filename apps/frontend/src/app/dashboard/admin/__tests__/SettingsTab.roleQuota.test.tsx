// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_CONFIG, openSettingsTab, renderSettings } from './settingsTabHarness';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab also mounts AlertSnoozeSection, AlertMutesSection and
    // UsageInsightsSettingsSection, which fetch on mount. Unmocked they
    // reach the real gateway (see vitest.setup.ts).
    // SettingsTab issues these reads on mount regardless of which tab
    // the test exercises; unmocked they reach the real gateway.
    listModelVisibility: vi.fn(async () => ({ models: [] })),
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listBatchModelLimits: vi.fn(async () => ({ default_concurrency: 4, models: [] })),
    listRoutewiseSettings: vi.fn(async () => ({ settings: [] })),
    listRouteWeights: vi.fn(async () => []),
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
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'user_daily_quota_pro',
          value: 250,
          value_type: 'float',
          default_value: 100,
          description: 'pro quota',
          min: 0,
          max: null,
        },
      ],
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    previewRoleQuotaApply: vi.fn(async () => ({
      role: 'pro',
      quota: 250,
      keys_affected: 42,
      users_affected: 38,
    })),
    applyRoleQuota: vi.fn(async () => ({ role: 'pro', quota: 250, keys_updated: 42 })),
    removeSignupAllowedDomain: vi.fn(async () => {}),
    updateRuntimeSetting: vi.fn(),
  };
});

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  // The page also holds the configuration; these tests need none of it.
  getConfig: vi.fn(async () => EMPTY_CONFIG),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsTab role quota', () => {
  it('renders Apply button only on quota settings and runs the confirm flow', async () => {
    const api = await import('@/lib/api/admin');
    await renderSettings('Limits');

    const applyBtn = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.click(applyBtn);
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));

    const dialog = await screen.findByRole('dialog', {
      name: 'Apply the pro quota to existing keys?',
    });
    expect(dialog).toHaveTextContent('42');
    expect(dialog).toHaveTextContent(/pro/i);
    expect(api.applyRoleQuota).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply quota' }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro'));
    expect(api.applyRoleQuota).toHaveBeenCalledTimes(1);
  });

  it('holds Apply back while its quota has an unsaved change', async () => {
    const api = await import('@/lib/api/admin');
    await renderSettings('Limits');

    // Applying writes the saved quota, not the one being typed.
    const apply = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.change(screen.getByLabelText('user_daily_quota_pro value'), {
      target: { value: '300' },
    });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(api.previewRoleQuotaApply).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Daily Quotas' }));
    expect(apply).toBeEnabled();
  });

  it('keeps an open Remove confirmation when the quota preview arrives after it', async () => {
    const api = await import('@/lib/api/admin');
    vi.mocked(api.listSignupAllowedDomains).mockResolvedValue({
      domains: [
        {
          domain: 'example.com',
          is_wildcard: false,
          created_at: null,
          created_by: null,
          created_by_email: null,
        },
      ],
    });
    // The preview is still out when the domain's removal is asked for, and
    // lands while its confirmation is open.
    let finishPreview!: (value: Awaited<ReturnType<typeof api.previewRoleQuotaApply>>) => void;
    vi.mocked(api.previewRoleQuotaApply).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPreview = resolve;
        }),
    );
    await renderSettings('Limits');

    fireEvent.click(await screen.findByRole('button', { name: /apply to existing users/i }));
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));
    await openSettingsTab('Access');
    fireEvent.click(await screen.findByRole('button', { name: 'Remove example.com' }));
    const review = screen.getByRole('dialog', {
      name: 'Remove example.com from the signup allowlist?',
    });
    const remove = within(review).getByRole('button', { name: 'Remove' });

    await act(async () =>
      finishPreview({ role: 'pro', quota: 250, keys_affected: 42, users_affected: 38 }),
    );

    // Same dialog, same action: the quota confirmation did not replace it.
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(
      screen.getByRole('dialog', { name: 'Remove example.com from the signup allowlist?' }),
    ).toHaveTextContent('Existing users keep their current status.');
    expect(remove).toHaveTextContent('Remove');
    fireEvent.click(remove);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(api.removeSignupAllowedDomain).mock.calls).toEqual([['example.com', false]]);
    expect(api.applyRoleQuota).not.toHaveBeenCalled();

    // Applying was not queued behind it: it needs its own click and review.
    expect(screen.queryByRole('dialog')).toBeNull();
    await openSettingsTab('Limits');
    fireEvent.click(screen.getByRole('button', { name: /apply to existing users/i }));
    expect(
      await screen.findByRole('dialog', { name: 'Apply the pro quota to existing keys?' }),
    ).toBeInTheDocument();
    expect(api.applyRoleQuota).not.toHaveBeenCalled();
  });
});
