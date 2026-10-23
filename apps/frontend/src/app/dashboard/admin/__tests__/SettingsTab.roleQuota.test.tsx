// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
afterEach(cleanup);

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
    updateRuntimeSetting: vi.fn(),
  };
});

describe('SettingsTab role quota', () => {
  it('renders Apply button only on quota settings and runs the confirm flow', async () => {
    const api = await import('@/lib/api/admin');
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    const applyBtn = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.click(applyBtn);
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('42');
    expect(dialog).toHaveTextContent(/pro/i);

    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro', 'USD'));
  });
});

it('edits a native CNY role quota and sends the draft currency', async () => {
  const api = await import('@/lib/api/admin');
  vi.mocked(api.listRuntimeSettings).mockResolvedValueOnce({
    settings: [
      {
        key: 'user_daily_quota_pro',
        currency: 'CNY',
        value: 15,
        amount: 100,
        value_type: 'float',
        default_value: 10,
        default_amount: 70,
        description: 'quota',
        min: 0,
        min_amount: 0,
        max: null,
        max_amount: null,
      },
    ],
  });
  vi.mocked(api.updateRuntimeSetting).mockResolvedValueOnce({
    key: 'user_daily_quota_pro',
    currency: 'CNY',
    value: 18,
    amount: 120,
    value_type: 'float',
    default_value: 10,
    default_amount: 70,
    description: 'quota',
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <SiteConfigProvider initialConfig={{ ...buildTimeSiteConfig, accounting: { currency: 'CNY' } }}>
      <QueryClientProvider client={queryClient}>
        <SettingsTab />
      </QueryClientProvider>
    </SiteConfigProvider>,
  );
  const field = await screen.findByLabelText('user_daily_quota_pro value');
  expect(field).toHaveValue(100);
  expect(screen.getByText('Daily quota (CNY)')).toBeInTheDocument();
  fireEvent.change(field, { target: { value: '120' } });
  fireEvent.click(within(field.parentElement!).getByRole('button', { name: 'Save' }));
  await waitFor(() =>
    expect(api.updateRuntimeSetting).toHaveBeenCalledWith('user_daily_quota_pro', 120, 'CNY'),
  );
});
