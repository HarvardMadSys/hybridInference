// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab also mounts AlertSnoozeSection and
    // UsageInsightsSettingsSection, which fetch on mount. Unmocked they reach
    // the real gateway (see vitest.setup.ts).
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
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro'));
  });
});
