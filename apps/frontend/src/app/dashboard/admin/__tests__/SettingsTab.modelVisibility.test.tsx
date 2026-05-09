// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
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

describe('SettingsTab model visibility', () => {
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

    expect(await screen.findByText('Model Visibility')).toBeInTheDocument();
    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(api.updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(await screen.findByText('Updated visibility for gpt-4o-mini.')).toBeInTheDocument();
  });
});
