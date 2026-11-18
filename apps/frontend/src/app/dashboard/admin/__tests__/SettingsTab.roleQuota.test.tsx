// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_CONFIG, openSettingsTab, renderSettings } from './settingsTabHarness';

/**
 * The harness supplies the page's providers; the accounting currency is the
 * one thing these tests vary, so `useSiteConfig` reports it from here.
 */
const siteConfigState = vi.hoisted(() => ({ accountingCurrency: 'USD' as 'USD' | 'CNY' }));

vi.mock('@/components/providers/SiteConfigProvider', async (importOriginal) => {
  const { buildTimeSiteConfig } = await import('@/config/site-config');
  return {
    ...(await importOriginal<typeof import('@/components/providers/SiteConfigProvider')>()),
    useSiteConfig: () => ({
      ...buildTimeSiteConfig,
      accounting: { currency: siteConfigState.accountingCurrency },
    }),
  };
});

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
  siteConfigState.accountingCurrency = 'USD';
  cleanup();
  vi.clearAllMocks();
});

/**
 * A CNY role quota: the native amounts (100, default 70) are what an admin
 * reads and edits; `value` (15) and `default_value` (10) are the legacy USD
 * figures and must not appear.
 */
const cnyQuotaSetting = {
  key: 'user_daily_quota_pro',
  currency: 'CNY' as const,
  value: 15,
  amount: 100,
  value_type: 'float',
  default_value: 10,
  default_amount: 70,
  // The gateway names the ledger's currency in a role quota's description.
  description: 'Default daily CNY spend quota seeded onto a pro user.',
  min: 0,
  min_amount: 0,
  max: null,
  max_amount: null,
};

/** The harness renders the page; the runtime's accounting unit comes from the state above. */
function renderCnySettings() {
  siteConfigState.accountingCurrency = 'CNY';
  return renderSettings('Common');
}

describe('SettingsTab role quota', () => {
  it('renders Apply button only on quota settings and runs the confirm flow', async () => {
    const api = await import('@/lib/api/admin');
    await renderSettings('Common');

    const applyBtn = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.click(applyBtn);
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));

    const dialog = await screen.findByRole('dialog', {
      name: 'Apply the pro quota to existing keys?',
    });
    expect(dialog).toHaveTextContent('42');
    expect(dialog).toHaveTextContent(/pro/i);
    // The community runtime stays USD: the preview's quota, sent with its currency.
    expect(dialog).toHaveTextContent('$250.00');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply quota' }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro', 'USD'));
    expect(api.applyRoleQuota).toHaveBeenCalledTimes(1);
  });

  it('holds Apply back while its quota has an unsaved change', async () => {
    const api = await import('@/lib/api/admin');
    await renderSettings('Common');

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
    await renderSettings('Common');

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
    await openSettingsTab('Common');
    fireEvent.click(screen.getByRole('button', { name: /apply to existing users/i }));
    expect(
      await screen.findByRole('dialog', { name: 'Apply the pro quota to existing keys?' }),
    ).toBeInTheDocument();
    expect(api.applyRoleQuota).not.toHaveBeenCalled();
  });
});

describe('SettingsTab CNY role quota', () => {
  // The extracted RuntimeSettingsSection edits in place: the native amount is
  // the field's value, and Save writes it with the setting's currency. Quota
  // rows sit in the "Daily Quotas" runtime group, whose title names its bar.
  it('saves the native amount with its currency', async () => {
    const api = await import('@/lib/api/admin');
    vi.mocked(api.listRuntimeSettings).mockResolvedValueOnce({ settings: [cnyQuotaSetting] });
    vi.mocked(api.updateRuntimeSetting).mockResolvedValueOnce({
      ...cnyQuotaSetting,
      value: 18,
      amount: 120,
    });
    renderCnySettings();

    // The native amount, default and currency, never the USD figures.
    const field = await screen.findByLabelText('user_daily_quota_pro value');
    expect(field).toHaveValue(100);
    expect(
      screen.getByText('Default daily CNY spend quota seeded onto a pro user.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Default: 70')).toBeInTheDocument();
    // Nothing to save until the native amount changes.
    expect(
      screen.queryByRole('button', { name: 'Save changes to Daily Quotas' }),
    ).not.toBeInTheDocument();

    fireEvent.change(field, { target: { value: '120' } });
    expect(api.updateRuntimeSetting).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Daily Quotas' }));

    await waitFor(() =>
      expect(api.updateRuntimeSetting).toHaveBeenCalledWith(
        'user_daily_quota_pro',
        120,
        undefined,
        'CNY',
      ),
    );
    expect(api.updateRuntimeSetting).toHaveBeenCalledTimes(1);
    // The native amount is what is sent; the legacy USD value is never written.
    expect(vi.mocked(api.updateRuntimeSetting).mock.calls[0][1]).toBe(120);
    // The saved row shows the native amount the server returned, not its USD value.
    await waitFor(() =>
      expect(screen.getByLabelText('user_daily_quota_pro value')).toHaveValue(120),
    );
    expect(await screen.findByText('user_daily_quota_pro set to 120')).toBeInTheDocument();
  });

  it('keeps the native draft until Discard, which restores the saved amount', async () => {
    const api = await import('@/lib/api/admin');
    vi.mocked(api.listRuntimeSettings).mockResolvedValueOnce({ settings: [cnyQuotaSetting] });
    renderCnySettings();

    const field = await screen.findByLabelText('user_daily_quota_pro value');
    fireEvent.change(field, { target: { value: '130' } });
    expect(field).toHaveValue(130);
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Daily Quotas' }));
    expect(screen.getByLabelText('user_daily_quota_pro value')).toHaveValue(100);
    expect(
      screen.queryByRole('button', { name: 'Save changes to Daily Quotas' }),
    ).not.toBeInTheDocument();
    expect(api.updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('applies the role quota in the currency the preview gave', async () => {
    const api = await import('@/lib/api/admin');
    vi.mocked(api.listRuntimeSettings).mockResolvedValueOnce({ settings: [cnyQuotaSetting] });
    vi.mocked(api.previewRoleQuotaApply).mockResolvedValueOnce({
      role: 'pro',
      currency: 'CNY',
      quota: 15,
      amount: 100,
      keys_affected: 42,
      users_affected: 38,
    });
    vi.mocked(api.applyRoleQuota).mockResolvedValueOnce({
      role: 'pro',
      currency: 'CNY',
      quota: 15,
      amount: 100,
      keys_updated: 42,
    });
    renderCnySettings();

    fireEvent.click(await screen.findByRole('button', { name: /apply to existing users/i }));
    const dialog = await screen.findByRole('dialog', {
      name: 'Apply the pro quota to existing keys?',
    });
    expect(dialog).toHaveTextContent('¥100.00');
    expect(dialog).not.toHaveTextContent('$');

    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply quota' }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro', 'CNY'));
    expect(await screen.findByText('Updated 42 keys for role pro to ¥100.00')).toBeInTheDocument();
  });
});
