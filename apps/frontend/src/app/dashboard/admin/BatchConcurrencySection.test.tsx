// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminBatchModelLimit } from '@/lib/api/admin';

import { BatchConcurrencySection } from './BatchConcurrencySection';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    MAX_BATCH_MODEL_CONCURRENCY: actual.MAX_BATCH_MODEL_CONCURRENCY,
    listBatchModelLimits: vi.fn(),
    updateBatchModelLimit: vi.fn(),
  };
});

import { listBatchModelLimits, updateBatchModelLimit } from '@/lib/api/admin';

function row(
  overrides: Partial<AdminBatchModelLimit> & { model_id: string },
): AdminBatchModelLimit {
  return {
    max_concurrency: null,
    effective_concurrency: 4,
    running: 0,
    queued: 0,
    ...overrides,
  };
}

function listing(...models: AdminBatchModelLimit[]) {
  return { default_concurrency: 4, models };
}

const capOf = (modelId: string) =>
  screen.getByLabelText(`Max concurrent batch requests for ${modelId}`);

/** Wait for the caps to load; they are edited in place. */
async function loaded() {
  await screen.findByText('glm-4.7');
}

const saveButton = () =>
  screen.queryByRole('button', { name: 'Save changes to Batch Concurrency' });

function clickSave() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to Batch Concurrency' }));
}

