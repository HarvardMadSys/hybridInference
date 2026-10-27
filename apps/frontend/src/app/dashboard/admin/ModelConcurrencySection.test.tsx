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

function save() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }));
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
    // Editable in place: no Edit step, and no save bar until something changes.
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    const checkbox = screen.getByLabelText('Concurrency exemption for gpt-4o-mini');
    expect(checkbox).not.toBeChecked();
    expect(
      screen.queryByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    ).toBeNull();
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
    // The box is a draft; Save writes it, with no confirmation.
    expect(updateModelConcurrency).not.toHaveBeenCalled();
    save();
    expect(screen.queryByRole('dialog')).toBeNull();

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

    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    save();

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

  it('keeps the draft and shows the error when an update fails, and saves it again', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    vi.mocked(updateModelConcurrency)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce({ model_id: 'gpt-4o-mini', exempt: true });

    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);

    fireEvent.click(await screen.findByLabelText('Concurrency exemption for gpt-4o-mini'));
    save();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'gpt-4o-mini could not be saved: write failed',
    );
    expect(screen.getByLabelText('Concurrency exemption for gpt-4o-mini')).toBeChecked();
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    save();
    await waitFor(() => expect(screen.queryByText('1 unsaved change')).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
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

    fireEvent.click(await screen.findByLabelText('Concurrency exemption for gpt-4o-mini'));
    save();

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });

  it('holds Save, Discard and the boxes while a save is in flight', async () => {
    let finish!: (value: { model_id: string; exempt: boolean }) => void;
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [
        { model_id: 'gpt-4o-mini', exempt: false },
        { model_id: 'claude-3-5-sonnet', exempt: false },
      ],
    });
    vi.mocked(updateModelConcurrency).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);
    fireEvent.click(await screen.findByLabelText('Concurrency exemption for gpt-4o-mini'));
    save();

    const saving = screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' });
    expect(saving).toHaveTextContent('Saving…');
    expect(saving).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to Model Concurrency Limit' }),
    ).toBeDisabled();
    expect(screen.getByLabelText('Concurrency exemption for claude-3-5-sonnet')).toBeDisabled();
    fireEvent.click(saving);
    expect(updateModelConcurrency).toHaveBeenCalledTimes(1);

    finish({ model_id: 'gpt-4o-mini', exempt: true });
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
      ).toBeNull(),
    );
    expect(updateModelConcurrency).toHaveBeenCalledTimes(1);
  });

  it('discards drafts on Discard without writing', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);
    fireEvent.click(await screen.findByLabelText('Concurrency exemption for gpt-4o-mini'));
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(
      screen.getByRole('button', { name: 'Discard changes to Model Concurrency Limit' }),
    );
    expect(screen.getByLabelText('Concurrency exemption for gpt-4o-mini')).not.toBeChecked();
    expect(screen.queryByText('1 unsaved change')).toBeNull();
    expect(updateModelConcurrency).not.toHaveBeenCalled();
  });

  it('hides the save bar again once a change is undone by hand', async () => {
    vi.mocked(listModelConcurrency).mockResolvedValue({
      models: [{ model_id: 'gpt-4o-mini', exempt: false }],
    });
    renderWithQueryClient(<ModelConcurrencySection onToast={onToast} />);
    const checkbox = await screen.findByLabelText('Concurrency exemption for gpt-4o-mini');
    fireEvent.click(checkbox);
    expect(
      screen.getByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    ).toBeEnabled();
    fireEvent.click(checkbox);
    expect(
      screen.queryByRole('button', { name: 'Save changes to Model Concurrency Limit' }),
    ).toBeNull();
    expect(updateModelConcurrency).not.toHaveBeenCalled();
  });
});
