// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab also mounts AlertSnoozeSection, UsageInsightsSettingsSection
    // and AgentRunnerHostSection, which fetch on mount. Unmocked they reach
    // the real gateway (see vitest.setup.ts).
    // SettingsTab issues these reads on mount regardless of which tab
    // the test exercises; unmocked they reach the real gateway.
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listRoutewiseSettings: vi.fn(async () => ({ settings: [] })),
    listRouteWeights: vi.fn(async () => []),
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
    updateModelVisibility: vi.fn(async () => ({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    })),
  };
});

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsTab />
    </QueryClientProvider>,
  );
}

describe('SettingsTab model visibility', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the section inside Admin Settings and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');

    renderSettings();

    expect(await screen.findByText('Model Visibility')).toBeInTheDocument();
    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(api.updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(await screen.findByText('Updated visibility for gpt-4o-mini.')).toBeInTheDocument();
  });

  it('does not render routing subtabs inside Settings', async () => {
    renderSettings();

    expect(await screen.findByText('Feature Flags')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Routing' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Routewise' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Routing Weights' })).not.toBeInTheDocument();
  });
});
