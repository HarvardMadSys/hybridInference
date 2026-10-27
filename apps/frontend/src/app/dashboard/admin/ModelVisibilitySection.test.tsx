// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { chooseOption } from '@/test-utils/select';

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

  async function renderSection() {
    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);
    await screen.findByRole('combobox', { name: 'Runtime override for gpt-4o-mini' });
  }

  const saveButton = () =>
    screen.queryByRole('button', { name: 'Save changes to Model Visibility' });

  function save() {
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Model Visibility' }));
  }

  it('edits overrides in place, with no Edit step and no save bar until a change', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />);

    expect(
      await screen.findByRole('combobox', { name: 'Runtime override for gpt-4o-mini' }),
    ).toHaveTextContent('Use default');
    expect(
      screen.getByRole('combobox', { name: 'Runtime override for claude-3-5-sonnet' }),
    ).toHaveTextContent('internal');
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(saveButton()).toBeNull();

    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(saveButton()).toBeEnabled();
    expect(updateModelVisibility).not.toHaveBeenCalled();
  });

  it('keeps changes as drafts: Discard writes nothing and restores the saved values', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Model Visibility' }));
    expect(screen.getByLabelText('Runtime override for gpt-4o-mini')).toHaveTextContent(
      'Use default',
    );
    expect(saveButton()).toBeNull();
    expect(updateModelVisibility).not.toHaveBeenCalled();
  });

  it('hides the save bar again once a select is changed back to its saved value', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    expect(saveButton()).toBeEnabled();
    chooseOption('Runtime override for gpt-4o-mini', 'Use default');
    expect(saveButton()).toBeNull();
  });

  it('saves each changed model once on Save, with no confirmation', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockImplementation(async (modelId, role) =>
      updated(modelId, role),
    );
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    chooseOption('Runtime override for claude-3-5-sonnet', 'Use default');
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    expect(updateModelVisibility).not.toHaveBeenCalled();

    save();
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(vi.mocked(updateModelVisibility).mock.calls).toEqual([
      ['gpt-4o-mini', 'internal'],
      ['claude-3-5-sonnet', null],
    ]);
    expect(onToast).toHaveBeenCalledWith('Updated visibility for 2 models.');
    // The saved values stay in place, and nothing is left to save.
    expect(
      screen.getByRole('combobox', { name: 'Runtime override for gpt-4o-mini' }),
    ).toHaveTextContent('internal');
    expect(saveButton()).toBeNull();
  });

  it('holds a second Save, Discard and the selects while the save is in flight', async () => {
    let finish!: (value: QueuedUpdate) => void;
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    save();

    const saving = screen.getByRole('button', { name: 'Save changes to Model Visibility' });
    expect(saving).toHaveTextContent('Saving…');
    expect(saving).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to Model Visibility' }),
    ).toBeDisabled();
    expect(
      screen.getByRole('combobox', { name: 'Runtime override for claude-3-5-sonnet' }),
    ).toBeDisabled();
    fireEvent.click(saving);
    expect(updateModelVisibility).toHaveBeenCalledTimes(1);

    finish(updated('gpt-4o-mini', 'internal'));
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(updateModelVisibility).toHaveBeenCalledTimes(1);
  });

  it('keeps the drafts and shows the error after a failure, and saves them again', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(updated('gpt-4o-mini', 'internal'));
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    save();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'gpt-4o-mini could not be saved: write failed',
    );
    expect(
      screen.getByRole('combobox', { name: 'Runtime override for gpt-4o-mini' }),
    ).toHaveTextContent('internal');
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    save();
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(updateModelVisibility).toHaveBeenCalledTimes(2);
  });

  it('reports a partial save and retries only the unsaved model', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility)
      .mockResolvedValueOnce(updated('gpt-4o-mini', 'internal'))
      .mockRejectedValueOnce(new Error('write failed'));
    await renderSection();
    chooseOption('Runtime override for gpt-4o-mini', 'internal');
    chooseOption('Runtime override for claude-3-5-sonnet', 'admin');
    save();

    expect(
      await screen.findByText(/claude-3-5-sonnet could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    vi.mocked(updateModelVisibility).mockResolvedValueOnce(updated('claude-3-5-sonnet', 'admin'));
    save();
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(vi.mocked(updateModelVisibility).mock.calls.map(([id]) => id)).toEqual([
      'gpt-4o-mini',
      'claude-3-5-sonnet',
      'claude-3-5-sonnet',
    ]);
  });

  it('invalidates the dashboard model list after a save', async () => {
    const queryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockResolvedValue(updated('gpt-4o-mini', 'admin'));

    renderWithQueryClient(<ModelVisibilitySection onToast={onToast} />, queryClient);
    await screen.findByRole('combobox', { name: 'Runtime override for gpt-4o-mini' });
    chooseOption('Runtime override for gpt-4o-mini', 'admin');
    expect(invalidateSpy).not.toHaveBeenCalled();
    save();

    await waitFor(() => {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['user', 'models'] });
    });
  });
  it('writes nothing when the list is only opened, browsed and dismissed', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    await renderSection();
    const select = screen.getByRole('combobox', { name: 'Runtime override for gpt-4o-mini' });
    fireEvent.click(select);
    fireEvent.keyDown(select, { key: 'ArrowDown' });
    fireEvent.keyDown(select, { key: 'ArrowDown' });
    fireEvent.keyDown(select, { key: 'Escape' });

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(select).toHaveTextContent('Use default');
    expect(saveButton()).toBeNull();
    expect(updateModelVisibility).not.toHaveBeenCalled();
  });

  it('treats reselecting the saved role as no change, and writes a different one once on Save', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue(TWO_MODELS);
    vi.mocked(updateModelVisibility).mockResolvedValue(updated('claude-3-5-sonnet', 'admin'));
    await renderSection();
    // claude-3-5-sonnet is saved as internal: choosing it again changes nothing.
    chooseOption('Runtime override for claude-3-5-sonnet', 'internal');
    expect(saveButton()).toBeNull();

    chooseOption('Runtime override for claude-3-5-sonnet', 'admin');
    expect(updateModelVisibility).not.toHaveBeenCalled();
    save();
    await waitFor(() => expect(updateModelVisibility).toHaveBeenCalledTimes(1));
    expect(updateModelVisibility).toHaveBeenCalledWith('claude-3-5-sonnet', 'admin');
  });
});
