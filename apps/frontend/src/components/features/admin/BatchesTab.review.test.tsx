// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BatchesTab } from './BatchesTab';
import { getBatchSummary, listAllBatches } from '@/lib/api/batches';
import type { ConsoleBatch, ConsoleBatchPage } from '@/lib/api/batches';

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
    target: 'chat',
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
