// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

const replaceMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: replaceMock }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab also mounts AlertSnoozeSection, UsageInsightsSettingsSection
    // and AgentRunnerHostSection, which fetch on mount. Unmocked they reach
    // the real gateway (see vitest.setup.ts).
    getAgentRunnerHosts: vi.fn(async () => ({ hosts: [], active_host: null })),
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
    listRuntimeSettings: vi.fn(async () => ({ settings: [] })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listModelVisibility: vi.fn(async () => ({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    })),
    updateModelVisibility: vi.fn(),
    listModelConcurrency: vi.fn(async () => ({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    })),
    updateModelConcurrency: vi.fn(async () => ({
      model_id: 'gpt-4o-mini',
      exempt: true,
    })),
    listRoutewiseSettings: vi.fn(async () => ({ settings: [] })),
    updateRoutewiseSetting: vi.fn(),
    listRouteWeights: vi.fn(async () => []),
    setRouteWeight: vi.fn(),
    clearRouteWeight: vi.fn(),
  };
});

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

describe('SettingsTab model concurrency', () => {
  afterEach(() => {
    cleanup();
    replaceMock.mockClear();
  });

  it('renders the section inside Admin Settings and shows update toasts', async () => {
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

    expect(await screen.findByText('Model Concurrency Limit')).toBeInTheDocument();
    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(api.updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', true);
    });
    expect(
      await screen.findByText('Updated concurrency exemption for gpt-4o-mini.'),
    ).toBeInTheDocument();
  });
});
