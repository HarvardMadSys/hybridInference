// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchesView, progress } from './BatchesView';
import { WebhookSettings } from './WebhookSettings';
import { AccountToastContext } from '@/lib/hooks/useAccountToast';
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
  BatchCategoryFilterUnavailable,
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
vi.mock('@/lib/hooks/useAccountToast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/hooks/useAccountToast')>()),
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
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
    category: 'processing',
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
  it('shows useful progress and unknown costs without routine technical metadata', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([batch('batch_a', { cost_unknown_attempts: 2 })]),
    );
    render(<BatchesView />);
    expect(await screen.findByText('batch_a')).toBeInTheDocument();
    expect(screen.getByText('2 / 4 succeeded · 1 failed · 1 unfinished')).toBeInTheDocument();
    expect(screen.queryByText('100 in · 50 out')).toBeNull();
    expect(screen.queryByText(/\$0\.2500/)).toBeNull();
    expect(screen.getByText('Cost is unknown for 2 attempts')).toBeInTheDocument();
  });

  it('does not repeat the opaque id for a named task, but keeps a detail link', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([batch('batch_long_id', { name: 'Eval run' })]),
    );
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-batch_long_id');
    expect(row).not.toHaveTextContent('batch_long_id');
    expect(within(row).getByRole('link', { name: 'Eval run' })).toHaveAttribute(
      'href',
      '/dashboard/batches/batch_long_id',
    );
    expect(within(row).getByRole('link', { name: 'Details' })).toBeInTheDocument();
  });

  it('does not imply zero requests during file validation', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_check', {
          input: 'file',
          status: 'validating',
          phase: 'validating',
          request_counts: { total: 0, completed: 0, failed: 0 },
        }),
      ]),
    );
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-batch_check');
    expect(row).toHaveTextContent('Checking the data');
    expect(row).not.toHaveTextContent('0 / 0 succeeded');
  });

  it('offers the error file first when all requests failed, and retains output in More', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_failed', {
          input: 'file',
          status: 'completed',
          category: 'issues',
          phase: 'sealed',
          request_counts: { total: 2, completed: 0, failed: 2 },
          results_available: true,
          output_file_id: 'file-empty',
          error_file_id: 'file-errors',
        }),
      ]),
    );
    vi.mocked(downloadMyResults).mockResolvedValue();
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-batch_failed');
    fireEvent.click(within(row).getByRole('button', { name: 'Download errors' }));
    expect(downloadMyResults).toHaveBeenCalledWith('batch_failed', 'error');
    const more = within(row).getByLabelText('More actions for batch_failed');
    fireEvent.click(more);
    expect(within(row).getByRole('button', { name: 'Download outputs' })).toBeInTheDocument();
    fireEvent.keyDown(more, { key: 'Escape' });
    expect(more.closest('details')).not.toHaveAttribute('open');
    expect(more).toHaveFocus();
  });

  it('shows target and model without repeating input provenance in the list', async () => {
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
    expect(messages).not.toHaveTextContent('Direct requests');
    const file = screen.getByTestId('batch-batch_f');
    expect(file).toHaveTextContent('Embeddings · embed-1');
    expect(file).not.toHaveTextContent('JSONL file');
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
      category: 'issues',
      phase: 'sealed',
      request_counts: { total: 5, completed: 3, failed: 1 },
      cancelled: 1,
      results_available: true,
    });
    expect(progress(legacy)).toEqual({ done: 5, total: 5, failed: 1 });
    vi.mocked(listMyBatches).mockResolvedValue(page([legacy]));
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-msgbatch_1');
    expect(row).toHaveTextContent('With issues');
    expect(row).toHaveTextContent('Some requests failed');
    expect(row).not.toHaveTextContent('Ended');
    expect(row).toHaveTextContent('3 / 5 succeeded · 1 failed · 1 cancelled');
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
    expect(
      await screen.findByText('Cancelling: requests already running will finish'),
    ).toBeInTheDocument();
    expect(cancelMyBatch).toHaveBeenCalledWith('batch_c');

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByLabelText('More actions for batch_c'));
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
    fireEvent.click(screen.getByLabelText('More actions for batch_file'));
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
    fireEvent.click(screen.getByLabelText('More actions for batch_run'));
    fireEvent.click(screen.getByLabelText('More actions for batch_done'));
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
    fireEvent.click(screen.getByText('Webhook'));
    expect(screen.getByText('Your plan cannot set up a webhook.')).toBeVisible();
    expect(screen.queryByLabelText('Endpoint URL (https)')).toBeNull();
  });

  it('when the capabilities cannot be loaded, still lists the batches and says why', async () => {
    access = { capabilities: null, loading: false, error: 'Gateway timeout' };
    vi.mocked(listMyBatches).mockResolvedValue(page([batch('batch_x')]));
    render(<BatchesView />);

    expect(await screen.findByText('batch_x')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Gateway timeout');
    fireEvent.click(screen.getByLabelText('More actions for batch_x'));
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });
});

