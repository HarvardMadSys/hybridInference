// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

const refreshMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn(), refresh: refreshMock }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab's other sections read these on mount; unmocked they would
    // reach the real gateway (see vitest.setup.ts).
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
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'signup_require_email_verification',
          value: true,
          value_type: 'bool',
          default_value: true,
          description: 'Require email verification for new signups',
        },
        {
          key: 'max_concurrent_requests_per_user',
          value: 3,
          value_type: 'int',
          default_value: 3,
          description: 'Concurrent requests per user',
          min: 1,
          max: null,
        },
      ],
    })),
    updateRuntimeSetting: vi.fn(),
  };
});

import { updateRuntimeSetting } from '@/lib/api/admin';

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // No answer queued by an earlier test.
  vi.mocked(updateRuntimeSetting).mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// The "required settings are missing" banner reads the server-rendered site
// configuration, and email verification decides whether SMTP is required.
describe('SettingsTab and the site configuration', () => {
  it('re-reads it after a flag changes', async () => {
    vi.mocked(updateRuntimeSetting).mockResolvedValueOnce({
      key: 'signup_require_email_verification',
      value: false,
      value_type: 'bool',
      default_value: true,
      description: 'Require email verification for new signups',
    });
    renderSettings();

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Toggle signup require email verification' }),
    );

    await waitFor(() =>
      expect(updateRuntimeSetting).toHaveBeenCalledWith('signup_require_email_verification', false),
    );
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));
  });

  it('re-reads it after a number is saved', async () => {
    vi.mocked(updateRuntimeSetting).mockResolvedValueOnce({
      key: 'max_concurrent_requests_per_user',
      value: 5,
      value_type: 'int',
      default_value: 3,
      description: 'Concurrent requests per user',
      min: 1,
      max: null,
    });
    renderSettings();

    const input = await screen.findByLabelText('max_concurrent_requests_per_user value');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.click(
      within(input.parentElement as HTMLElement).getByRole('button', { name: 'Save' }),
    );

    await waitFor(() =>
      expect(updateRuntimeSetting).toHaveBeenCalledWith('max_concurrent_requests_per_user', 5),
    );
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));
  });

  it('leaves it alone when the change is refused', async () => {
    vi.mocked(updateRuntimeSetting).mockRejectedValueOnce(new Error('forbidden'));
    renderSettings();

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Toggle signup require email verification' }),
    );

    expect(
      await screen.findByText(/Failed to update signup_require_email_verification/),
    ).toBeInTheDocument();
    expect(refreshMock).not.toHaveBeenCalled();
  });
});
