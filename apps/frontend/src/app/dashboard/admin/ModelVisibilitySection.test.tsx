// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelVisibilitySection } from './ModelVisibilitySection';

vi.mock('@/lib/api/admin', () => ({
  listModelVisibility: vi.fn(),
  updateModelVisibility: vi.fn(),
}));

import { listModelVisibility, updateModelVisibility } from '@/lib/api/admin';
import type { Role } from '@/lib/api/admin';

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

  const TWO_MODELS = {
    models: [
      {
        model_id: 'gpt-4o-mini',
        baseline_required_role: 'free' as Role,
        override_required_role: null,
        effective_required_role: 'free' as Role,
      },
      {
        model_id: 'claude-3-5-sonnet',
        baseline_required_role: 'pro' as Role,
        override_required_role: 'internal' as Role,
        effective_required_role: 'internal' as Role,
      },
    ],
  };

  function updated(modelId: string, role: Role | null): QueuedUpdate {
    return {
      model_id: modelId,
      baseline_required_role: 'free',
      override_required_role: role,
      effective_required_role: role ?? 'free',
    };
  }

  async function openEditor() {
    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Model Visibility' }));
  }

  function save() {
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Visibility' }));
  }

  function confirmSave() {
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }),
    );
  }

  it('shows overrides read-only until Edit', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    const value = await screen.findByLabelText('Runtime override for gpt-4o-mini');
    expect(value.tagName).toBe('OUTPUT');
    expect(value).toHaveTextContent('Use default');
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Edit Model Visibility' }));
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini')).toHaveValue('__default__');
    expect(screen.getByText('No changes yet')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save changes to Model Visibility' })).toBeDisabled();
  });

  it('keeps changes as drafts: Cancel writes nothing and restores the saved values', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    await openEditor();
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'internal' },
    });
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Model Visibility' }));
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini')).toHaveTextContent(
      'Use default',
    );
    expect(updateModelVisibility).not.toHaveBeenCalled();
  });

  it('treats a select changed back to its saved value as no change', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    await openEditor();
    const select = screen.getByLabelText('Runtime override for gpt-4o-mini');
    fireEvent.change(select, { target: { value: 'internal' } });
    fireEvent.change(select, { target: { value: '__default__' } });
    expect(screen.getByRole('button', { name: 'Save changes to Model Visibility' })).toBeDisabled();
  });

  it('lists old -> new, keeps drafts on Back, and saves each changed model once on Confirm', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockImplementation(async (modelId, role) =>
      updated(modelId, role),
    );
    await openEditor();
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'internal' },
    });
    fireEvent.change(screen.getByLabelText('Runtime override for claude-3-5-sonnet'), {
      target: { value: '__default__' },
    });
    save();

    const dialog = screen.getByRole('dialog', { name: 'Save 2 visibility changes?' });
    expect(dialog).toHaveTextContent('gpt-4o-miniUse default → internal');
    expect(dialog).toHaveTextContent('claude-3-5-sonnetinternal → Use default');
    expect(updateModelVisibility).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini')).toHaveValue('internal');
    expect(updateModelVisibility).not.toHaveBeenCalled();

    save();
    confirmSave();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(updateModelVisibility).mock.calls).toEqual([
      ['gpt-4o-mini', 'internal'],
      ['claude-3-5-sonnet', null],
    ]);
    expect(onToast).toHaveBeenCalledWith('Updated visibility for 2 models.');
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini').tagName).toBe('OUTPUT');
  });

  it('blocks a second Confirm and closing while the save is in flight', async () => {
    let finish!: (value: QueuedUpdate) => void;
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await openEditor();
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'internal' },
    });
    save();
    confirmSave();

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Saving…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(updateModelVisibility).toHaveBeenCalledTimes(1);

    finish(updated('gpt-4o-mini', 'internal'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(updateModelVisibility).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog and drafts after a failure, and retries from the dialog', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(updated('gpt-4o-mini', 'internal'));
    await openEditor();
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'internal' },
    });
    save();
    confirmSave();

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'gpt-4o-mini could not be saved: write failed',
    );
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini')).toHaveValue('internal');

    confirmSave();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(updateModelVisibility).toHaveBeenCalledTimes(2);
  });

  it('reports a partial save and retries only the unsaved model', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility)
      .mockResolvedValueOnce(updated('gpt-4o-mini', 'internal'))
      .mockRejectedValueOnce(new Error('write failed'));
    await openEditor();
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'internal' },
    });
    fireEvent.change(screen.getByLabelText('Runtime override for claude-3-5-sonnet'), {
      target: { value: 'admin' },
    });
    save();
    confirmSave();

    expect(
      await screen.findByText(/claude-3-5-sonnet could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    vi.mocked(updateModelVisibility).mockResolvedValueOnce(updated('claude-3-5-sonnet', 'admin'));
    save();
    expect(screen.getByRole('dialog', { name: 'Save 1 visibility change?' })).not.toHaveTextContent(
      'gpt-4o-mini',
    );
    confirmSave();
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(vi.mocked(updateModelVisibility).mock.calls.map(([id]) => id)).toEqual([
      'gpt-4o-mini',
      'claude-3-5-sonnet',
      'claude-3-5-sonnet',
    ]);
  });

  it('invalidates the dashboard model list after a confirmed save', async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockResolvedValue(updated('gpt-4o-mini', 'admin'));

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />, queryClient);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Model Visibility' }));
    fireEvent.change(screen.getByLabelText('Runtime override for gpt-4o-mini'), {
      target: { value: 'admin' },
    });
    save();
    expect(invalidateSpy).not.toHaveBeenCalled();
    confirmSave();

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });
});
