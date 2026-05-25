// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelConcurrencySection } from './ModelConcurrencySection';

vi.mock('@/lib/api/admin', () => ({
  listModelConcurrency: vi.fn(),
  updateModelConcurrency: vi.fn(),
}));

import { listModelConcurrency, updateModelConcurrency } from '@/lib/api/admin';

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function renderWithQueryClient(ui: React.ReactElement, queryClient = createTestQueryClient()) {
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

describe('ModelConcurrencySection', () => {
  const onToast = vi.fn();

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and renders returned model concurrency rows', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    const checkbox = screen.getByLabelText('Concurrency exemption for gpt-4o-mini');
    expect(checkbox).not.toBeChecked();
  });

  it('PATCHes exempt=true when the admin checks the box', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    vi.mocked(updateModelConcurrency).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      exempt: true,
    });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', true);
    });
    expect(onToast).toHaveBeenCalledWith('Updated concurrency exemption for gpt-4o-mini.');
  });

  it('PATCHes exempt=false when the admin unchecks the box', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: true }],
    });
    vi.mocked(updateModelConcurrency).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      exempt: false,
    });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', false);
    });
  });

  it('shows an inline error when loading fails', async () => {
    vi.mocked(listModelConcurrency).mockRejectedValue(new Error('backend unavailable'));

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
  });

  it('retries loading after an inline error', async () => {
    vi.mocked(listModelConcurrency)
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce({
        models: [{ model_id: 'gpt-4o-mini', exempt: false }],
      });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
    const retryButton = screen.getByRole('button', { name: 'Retry' });
    expect(retryButton).toHaveAttribute('type', 'button');
    fireEvent.click(retryButton);

    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    await waitFor(() => {
      expect(listModelConcurrency).toHaveBeenCalledTimes(2);
    });
  });

  it('shows a failure toast when an update fails', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    vi.mocked(updateModelConcurrency).mockRejectedValue(new Error('write failed'));

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(onToast).toHaveBeenCalledWith('Failed to update gpt-4o-mini: write failed');
    });
  });

  it('invalidates the dashboard model list after a successful update', async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    vi.mocked(updateModelConcurrency).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      exempt: true,
    });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />, queryClient);

    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });
});
