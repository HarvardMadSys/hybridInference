// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_CONFIG, renderSettings } from './settingsTabHarness';

const replaceMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: replaceMock, refresh: vi.fn() }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab also mounts AlertSnoozeSection, AlertMutesSection and
    // UsageInsightsSettingsSection, which fetch on mount. Unmocked they
    // reach the real gateway (see vitest.setup.ts).
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
    updateModelVisibility: vi.fn(),
    listModelConcurrency: vi.fn(async () => ({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    })),
    updateModelConcurrency: vi.fn(async () => ({
      model_id: 'gpt-4o-mini',
      exempt: true,
    })),
    listBatchModelLimits: vi.fn(async () => ({ default_concurrency: 4, models: [] })),
    listRoutewiseSettings: vi.fn(async () => ({ settings: [] })),
    updateRoutewiseSetting: vi.fn(),
    listRouteWeights: vi.fn(async () => []),
    setRouteWeight: vi.fn(),
    clearRouteWeight: vi.fn(),
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

describe('SettingsTab model concurrency', () => {
  afterEach(() => {
    cleanup();
    replaceMock.mockClear();
  });

  it('renders the section inside Admin Settings and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');
    await renderSettings('Models');

    expect(await screen.findByText('Model Concurrency Limit')).toBeInTheDocument();
    fireEvent.click(await screen.findByLabelText('Concurrency exemption for gpt-4o-mini'));
    expect(api.updateModelConcurrency).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    );

    await waitFor(() => {
      expect(api.updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', true);
    });
    expect(
      await screen.findByText('Updated concurrency exemption for gpt-4o-mini.'),
    ).toBeInTheDocument();
  });
});
