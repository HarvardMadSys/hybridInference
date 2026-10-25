// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

async function editSection() {
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Model Concurrency Limit' }));
}

function saveAndConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }));
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
    // Read-only until Edit.
    expect(screen.getByLabelText('Concurrency exemption for gpt-4o-mini')).toHaveTextContent(
      'Not exempt',
    );
    expect(screen.queryByRole('checkbox')).toBeNull();
    await editSection();
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

    await editSection();
    const checkbox = screen.getByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);
    expect(updateModelConcurrency).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    );
    const dialog = screen.getByRole('dialog', { name: 'Save 1 concurrency change?' });
    expect(dialog).toHaveTextContent('gpt-4o-miniNot exempt → Exempt');
    expect(updateModelConcurrency).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));

    await waitFor(() => {
      expect(updateModelConcurrency).toHaveBeenCalledWith('gpt-4o-mini', true);
    });
    expect(updateModelConcurrency).toHaveBeenCalledTimes(1);
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

    await editSection();
    const checkbox = screen.getByLabelText('Concurrency exemption for gpt-4o-mini');
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    saveAndConfirm();

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

  it('keeps the draft and the dialog when an update fails, and retries from it', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    vi.mocked(updateModelConcurrency)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce({ model_id: 'gpt-4o-mini', exempt: true });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.click(screen.getByLabelText('Concurrency exemption for gpt-4o-mini'));
    saveAndConfirm();

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'gpt-4o-mini could not be saved: write failed',
    );
    expect(screen.getByLabelText('Concurrency exemption for gpt-4o-mini')).toBeChecked();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(updateModelConcurrency).toHaveBeenCalledTimes(2);
    expect(onToast).toHaveBeenCalledWith('Updated concurrency exemption for gpt-4o-mini.');
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

    await editSection();
    fireEvent.click(screen.getByLabelText('Concurrency exemption for gpt-4o-mini'));
    saveAndConfirm();

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });

  it('discards drafts on Cancel without writing', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);
    await editSection();
    fireEvent.click(screen.getByLabelText('Concurrency exemption for gpt-4o-mini'));
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Model Concurrency Limit' }));
    expect(screen.getByLabelText('Concurrency exemption for gpt-4o-mini')).toHaveTextContent(
      'Not exempt',
    );
    expect(updateModelConcurrency).not.toHaveBeenCalled();
  });
});
