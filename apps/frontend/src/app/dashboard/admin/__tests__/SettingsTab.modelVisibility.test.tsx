// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { chooseOption } from '@/test-utils/select';

import { EMPTY_CONFIG, renderSettings } from './settingsTabHarness';

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

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  // The page also holds the configuration; these tests need none of it.
  getConfig: vi.fn(async () => EMPTY_CONFIG),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

describe('SettingsTab model visibility', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the section inside Admin Settings and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');

    await renderSettings('Models');

    expect(await screen.findByRole('heading', { name: 'Model Visibility' })).toBeInTheDocument();
    await screen.findByRole('combobox', { name: 'Runtime override for gpt-4o-mini' });
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    expect(api.updateModelVisibility).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Visibility' }));

    await waitFor(() => {
      expect(api.updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(await screen.findByText('Updated visibility for gpt-4o-mini.')).toBeInTheDocument();
  });

  it('does not render routing subtabs inside Settings', async () => {
    await renderSettings('Setup');

    expect(await screen.findByRole('heading', { name: 'Usage Insights' })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Routing' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Routewise' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Routing Weights' })).not.toBeInTheDocument();
  });
});