describe('BatchConcurrencySection', () => {
  const onToast = vi.fn();

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows each cap, its load, and the default, editable in place', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(
        row({ model_id: 'glm-4.7', running: 3, queued: 120 }),
        row({ model_id: 'qwen3.6-35b', max_concurrency: 8, effective_concurrency: 8 }),
        row({ model_id: 'deepseek-v4', max_concurrency: 0, effective_concurrency: 0, queued: 7 }),
      ),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(await screen.findByText('glm-4.7')).toBeInTheDocument();
    // An empty input is the default, named by its placeholder.
    expect(capOf('glm-4.7')).toHaveValue(null);
    expect(capOf('glm-4.7')).toHaveAttribute('placeholder', 'Default (4)');
    expect(capOf('qwen3.6-35b')).toHaveValue(8);
    expect(capOf('deepseek-v4')).toHaveValue(0);
    const glm = screen.getByText('glm-4.7').closest('tr') as HTMLElement;
    expect(within(glm).getByText('3')).toBeInTheDocument();
    expect(within(glm).getByText('120')).toBeInTheDocument();
    // No Edit step, and no save bar until something changes.
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(saveButton()).toBeNull();
  });

  it('sets a cap on Save, with no confirmation', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit).mockResolvedValue(
      row({ model_id: 'glm-4.7', max_concurrency: 6, effective_concurrency: 6, running: 2 }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '6' } });
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
    clickSave();
    expect(screen.queryByRole('dialog')).toBeNull();

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith('Updated batch concurrency for glm-4.7.'),
    );
    expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 6);
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(1);
    // Nothing left to save, and the row shows what the backend returned.
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(capOf('glm-4.7')).toHaveValue(6);
    const glm = screen.getByText('glm-4.7').closest('tr') as HTMLElement;
    expect(within(glm).getByText('2')).toBeInTheDocument();
  });

  it('clears a cap when the input is emptied, so the default applies', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 })),
    );
    vi.mocked(updateBatchModelLimit).mockResolvedValue(row({ model_id: 'glm-4.7' }));

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '' } });
    clickSave();

    await waitFor(() => expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', null));
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(capOf('glm-4.7')).toHaveValue(null);
  });

  it('pauses a model with a cap of 0', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit).mockResolvedValue(
      row({ model_id: 'glm-4.7', max_concurrency: 0, effective_concurrency: 0 }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '0' } });
    clickSave();

    await waitFor(() => expect(saveButton()).toBeNull());
    expect(capOf('glm-4.7')).toHaveValue(0);
    expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 0);
  });

  it.each([
    ['-1', 'Must be ≥ 0.'],
    ['2.5', 'Must be a whole number.'],
    ['10001', 'Must be ≤ 10000.'],
  ])('refuses %s as a cap', async (value, message) => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(message);
    expect(capOf('glm-4.7')).toHaveAttribute('aria-invalid', 'true');
    // The bar shows for the invalid draft, with Save held back and the reason.
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText('Correct the cap for glm-4.7 before saving.')).toBeVisible();
    fireEvent.keyDown(capOf('glm-4.7'), { key: 'Enter' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });

  it('will not save valid changes while another cap is invalid', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7' }), row({ model_id: 'qwen3.6-35b' })),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '3' } });
    fireEvent.change(capOf('qwen3.6-35b'), { target: { value: '-2' } });
    expect(saveButton()).toBeDisabled();
    clickSave();
    fireEvent.keyDown(capOf('glm-4.7'), { key: 'Enter' });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(
      await screen.findByText('Correct the cap for qwen3.6-35b before saving.'),
    ).toBeInTheDocument();
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });

  it('saves several changes one request at a time', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(
        row({ model_id: 'glm-4.7' }),
        row({ model_id: 'qwen3.6-35b', max_concurrency: 8, effective_concurrency: 8 }),
      ),
    );
    vi.mocked(updateBatchModelLimit).mockImplementation(async (modelId, cap) =>
      row({ model_id: modelId, max_concurrency: cap, effective_concurrency: cap ?? 4 }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '1' } });
    fireEvent.change(capOf('qwen3.6-35b'), { target: { value: '' } });
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    clickSave();

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith('Updated batch concurrency for 2 models.'),
    );
    expect(vi.mocked(updateBatchModelLimit).mock.calls).toEqual([
      ['glm-4.7', 1],
      ['qwen3.6-35b', null],
    ]);
  });

  it('shows an inline error when loading fails, and retries', async () => {
    vi.mocked(listBatchModelLimits)
      .mockRejectedValueOnce(new Error('Database not configured'))
      .mockResolvedValueOnce(listing(row({ model_id: 'glm-4.7' })));

    render(<BatchConcurrencySection onToast={onToast} />);

    expect(await screen.findByText('Database not configured')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('glm-4.7')).toBeInTheDocument();
    expect(listBatchModelLimits).toHaveBeenCalledTimes(2);
  });

  it('keeps the draft and shows the error when an update fails, and saves it again', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(
        row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 }),
      );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '2' } });
    clickSave();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'glm-4.7 could not be saved: write failed',
    );
    expect(capOf('glm-4.7')).toHaveValue(2);
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    clickSave();
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(2);
    expect(onToast).toHaveBeenCalledWith('Updated batch concurrency for glm-4.7.');
  });

  it('saves on Enter in a cap, once, and holds the caps while it saves', async () => {
    let finish!: (value: AdminBatchModelLimit) => void;
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7' }), row({ model_id: 'qwen3.6-35b' })),
    );
    vi.mocked(updateBatchModelLimit).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '3' } });
    fireEvent.keyDown(capOf('glm-4.7'), { key: 'Enter' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 3);

    expect(saveButton()).toHaveTextContent('Saving…');
    expect(saveButton()).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to Batch Concurrency' }),
    ).toBeDisabled();
    expect(capOf('qwen3.6-35b')).toBeDisabled();
    fireEvent.keyDown(capOf('glm-4.7'), { key: 'Enter' });
    clickSave();
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(1);

    finish(row({ model_id: 'glm-4.7', max_concurrency: 3, effective_concurrency: 3 }));
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(1);
  });

  it('discards drafts on Discard without writing', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 })),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '5' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Batch Concurrency' }));

    expect(capOf('glm-4.7')).toHaveValue(2);
    expect(saveButton()).toBeNull();
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });

  it('hides the save bar again once a cap is typed back to its saved value', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 })),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await loaded();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '5' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '2' } });
    expect(saveButton()).toBeNull();
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });
});
