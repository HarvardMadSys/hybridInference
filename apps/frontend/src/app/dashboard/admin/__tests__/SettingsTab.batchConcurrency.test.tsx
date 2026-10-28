// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_CONFIG, renderSettings } from './settingsTabHarness';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab's other sections read these on mount; unmocked they would
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
    listModelVisibility: vi.fn(async () => ({ models: [] })),
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listBatchModelLimits: vi.fn(async () => ({
      default_concurrency: 4,
      models: [
        {
          model_id: 'glm-4.7',
          max_concurrency: null,
          effective_concurrency: 4,
          running: 1,
          queued: 9,
        },
      ],
    })),
    updateBatchModelLimit: vi.fn(async () => ({
      model_id: 'glm-4.7',
      max_concurrency: 2,
      effective_concurrency: 2,
      running: 1,
      queued: 9,
    })),
  };
});

vi.mock('@/lib/api/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/config')>()),
  // The page also holds the configuration; these tests need none of it.
  getConfig: vi.fn(async () => EMPTY_CONFIG),
}));

describe('SettingsTab batch concurrency', () => {
  afterEach(() => {
    cleanup();
  });

  it('lists the section on the Batch sub-tab and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');

    await renderSettings('Batch');
    expect(
      within(screen.getByRole('tabpanel')).getByRole('region', { name: 'Batch Concurrency' }),
    ).toBeInTheDocument();

    expect(await screen.findByText('glm-4.7')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Max concurrent batch requests for glm-4.7'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Batch Concurrency' }));

    await waitFor(() => {
      expect(api.updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 2);
    });
    expect(await screen.findByText('Updated batch concurrency for glm-4.7.')).toBeInTheDocument();
  });
});