describe('WebhookSettings', () => {
  it('keeps the webhook entry compact but exposes configuration and delivery failures', async () => {
    vi.mocked(getMyWebhook).mockResolvedValue({
      configured: true,
      url: 'https://example.com/h',
      enabled: true,
    });
    vi.mocked(listMyWebhookEvents).mockResolvedValue({
      data: [
        {
          id: 'event-1',
          type: 'batch.completed',
          batch_id: 'batch_x',
          status: 'failed',
          attempts: 1,
          last_status_code: null,
          last_error: null,
          created_at: null,
          delivered_at: null,
          next_attempt_at: null,
        },
      ],
    });
    render(<WebhookSettings />);
    expect(await screen.findByText('Configured')).toBeVisible();
    expect(await screen.findByText('Recent delivery failures')).toBeVisible();
    const form = screen.getByLabelText('Endpoint URL (https)');
    expect(form).not.toBeVisible();
    fireEvent.click(screen.getByText('Webhook'));
    expect(form).toBeVisible();
  });

  it('does not hide a webhook loading error when the settings are collapsed', async () => {
    vi.mocked(getMyWebhook).mockRejectedValue(new Error('Webhook unavailable'));
    render(<WebhookSettings />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Webhook unavailable');
    expect(screen.getByText('Could not load')).toBeVisible();
  });

  it.each(['create', 'rotate'] as const)(
    'reopens a collapsed webhook after %s returns a one-time secret',
    async (action) => {
      const request = deferred<Awaited<ReturnType<typeof saveMyWebhook>>>();
      vi.mocked(saveMyWebhook).mockReturnValue(request.promise);
      vi.mocked(getMyWebhook).mockResolvedValue(
        action === 'rotate'
          ? { configured: true, url: 'https://example.com/h', enabled: true }
          : { configured: false },
      );
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      render(<WebhookSettings />);
      fireEvent.click(screen.getByText('Webhook'));
      const field = screen.getByLabelText('Endpoint URL (https)');
      await waitFor(() => expect(field).toBeEnabled());
      if (action === 'create') {
        fireEvent.change(field, { target: { value: 'https://example.com/h' } });
        fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }));
      } else {
        fireEvent.click(screen.getByRole('button', { name: 'Rotate secret' }));
      }
      expect(saveMyWebhook).toHaveBeenCalledWith({
        url: 'https://example.com/h',
        enabled: true,
        rotate_secret: action === 'rotate',
      });
      fireEvent.click(screen.getByText('Webhook'));
      expect(field).not.toBeVisible();
      await act(async () => {
        request.resolve({
          configured: true,
          url: 'https://example.com/h',
          enabled: true,
          secret: 'whsec_once',
        });
        await request.promise;
      });
      expect(screen.getByText('whsec_once')).toBeVisible();
      expect(screen.getByRole('status')).toHaveTextContent(
        'Save the signing secret now; it is shown only once.',
      );
      fireEvent.click(screen.getByText('Webhook'));
      expect(screen.getByRole('status')).toBeVisible();
      confirm.mockRestore();
    },
  );

  it('reopens after a failed save and preserves an earlier one-time secret', async () => {
    const failure = deferred<Awaited<ReturnType<typeof saveMyWebhook>>>();
    vi.mocked(saveMyWebhook)
      .mockResolvedValueOnce({
        configured: true,
        url: 'https://example.com/h',
        secret: 'whsec_first',
      })
      .mockReturnValueOnce(failure.promise);
    render(<WebhookSettings />);
    fireEvent.click(screen.getByText('Webhook'));
    const field = screen.getByLabelText('Endpoint URL (https)');
    await waitFor(() => expect(field).toBeEnabled());
    fireEvent.change(field, { target: { value: 'https://example.com/h' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }));
    expect(await screen.findByText('whsec_first')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(screen.getByText('Webhook'));
    await act(async () => {
      failure.reject(new Error('Save failed'));
      await failure.promise.catch(() => {});
    });
    const alert = screen.getByRole('alert');
    expect(alert).toBeVisible();
    expect(alert).toHaveTextContent('Save failed');
    expect(screen.getAllByText('Save failed')).toHaveLength(1);
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(field).toHaveAttribute('aria-describedby', alert.id);
    expect(screen.getByText('whsec_first')).toBeVisible();
  });

  it.each(['before', 'after'] as const)(
    'does not reveal a pending secret when its account is invalidated %s the page remounts',
    async (settle) => {
      const request = deferred<Awaited<ReturnType<typeof saveMyWebhook>>>();
      vi.mocked(saveMyWebhook).mockReturnValue(request.promise);
      const oldScope = { userId: 'old', active: true };
      const newScope = { userId: 'new', active: true };
      const view = render(
        <AccountToastContext.Provider value={oldScope}>
          <WebhookSettings key="old" />
        </AccountToastContext.Provider>,
      );
      fireEvent.click(screen.getByText('Webhook'));
      const field = screen.getByLabelText('Endpoint URL (https)');
      await waitFor(() => expect(field).toBeEnabled());
      fireEvent.change(field, { target: { value: 'https://example.com/h' } });
      fireEvent.click(screen.getByRole('button', { name: 'Create webhook' }));
      oldScope.active = false;
      const showNewAccount = () =>
        view.rerender(
          <AccountToastContext.Provider value={newScope}>
            <WebhookSettings key="new" />
          </AccountToastContext.Provider>,
        );
      if (settle === 'after') showNewAccount();
      await act(async () => {
        request.resolve({ configured: true, url: 'https://example.com/h', secret: 'whsec_old' });
        await request.promise;
      });
      expect(screen.queryByText('whsec_old')).toBeNull();
      if (settle === 'before') showNewAccount();
      expect(screen.queryByText('Save the signing secret now; it is shown only once.')).toBeNull();
    },
  );

  it('below the submission role, shows the webhook read-only and lets it be removed', async () => {
    vi.mocked(getMyWebhook).mockResolvedValue({
      configured: true,
      url: 'https://old.example.com/h',
      enabled: true,
    });
    vi.mocked(deleteMyWebhook).mockResolvedValue({ deleted: true });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<WebhookSettings canEdit={false} />);
    fireEvent.click(screen.getByText('Webhook'));

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
    fireEvent.click(screen.getByText('Webhook'));
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
      expect(listMyBatches).toHaveBeenLastCalledWith(null, 20, { category: '', q: 'eval' }),
    );
  });

  it('offers no create button to a role that cannot submit', async () => {
    access = { capabilities: caps({ role_may_submit: false }), loading: false, error: null };
    vi.mocked(listMyBatches).mockResolvedValue(page([]));
    render(<BatchesView />);
    expect(await screen.findByText('You have no batches.')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Create batch' })).not.toBeInTheDocument();
  });

  it('filters by four categories plus All, asked of the server', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(page([]));
    render(<BatchesView />);
    await screen.findByText(/You have no batches/);
    fireEvent.click(screen.getByRole('combobox', { name: 'Status' }));
    const options = screen.getAllByRole('option').map((option) => option.textContent);
    expect(options).toEqual(['All', 'Processing', 'Completed', 'With issues', 'Cancelled']);
    fireEvent.click(screen.getByRole('option', { name: 'With issues' }));
    await waitFor(() =>
      expect(listMyBatches).toHaveBeenLastCalledWith(null, 20, { category: 'issues', q: '' }),
    );
  });

  it('never shows a partly failed batch as a green completion', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_partial', {
          status: 'completed',
          category: 'issues',
          phase: 'sealed',
          request_counts: { total: 1000, completed: 980, failed: 20 },
          results_available: true,
        }),
        batch('batch_ok', {
          status: 'completed',
          category: 'completed',
          phase: 'sealed',
          request_counts: { total: 10, completed: 10, failed: 0 },
          results_available: true,
        }),
      ]),
    );
    render(<BatchesView />);
    const partial = await screen.findByTestId('batch-batch_partial');
    const badge = partial.querySelector('[data-category]') as HTMLElement;
    expect(badge).toHaveTextContent('With issues');
    expect(badge.className).not.toMatch(/green/);
    expect(partial).toHaveTextContent('Some requests failed');
    expect(partial).toHaveTextContent('980 / 1,000 succeeded · 20 failed');
    const ok = screen.getByTestId('batch-batch_ok');
    expect(ok.querySelector('[data-category]')).toHaveTextContent('Completed');
    expect(ok).not.toHaveTextContent('Some requests failed');
  });

  it('keeps a pause warning beside a processing badge', async () => {
    vi.mocked(listMyBatches).mockResolvedValue(
      page([
        batch('batch_paused', {
          paused_until: 1_900_000_000,
          paused_reason: 'quota',
        }),
      ]),
    );
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-batch_paused');
    expect(row.querySelector('[data-category]')).toHaveTextContent('Processing');
    expect(row).toHaveTextContent(/Paused: your daily quota is spent/);
  });
});

