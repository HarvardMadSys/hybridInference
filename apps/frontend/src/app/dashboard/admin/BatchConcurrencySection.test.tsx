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

async function editSection() {
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Batch Concurrency' }));
}

function clickSave() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to Batch Concurrency' }));
}

function saveAndConfirm() {
  clickSave();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }));
}

describe('BatchConcurrencySection', () => {
  const onToast = vi.fn();

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows each cap, its load, and the default read-only until Edit', async () => {
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
    expect(capOf('glm-4.7')).toHaveTextContent('Default (4)');
    expect(capOf('qwen3.6-35b')).toHaveTextContent('8');
    expect(capOf('deepseek-v4')).toHaveTextContent('0 (paused)');
    const glm = screen.getByText('glm-4.7').closest('tr') as HTMLElement;
    expect(within(glm).getByText('3')).toBeInTheDocument();
    expect(within(glm).getByText('120')).toBeInTheDocument();
    expect(screen.queryByRole('spinbutton')).toBeNull();

    await editSection();
    expect(capOf('glm-4.7')).toHaveValue(null);
    expect(capOf('glm-4.7')).toHaveAttribute('placeholder', 'Default (4)');
    expect(capOf('qwen3.6-35b')).toHaveValue(8);
  });

  it('sets a cap after the administrator confirms the change', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit).mockResolvedValue(
      row({ model_id: 'glm-4.7', max_concurrency: 6, effective_concurrency: 6, running: 2 }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '6' } });
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
    clickSave();
    const dialog = screen.getByRole('dialog', { name: 'Save 1 batch concurrency change?' });
    expect(dialog).toHaveTextContent('glm-4.7Default (4) → 6');
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith('Updated batch concurrency for glm-4.7.'),
    );
    expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 6);
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(1);
    // Read-only again, showing what the backend returned.
    expect(capOf('glm-4.7')).toHaveTextContent('6');
    const glm = screen.getByText('glm-4.7').closest('tr') as HTMLElement;
    expect(within(glm).getByText('2')).toBeInTheDocument();
  });

  it('clears a cap when the input is emptied, so the default applies', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 })),
    );
    vi.mocked(updateBatchModelLimit).mockResolvedValue(row({ model_id: 'glm-4.7' }));

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '' } });
    clickSave();
    expect(screen.getByRole('dialog')).toHaveTextContent('glm-4.72 → Default (4)');
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }),
    );

    await waitFor(() => expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', null));
  });

  it('pauses a model with a cap of 0', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit).mockResolvedValue(
      row({ model_id: 'glm-4.7', max_concurrency: 0, effective_concurrency: 0 }),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '0' } });
    clickSave();
    expect(screen.getByRole('dialog')).toHaveTextContent('glm-4.7Default (4) → 0 (paused)');
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }),
    );

    await waitFor(() => expect(capOf('glm-4.7')).toHaveTextContent('0 (paused)'));
    expect(updateBatchModelLimit).toHaveBeenCalledWith('glm-4.7', 0);
  });

  it.each([
    ['-1', 'Must be ≥ 0.'],
    ['2.5', 'Must be a whole number.'],
    ['10001', 'Must be ≤ 10000.'],
  ])('refuses %s as a cap', async (value, message) => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(message);
    expect(capOf('glm-4.7')).toHaveAttribute('aria-invalid', 'true');
    expect(
      screen.getByRole('button', { name: 'Save changes to Batch Concurrency' }),
    ).toBeDisabled();
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });

  it('will not save valid changes while another cap is invalid', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7' }), row({ model_id: 'qwen3.6-35b' })),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '3' } });
    fireEvent.change(capOf('qwen3.6-35b'), { target: { value: '-2' } });
    clickSave();

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

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '1' } });
    fireEvent.change(capOf('qwen3.6-35b'), { target: { value: '' } });
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    saveAndConfirm();

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

  it('keeps the draft and the dialog when an update fails, and retries from it', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(listing(row({ model_id: 'glm-4.7' })));
    vi.mocked(updateBatchModelLimit)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(
        row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 }),
      );

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '2' } });
    saveAndConfirm();

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'glm-4.7 could not be saved: write failed',
    );
    expect(capOf('glm-4.7')).toHaveValue(2);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm save' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(updateBatchModelLimit).toHaveBeenCalledTimes(2);
    expect(onToast).toHaveBeenCalledWith('Updated batch concurrency for glm-4.7.');
  });

  it('discards drafts on Cancel without writing', async () => {
    vi.mocked(listBatchModelLimits).mockResolvedValue(
      listing(row({ model_id: 'glm-4.7', max_concurrency: 2, effective_concurrency: 2 })),
    );

    render(<BatchConcurrencySection onToast={onToast} />);

    await editSection();
    fireEvent.change(capOf('glm-4.7'), { target: { value: '5' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Batch Concurrency' }));

    expect(capOf('glm-4.7')).toHaveTextContent('2');
    expect(updateBatchModelLimit).not.toHaveBeenCalled();
  });
});
