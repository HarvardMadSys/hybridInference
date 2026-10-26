// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchesView, progress } from './BatchesView';
import { WebhookSettings } from './WebhookSettings';
import {
  cancelMyBatch,
  deleteMyBatch,
  getMyWebhook,
  listMyBatches,
  listMyWebhookEvents,
  saveMyWebhook,
  type ConsoleBatch,
  type ConsoleBatchPage,
} from '@/lib/api/batches';

vi.mock('@/lib/api/batches', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/batches')>();
  return {
    ...actual,
    listMyBatches: vi.fn(),
    cancelMyBatch: vi.fn(),
    deleteMyBatch: vi.fn(),
    downloadMyResults: vi.fn(),
    getMyWebhook: vi.fn(),
    saveMyWebhook: vi.fn(),
    deleteMyWebhook: vi.fn(),
    listMyWebhookEvents: vi.fn(),
  };
});
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function batch(id: string, overrides: Partial<ConsoleBatch> = {}): ConsoleBatch {
  return {
    id,
    user_id: 'me',
    api_surface: 'openai_inline',
    target: 'chat',
    endpoint: '/v1/chat/completions',
    status: 'in_progress',
    phase: 'runnable',
    models: ['m'],
    request_counts: { total: 4, completed: 2, failed: 1 },
    cancelled: 0,
    expired: 0,
    usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150 },
    cost_usd: 0.25,
    cost_unknown_attempts: 0,
    created_at: 1_790_000_000,
    expires_at: null,
    ended_at: null,
    results_expires_at: null,
    input_file_id: null,
    output_file_id: null,
    error_file_id: null,
    results_available: false,
    metadata: null,
    error: null,
    validation_error: null,
    protocol_profile: null,
    ...overrides,
  };
}

function page(rows: ConsoleBatch[], hasMore = false): ConsoleBatchPage {
  return { object: 'list', data: rows, has_more: hasMore, last_id: rows.at(-1)?.id ?? null };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getMyWebhook).mockResolvedValue({ configured: false });
  vi.mocked(listMyWebhookEvents).mockResolvedValue({ data: [] });
});
afterEach(cleanup);

describe('BatchesView', () => {
  it('shows progress, tokens, known cost and unknown-cost attempts', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([batch('batch_a', { cost_unknown_attempts: 2 })]),
    );
    render(<BatchesView />);
    expect(await screen.findByText('batch_a')).toBeInTheDocument();
    expect(screen.getByText('3 of 4 done · 1 failed')).toBeInTheDocument();
    expect(screen.getByText('100 in · 50 out')).toBeInTheDocument();
    expect(screen.getByText(/\$0\.2500/)).toBeInTheDocument();
    expect(screen.getByText('+ 2 unknown')).toBeInTheDocument();
  });

  it('counts an Anthropic batch done from its tallies', () => {
    const row = batch('msgbatch_1', {
      api_surface: 'anthropic',
      request_counts: { processing: 1, succeeded: 2, errored: 1, canceled: 0, expired: 1 },
    });
    expect(progress(row)).toEqual({ done: 4, total: 5, failed: 2 });
  });

  it('requests the next page once while it loads, and lists each batch once', async () => {
    const next = deferred<ConsoleBatchPage>();
    vi.mocked(listMyBatches).mockImplementation((after) =>
      after ? next.promise : Promise.resolve(page([batch('batch_1')], true)),
    );
    render(<BatchesView />);
    const more = await screen.findByRole('button', { name: 'Show more' });
    fireEvent.click(more);
    fireEvent.click(more);
    expect(vi.mocked(listMyBatches).mock.calls).toHaveLength(2);
    await act(async () => {
      // The next page repeats a row the list already shows.
      next.resolve(page([batch('batch_1'), batch('batch_2')]));
      await next.promise;
    });
    expect(screen.getAllByText('batch_1')).toHaveLength(1);
    expect(screen.getAllByText('batch_2')).toHaveLength(1);
  });

  it('cancels and deletes after a confirmation, and not without one', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(page([batch('batch_c')]));
    vi.mocked(cancelMyBatch).mockResolvedValue(
      batch('batch_c', { status: 'cancelling', phase: 'runnable' }),
    );
    vi.mocked(deleteMyBatch).mockResolvedValue({ id: 'batch_c', deleted: true });
    const confirm = vi.spyOn(window, 'confirm');
    render(<BatchesView />);
    await screen.findByText('batch_c');

    confirm.mockReturnValueOnce(false);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(cancelMyBatch).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(await screen.findByText('Cancelling')).toBeInTheDocument();
    expect(cancelMyBatch).toHaveBeenCalledWith('batch_c');

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(screen.queryByText('batch_c')).toBeNull());
    confirm.mockRestore();
  });

  it('offers downloads only for finished results, by file kind', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_file', {
          api_surface: 'openai_file',
          status: 'completed',
          phase: 'sealed',
          results_available: true,
          output_file_id: 'file-out',
          error_file_id: 'file-err',
        }),
      ]),
    );
    render(<BatchesView />);
    expect(await screen.findByRole('button', { name: 'Download outputs' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download errors' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download results' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
  });
});

describe('WebhookSettings', () => {
  it('keeps the form read-only until the stored settings have loaded', async () => {
    const initial = deferred<Awaited<ReturnType<typeof getMyWebhook>>>();
    vi.mocked(getMyWebhook).mockReturnValue(initial.promise);
    vi.mocked(listMyWebhookEvents).mockResolvedValue({ data: [] });
    vi.mocked(saveMyWebhook).mockResolvedValue({
      configured: true,
      url: 'https://new.example.com/h',
      enabled: true,
      secret: 'whsec_new',
    });
    render(<WebhookSettings />);
    const field = screen.getByLabelText('Endpoint URL (https)');
    expect(field).toBeDisabled();
    fireEvent.submit(field.closest('form') as HTMLFormElement);
    expect(saveMyWebhook).not.toHaveBeenCalled();

    await act(async () => {
      initial.resolve({ configured: true, url: 'https://old.example.com/h', enabled: true });
      await initial.promise;
    });
    expect(field).not.toBeDisabled();
    expect(field).toHaveValue('https://old.example.com/h');

    fireEvent.change(field, { target: { value: 'https://new.example.com/h' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('whsec_new')).toBeInTheDocument();
    expect(field).toHaveValue('https://new.example.com/h');
    expect(saveMyWebhook).toHaveBeenCalledWith({
      url: 'https://new.example.com/h',
      enabled: true,
      rotate_secret: false,
    });
  });
});