describe('BatchesView with a gateway older than the categories', () => {
  it('shows a partly failed batch without a category as with issues, never green', async () => {
    const { category: _omitted, ...withoutCategory } = batch('batch_old', {
      status: 'completed',
      phase: 'sealed',
      request_counts: { total: 10, completed: 8, failed: 2 },
      results_available: true,
    });
    vi.mocked(listMyBatches).mockResolvedValue(page([withoutCategory as ConsoleBatch]));
    render(<BatchesView />);
    const row = await screen.findByTestId('batch-batch_old');
    const badge = row.querySelector('[data-category]') as HTMLElement;
    expect(badge).toHaveTextContent('With issues');
    expect(badge.className).not.toMatch(/green/);
    expect(row).toHaveTextContent('Some requests failed');
  });

  it('says filtering is unavailable instead of showing an unfiltered page', async () => {
    vi.mocked(listMyBatches).mockImplementation(async (_after, _limit, filters) => {
      if (filters?.category) throw new BatchCategoryFilterUnavailable();
      return page([batch('batch_any')]);
    });
    render(<BatchesView />);
    await screen.findByTestId('batch-batch_any');
    fireEvent.click(screen.getByRole('combobox', { name: 'Status' }));
    fireEvent.click(screen.getByRole('option', { name: 'With issues' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Filtering by status is not available right now',
    );
    expect(screen.queryByTestId('batch-batch_any')).not.toBeInTheDocument();
  });

  it('does not add a later page an older gateway answered unfiltered', async () => {
    vi.mocked(listMyBatches).mockImplementation(async (after, _limit, filters) => {
      if (filters?.category && after) throw new BatchCategoryFilterUnavailable();
      return filters?.category
        ? page([batch('batch_issue', { status: 'failed', category: 'issues' })], true)
        : page([batch('batch_any')]);
    });
    render(<BatchesView />);
    await screen.findByTestId('batch-batch_any');
    fireEvent.click(screen.getByRole('combobox', { name: 'Status' }));
    fireEvent.click(screen.getByRole('option', { name: 'With issues' }));
    await screen.findByTestId('batch-batch_issue');
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Filtering by status is not available right now',
    );
    expect(screen.getAllByRole('listitem').map((item) => item.getAttribute('data-testid'))).toEqual(
      ['batch-batch_issue'],
    );
  });
});

describe('the category labels in Chinese', () => {
  it('are the agreed five', async () => {
    const catalog = (await import('@/lib/i18n/messages/zh-CN.json')).default as unknown as Record<
      string,
      { text: string }
    >;
    expect(
      [
        'batches.filter.all',
        'batches.category.processing',
        'batches.category.completed',
        'batches.category.issues',
        'batches.category.cancelled',
      ].map((slot) => catalog[slot].text),
    ).toEqual(['全部', '处理中', '已完成', '有异常', '已取消']);
    expect(
      ['batches.note.partial', 'batches.note.expired', 'batches.note.cancelling'].map(
        (slot) => catalog[slot].text,
      ),
    ).toEqual(['部分请求失败', '超过执行期限', '正在取消，进行中的请求会完成']);
  });
});
