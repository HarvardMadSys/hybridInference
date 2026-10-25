// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

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
    removeSignupAllowedDomain: vi.fn(async () => {}),
    updateRuntimeSetting: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSettings() {
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
}

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
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: /apply to existing users/i }));
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));
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
    ).toHaveTextContent('will require admin approval');
    expect(remove).toHaveTextContent('Remove');
    fireEvent.click(remove);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(api.removeSignupAllowedDomain).mock.calls).toEqual([['example.com', false]]);
    expect(api.applyRoleQuota).not.toHaveBeenCalled();

    // Applying was not queued behind it: it needs its own click and review.
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /apply to existing users/i }));
    expect(
      await screen.findByRole('dialog', { name: 'Apply the pro quota to existing keys?' }),
    ).toBeInTheDocument();
    expect(api.applyRoleQuota).not.toHaveBeenCalled();
  });
});
