// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

describe('SettingsTab batch concurrency', () => {
  afterEach(() => {
    cleanup();
  });

  it('lists the section after Model Concurrency and shows update toasts', async () => {
    const api = await import('@/lib/api/admin');

    render(
      <QueryClientProvider client={new QueryClient()}>
        <SettingsTab />
      </QueryClientProvider>,
    );

    const index = screen.getByRole('navigation', { name: 'Settings sections' });
    const labels = within(index)
      .getAllByRole('link')
      .map((link) => link.textContent);
    expect(labels.indexOf('Batch Concurrency')).toBe(labels.indexOf('Model Concurrency') + 1);

    expect(await screen.findByText('glm-4.7')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Max concurrent batch requests for glm-4.7'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Batch Concurrency' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }),
    );

    await waitFor(() => {
      expect(api.updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 2);
    });
    expect(await screen.findByText('Updated batch concurrency for glm-4.7.')).toBeInTheDocument();
  });
});
