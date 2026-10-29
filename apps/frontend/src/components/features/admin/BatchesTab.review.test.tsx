// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchesTab } from './BatchesTab';
import { getBatchSummary, listAllBatches } from '@/lib/api/batches';
import type { AdminBatchSummary, ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';

vi.mock('@/lib/api/batches', () => ({
  getBatchSummary: vi.fn(),
  listAllBatches: vi.fn(),
  adminCancelBatch: vi.fn(),
  isCancellable: () => false,
}));
vi.mock('@/components/features/admin/confirm/useConfirmAction', () => ({
  useConfirmAction: () => ({ request: vi.fn(), dialog: null, busy: false }),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function page(id: string, hasMore = false): ConsoleBatchPage {
  const batch = {
    id,
    user_id: `${id}-owner`,
    api_surface: 'openai_inline',
    input: 'inline',
    target: 'chat',
    endpoint: '/v1/chat/completions',
    models: ['m'],
    status: 'completed',
    request_counts: { total: 1, completed: 1, failed: 0 },
    cancelled: 0,
    expired: 0,
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
    cost_usd: 0,
    cost_unknown_attempts: 0,
    created_at: 1,
  } as unknown as ConsoleBatch;
  return { object: 'list', data: [batch], has_more: hasMore, last_id: id };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getBatchSummary).mockResolvedValue({ users: [], totals: {} } as never);
});

describe('Batch list request ownership', () => {
  it('ignores the old filter response after a newer filter has loaded', async () => {
    const old = deferred<ConsoleBatchPage>();
    vi.mocked(listAllBatches).mockImplementation((filters) =>
      filters?.userId === 'new-owner' ? Promise.resolve(page('new-filter-batch')) : old.promise,
    );
    render(<BatchesTab />);
    await waitFor(() => expect(listAllBatches).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText('Owner user id'), { target: { value: 'new-owner' } });
    await screen.findByText('new-filter-batch');
    await act(async () => {
      old.resolve(page('old-filter-batch'));
      await old.promise;
    });
    expect(screen.queryByText('old-filter-batch')).toBeNull();
    expect(screen.queryByText('new-filter-batch')).not.toBeNull();
  });

  it('does not request the same next page twice while Show more is pending', async () => {
    const next = deferred<ConsoleBatchPage>();
    vi.mocked(listAllBatches).mockImplementation((filters) =>
      filters?.after ? next.promise : Promise.resolve(page('first-batch', true)),
    );
    render(<BatchesTab />);
    const more = await screen.findByRole('button', { name: 'Show more' });
    fireEvent.click(more);
    fireEvent.click(more);
    const requestsWhilePending = vi.mocked(listAllBatches).mock.calls.length;
    await act(async () => {
      next.resolve(page('second-batch'));
      await next.promise;
    });
    expect(requestsWhilePending).toBe(2);
    expect(screen.getAllByText('second-batch')).toHaveLength(1);
  });
});

describe('Batch filters', () => {
  it('filters by the status the list shows, with no API dropdown', async () => {
    vi.mocked(listAllBatches).mockResolvedValue(page('any-batch'));
    render(<BatchesTab />);
    await screen.findByText('any-batch');
    expect(screen.queryByLabelText('API')).toBeNull();
    expect(screen.queryByText(/OpenAI|Anthropic/)).toBeNull();
    // The target column shows what the batch runs and how its requests arrived.
    expect(screen.queryByText('/v1/chat/completions')).not.toBeNull();
    expect(screen.queryByText('Direct requests · m')).not.toBeNull();
  });
});

describe('Batch usage by user', () => {
  it('counts cancelled requests as done, as each batch does', async () => {
    const usage = {
      batches: 3,
      active: 1,
      requests: 592445,
      completed: 590000,
      failed: 1200,
      cancelled: 653,
      cost_usd: 0,
      prompt_tokens: 10,
      completion_tokens: 5,
      unknown_cost_attempts: 1066,
    };
    const summary: AdminBatchSummary = {
      users: [{ ...usage, user_id: 'owner-1', last_created_at: 1 }],
      totals: usage,
    };
    vi.mocked(getBatchSummary).mockResolvedValue(summary);
    vi.mocked(listAllBatches).mockResolvedValue({
      object: 'list',
      data: [],
      has_more: false,
      last_id: null,
    } as ConsoleBatchPage);
    render(<BatchesTab />);

    const row = (await screen.findByRole('button', { name: 'owner-1' })).closest('tr');
    const footer = screen.getByText('All users').closest('tr');
    for (const cells of [row, footer]) {
      expect(cells?.textContent).toContain('591,853/592,445');
      expect(cells?.textContent).toContain('1,066');
    }
  });
});
