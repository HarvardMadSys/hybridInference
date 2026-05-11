// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelVisibilitySection } from './ModelVisibilitySection';

vi.mock('@/lib/api/admin', () => ({
  listModelVisibility: vi.fn(),
  updateModelVisibility: vi.fn(),
}));

import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import type { Role } from '@/lib/api/admin';

type InternalVisibilityUpdate = {
  model_id: string;
  baseline_required_role: 'free';
  override_required_role: 'internal';
  effective_required_role: 'internal';
};

type QueuedUpdate = {
  model_id: string;
  baseline_required_role: Role;
  override_required_role: Role | null;
  effective_required_role: Role;
};

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

describe('ModelVisibilitySection', () => {
  const onToast = vi.fn();

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and renders returned model visibility rows', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    expect(screen.getAllByText('free').length).toBeGreaterThanOrEqual(2);
  });

  it('PATCHes a role when the admin changes the select', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    });

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(onToast).toHaveBeenCalledWith('Updated visibility for gpt-4o-mini.');
  });

  it('sends null when Use default is selected', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: 'internal',
          effective_required_role: 'internal',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: null,
      effective_required_role: 'free',
    });

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: '__default__' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', null);
    });
  });

  it('shows an inline error when loading fails', async () => {
    vi.mocked(listModelVisibility).mockRejectedValue(new Error('backend unavailable'));

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
  });

  it('retries loading after an inline error', async () => {
    vi.mocked(listModelVisibility)
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce({
        models: [
          {
            model_id: 'gpt-4o-mini',
            baseline_required_role: 'free',
            override_required_role: null,
            effective_required_role: 'free',
          },
        ],
      });

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    expect(await screen.findByText('backend unavailable')).toBeInTheDocument();
    const retryButton = screen.getByRole('button', { name: 'Retry' });
    expect(retryButton).toHaveAttribute('type', 'button');
    fireEvent.click(retryButton);

    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    await waitFor(() => {
      expect(listModelVisibility).toHaveBeenCalledTimes(2);
    });
  });

  it('disables only the saving row while an update is in flight', async () => {
    let resolveUpdate: undefined | ((value: InternalVisibilityUpdate) => void);

    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
        {
          model_id: 'claude-3-5-sonnet',
          baseline_required_role: 'pro',
          override_required_role: null,
          effective_required_role: 'pro',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const firstSelect = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    const secondSelect = screen.getByLabelText('Runtime override for claude-3-5-sonnet');

    fireEvent.change(firstSelect, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(firstSelect).toBeDisabled();
    expect(secondSelect).not.toBeDisabled();

    if (!resolveUpdate) {
      throw new Error('Expected update promise resolver to be assigned');
    }

    resolveUpdate({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    });

    await waitFor(() => {
      expect(firstSelect).not.toBeDisabled();
    });
  });

  it('keeps multiple rows disabled while their own saves are still in flight', async () => {
    const resolvers = new Map<string, (value: QueuedUpdate) => void>();

    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
        {
          model_id: 'claude-3-5-sonnet',
          baseline_required_role: 'pro',
          override_required_role: null,
          effective_required_role: 'pro',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockImplementation(
      (modelId) =>
        new Promise((resolve) => {
          resolvers.set(modelId, resolve);
        }),
    );

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const firstSelect = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    const secondSelect = screen.getByLabelText('Runtime override for claude-3-5-sonnet');

    fireEvent.change(firstSelect, { target: { value: 'internal' } });
    fireEvent.change(secondSelect, { target: { value: 'admin' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
      expect(updateModelVisibility).toHaveBeenCalledWith('claude-3-5-sonnet', 'admin');
    });
    expect(firstSelect).toBeDisabled();
    expect(secondSelect).toBeDisabled();

    const resolveFirst = resolvers.get('gpt-4o-mini');
    if (!resolveFirst) {
      throw new Error('Expected first update promise resolver to be assigned');
    }

    resolveFirst({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'free',
      override_required_role: 'internal',
      effective_required_role: 'internal',
    });

    await waitFor(() => {
      expect(firstSelect).not.toBeDisabled();
    });
    expect(secondSelect).toBeDisabled();
  });

  it('keeps the newly selected value visible while a save is in flight', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockImplementation(
      () =>
        new Promise(() => {
          // Intentionally left pending to assert optimistic UI state.
        }),
    );

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(updateModelVisibility).toHaveBeenCalledWith('gpt-4o-mini', 'internal');
    });
    expect(select).toHaveValue('internal');
  });

  it('shows a failure toast when an update fails', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockRejectedValue(new Error('write failed'));

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });

    await waitFor(() => {
      expect(onToast).toHaveBeenCalledWith('Failed to update gpt-4o-mini: write failed');
    });
  });

  it('invalidates the dashboard model list after a successful update', async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'admin',
          override_required_role: null,
          effective_required_role: 'admin',
        },
      ],
    });
    vi.mocked(updateModelVisibility).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      baseline_required_role: 'admin',
      override_required_role: 'free',
      effective_required_role: 'free',
    });

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />, queryClient);

    const select = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'free' } });

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });
});
