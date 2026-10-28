// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchesView, progress } from './BatchesView';
import { WebhookSettings } from './WebhookSettings';
import type { BatchAccess } from '@/lib/hooks/queries/useBatchCapabilities';
import {
  cancelMyBatch,
  deleteMyBatch,
  deleteMyWebhook,
  downloadMyResults,
  getMyWebhook,
  listMyBatches,
  listMyWebhookEvents,
  saveMyWebhook,
  type BatchCapabilities,
  type ConsoleBatch,
  type ConsoleBatchPage,
} from '@/lib/api/batches';

function caps(overrides: Partial<BatchCapabilities> = {}): BatchCapabilities {
  return {
    user_id: 'me',
    status: 'enabled',
    enabled: true,
    min_role: 'internal',
    role_may_submit: true,
    has_history: true,
    history_error: false,
    show_entry: true,
    ...overrides,
  };
}

let access: BatchAccess = { capabilities: caps(), loading: false, error: null };
vi.mock('@/lib/hooks/queries/useBatchCapabilities', () => ({
  useBatchCapabilities: () => access,
}));

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
    input: 'inline',
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
  access = { capabilities: caps(), loading: false, error: null };
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
    expect(
      screen.getByText(
        '2 succeeded · 1 failed · 0 expired unrun · 0 cancelled · 1 not finished · 4 total',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('100 in · 50 out')).toBeInTheDocument();
    expect(screen.getByText(/\$0\.2500/)).toBeInTheDocument();
    expect(screen.getByText('+ 2 unknown')).toBeInTheDocument();
  });

  it('shows every batch as one product: target and model first, input method second', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_m', {
          target: 'messages',
          endpoint: '/v1/messages',
          models: ['claude-x'],
          protocol_profile: '2023-06-01',
        }),
        batch('batch_f', {
          api_surface: 'openai_file',
          input: 'file',
          target: 'embeddings',
          endpoint: '/v1/embeddings',
          models: ['embed-1'],
        }),
      ]),
    );
    render(<BatchesView />);
    const messages = await screen.findByTestId('batch-batch_m');
    expect(messages).toHaveTextContent('Messages · claude-x');
    expect(messages).toHaveTextContent('Direct requests');
    const file = screen.getByTestId('batch-batch_f');
    expect(file).toHaveTextContent('Embeddings · embed-1');
    expect(file).toHaveTextContent('JSONL file');
    // No vendor-named products, no separate API surfaces.
    for (const text of [/OpenAI batch/, /Anthropic/, /Message Batch/, /\/v1\/messages\/batches/]) {
      expect(screen.queryByText(text)).toBeNull();
    }
  });

  it('shows a legacy Message Batch with the common status words and counts', async () => {
    const legacy = batch('msgbatch_1', {
      api_surface: 'anthropic',
      target: 'messages',
      endpoint: '/v1/messages',
      status: 'completed',
      phase: 'sealed',
      request_counts: { total: 5, completed: 3, failed: 1 },
      cancelled: 1,
      results_available: true,
    });
    expect(progress(legacy)).toEqual({ done: 5, total: 5, failed: 1 });
    vi.mocked(listMyBatches).mockResolvedValue(page([legacy]));
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-msgbatch_1');
    expect(row).toHaveTextContent('Completed');
    expect(row).not.toHaveTextContent('Ended');
    expect(row).toHaveTextContent(
      '3 succeeded · 1 failed · 0 expired unrun · 1 cancelled · 0 not finished · 5 total',
    );
    expect(screen.getByRole('button', { name: 'Download results' })).toBeInTheDocument();
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
          input: 'file',
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

describe('BatchesView capabilities', () => {
  it.each(['disabled', 'unavailable'] as const)(
    'when Batch is %s, explains so and loads no batch data',
    async (status) => {
      access = {
        capabilities: caps({ status, enabled: status !== 'disabled', show_entry: false }),
        loading: false,
        error: null,
      };
      render(<BatchesView />);

      expect(screen.getByTestId('batches-unavailable')).toHaveTextContent('Batch is not available');
      expect(
        screen.getByText(status === 'disabled' ? /turned off on this gateway/ : /temporarily/),
      ).toBeInTheDocument();
      expect(listMyBatches).not.toHaveBeenCalled();
      expect(getMyWebhook).not.toHaveBeenCalled();
      expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
    },
  );

  it('loads nothing until the capabilities are known', () => {
    access = { capabilities: null, loading: true, error: null };
    render(<BatchesView />);

    expect(screen.getByText('Loading batches…')).toBeInTheDocument();
    expect(listMyBatches).not.toHaveBeenCalled();
  });

  it('below the submission role, says so and keeps every owner action', async () => {
    access = {
      capabilities: caps({ role_may_submit: false, min_role: 'pro' }),
      loading: false,
      error: null,
    };
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_run'),
        batch('batch_done', { status: 'completed', phase: 'sealed', results_available: true }),
      ]),
    );
    vi.mocked(downloadMyResults).mockResolvedValue();
    render(<BatchesView />);

    expect(await screen.findByText('batch_run')).toBeInTheDocument();
    expect(screen.getByText(/cannot submit new batches: that needs the pro plan/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Download results' }));
    expect(downloadMyResults).toHaveBeenCalledWith('batch_done', 'all');
    expect(screen.queryByRole('button', { name: /create|new batch|submit/i })).toBeNull();
  });

  it('below the submission role with no batches, does not invite a submission', async () => {
    access = {
      capabilities: caps({ role_may_submit: false, has_history: false, show_entry: false }),
      loading: false,
      error: null,
    };
    vi.mocked(listMyBatches).mockResolvedValue(page([]));
    render(<BatchesView />);

    expect(await screen.findByText('You have no batches.')).toBeInTheDocument();
    expect(screen.getByText('Your plan cannot set up a webhook.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Endpoint URL (https)')).toBeNull();
  });

  it('when the capabilities cannot be loaded, still lists the batches and says why', async () => {
    access = { capabilities: null, loading: false, error: 'Gateway timeout' };
    vi.mocked(listMyBatches).mockResolvedValue(page([batch('batch_x')]));
    render(<BatchesView />);

    expect(await screen.findByText('batch_x')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Gateway timeout');
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });
});

describe('WebhookSettings', () => {
  it('below the submission role, shows the webhook read-only and lets it be removed', async () => {
    vi.mocked(getMyWebhook).mockResolvedValue({
      configured: true,
      url: 'https://old.example.com/h',
      enabled: true,
    });
    vi.mocked(deleteMyWebhook).mockResolvedValue({ deleted: true });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<WebhookSettings canEdit={false} />);

    const field = await screen.findByDisplayValue('https://old.example.com/h');
    expect(field).toBeDisabled();
    expect(screen.getByText(/cannot change webhook settings/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Rotate secret' })).toBeNull();
    fireEvent.submit(field.closest('form') as HTMLFormElement);
    expect(saveMyWebhook).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(deleteMyWebhook).toHaveBeenCalled());
    confirm.mockRestore();
  });

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

  it('filters by name or id and by the status shown, and links to creating a batch', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(page([batch('batch_a', { name: 'eval-0420' })]));
    render(<BatchesView />);
    expect(await screen.findByText('eval-0420')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Create batch' })).toHaveAttribute(
      'href',
      '/dashboard/batches/new',
    );
    fireEvent.change(screen.getByLabelText('Search by name or batch ID'), {
      target: { value: 'eval' },
    });
    await waitFor(() =>
      expect(listMyBatches).toHaveBeenLastCalledWith(null, 20, { status: '', q: 'eval' }),
    );
  });

  it('offers no create button to a role that cannot submit', async () => {
    access = { capabilities: caps({ role_may_submit: false }), loading: false, error: null };
    vi.mocked(listMyBatches).mockResolvedValue(page([]));
    render(<BatchesView />);
    expect(await screen.findByText('You have no batches.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Create batch' })).not.toBeInTheDocument();
  });
});
