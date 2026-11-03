// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within, cleanup } from '@testing-library/react';
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

describe('Batch usage', () => {
  const usage = {
    batches: 3,
    active: 1,
    requests: 592445,
    completed: 590000,
    failed: 1200,
    cancelled: 653,
    expired: 200,
    cost_usd: 1234.5,
    prompt_tokens: 12_900_000,
    completion_tokens: 4_200,
    unknown_cost_attempts: 1066,
  };
  const empty = { object: 'list', data: [], has_more: false, last_id: null } as ConsoleBatchPage;

  it('splits requests by outcome instead of one done count', async () => {
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...usage, user_id: 'owner-1', last_created_at: 1 }],
      totals: usage,
    });
    vi.mocked(listAllBatches).mockResolvedValue(empty);
    render(<BatchesTab />);

    const row = await screen.findByTestId('usage-owner-1');
    // Expired requests are part of the gateway's failed count: shown apart, not twice.
    // 592,445 - 590,000 - 1,200 - 653 = 592 queued or running.
    for (const part of [
      '590,000 succeeded',
      '1,000 failed',
      '200 expired unrun',
      '653 cancelled',
      '592 not finished',
    ]) {
      expect(row.textContent).toContain(part);
    }
    expect(row.textContent).toContain('99.6% succeeded');
    expect(row.textContent).toContain('1 running');
    expect(row.textContent).toContain('12.9M in · 4,200 out');
    expect(row.textContent).toContain('$1,234.50');
    expect(row.textContent).toContain('+ 1,066 unpriced attempts');
    expect(row.textContent).not.toMatch(/\d\/\d/);
  });

  it('leads with the totals across every user', async () => {
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...usage, user_id: 'owner-1', last_created_at: 1 }],
      totals: usage,
    });
    vi.mocked(listAllBatches).mockResolvedValue(empty);
    render(<BatchesTab />);

    await screen.findByTestId('usage-owner-1');
    const tile = (label: string) => screen.getByText(label, { selector: 'dt' }).parentElement;
    expect(tile('Batches')?.textContent).toContain('1 running');
    expect(tile('Requests')?.textContent).toContain('592.4K');
    expect(tile('Requests')?.textContent).toContain('99.6% succeeded');
    expect(
      within(tile('Requests')!).getByRole('img', {
        name: '590,000 succeeded, 1,000 failed, 200 expired unrun, 653 cancelled, 592 not finished',
      }),
    ).not.toBeNull();
    expect(tile('Tokens')?.textContent).toContain('12.9M');
    expect(tile('Known cost')?.textContent).toContain('+ 1,066 unpriced attempts');
  });

  it('keeps expired requests in failed for a gateway that does not report them', async () => {
    const { expired: _omitted, ...older } = usage;
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...older, user_id: 'owner-1', last_created_at: 1 }],
      totals: older,
    });
    vi.mocked(listAllBatches).mockResolvedValue(empty);
    render(<BatchesTab />);

    const row = await screen.findByTestId('usage-owner-1');
    expect(row.textContent).toContain('1,200 failed');
    expect(row.textContent).not.toContain('expired');
  });

  it('lists only the selected user, and every user again on a second select', async () => {
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...usage, user_id: 'owner-1', last_created_at: 1 }],
      totals: usage,
    });
    vi.mocked(listAllBatches).mockResolvedValue(empty);
    render(<BatchesTab />);

    const owner = await screen.findByRole('button', { name: 'owner-1' });
    fireEvent.click(owner);
    await waitFor(() =>
      expect(listAllBatches).toHaveBeenLastCalledWith(
        expect.objectContaining({ userId: 'owner-1' }),
      ),
    );
    expect(owner.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('Batches of owner-1')).not.toBeNull();
    fireEvent.click(owner);
    await waitFor(() =>
      expect(listAllBatches).toHaveBeenLastCalledWith(expect.objectContaining({ userId: '' })),
    );
    expect(screen.getByText('All batches')).not.toBeNull();
  });
});

describe('Batch list', () => {
  it('shows each batch by outcome, with its status in words', async () => {
    const batch = page('mixed-batch').data[0];
    vi.mocked(listAllBatches).mockResolvedValue({
      object: 'list',
      data: [
        {
          ...batch,
          name: 'Nightly eval',
          status: 'in_progress',
          request_counts: { total: 10, completed: 6, failed: 2 },
          cancelled: 0,
          expired: 1,
          cost_usd: 0.0123,
          cost_unknown_attempts: 1,
        } as ConsoleBatch,
      ],
      has_more: false,
      last_id: 'mixed-batch',
    });
    render(<BatchesTab />);

    const row = await screen.findByTestId('batch-mixed-batch');
    expect(row.textContent).toContain('Nightly eval');
    expect(row.textContent).toContain('In progress');
    expect(row.textContent).not.toContain('in_progress');
    for (const part of ['6 succeeded', '1 failed', '1 expired unrun', '2 not finished']) {
      expect(row.textContent).toContain(part);
    }
    expect(row.textContent).toContain('$0.0123');
    expect(row.textContent).toContain('+ 1 unpriced attempt');
    expect(row.textContent).not.toContain('unpriced attempts');
  });
});

describe('Owner names and cached input', () => {
  const usage = {
    batches: 2,
    active: 0,
    requests: 10,
    completed: 10,
    failed: 0,
    cancelled: 0,
    expired: 0,
    cost_usd: 0,
    prompt_tokens: 1000,
    completion_tokens: 200,
    unknown_cost_attempts: 0,
    cached_tokens: 250,
    cache_unrecorded_batches: 0,
  };
  const empty = { object: 'list', data: [], has_more: false, last_id: null } as ConsoleBatchPage;

  it('names each owner and shows how much input came from the cache', async () => {
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [
        {
          ...usage,
          user_id: 'u-1',
          user_name: 'Ada',
          email: 'ada@example.test',
          last_created_at: 1,
        },
        // An account with no display name is named by its email.
        { ...usage, user_id: 'u-2', user_name: null, email: 'bo@example.test', last_created_at: 1 },
      ],
      totals: { ...usage, cached_tokens: 500, prompt_tokens: 2000 },
    });
    vi.mocked(listAllBatches).mockResolvedValue(empty);
    render(<BatchesTab />);

    const ada = await screen.findByTestId('usage-u-1');
    expect(within(ada).getByRole('button', { name: 'Ada' })).not.toBeNull();
    expect(ada.textContent).toContain('ada@example.test');
    expect(ada.textContent).toContain('u-1');
    expect(ada.textContent).toContain('250 cached (25%)');
    expect(
      within(screen.getByTestId('usage-u-2')).getByRole('button', { name: 'bo@example.test' }),
    ).not.toBeNull();

    const tile = screen.getByText('Tokens', { selector: 'dt' }).parentElement;
    expect(tile?.textContent).toContain('500 cached (25%)');

    fireEvent.click(within(ada).getByRole('button', { name: 'Ada' }));
    expect(await screen.findByText('Batches of Ada')).not.toBeNull();
  });

  it('says which batches ran before cached input was recorded', async () => {
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...usage, user_id: 'u-1', cache_unrecorded_batches: 1, last_created_at: 1 }],
      totals: { ...usage, cache_unrecorded_batches: 1 },
    });
    const batch = page('old-batch').data[0];
    vi.mocked(listAllBatches).mockResolvedValue({
      object: 'list',
      data: [
        {
          ...batch,
          user_name: 'Ada',
          usage: { ...batch.usage, cached_input_tokens: null },
        } as ConsoleBatch,
      ],
      has_more: false,
      last_id: 'old-batch',
    });
    render(<BatchesTab />);

    const user = await screen.findByTestId('usage-u-1');
    // A share of a total that leaves batches out would understate it: none is shown.
    expect(user.textContent).toContain('250 cached · 1 batch not recorded');
    expect(user.textContent).not.toContain('%)');
    const row = await screen.findByTestId('batch-old-batch');
    expect(row.textContent).toContain('Ada');
    expect(row.textContent).toContain('old-batch-owner');
    expect(row.textContent).toContain('Cached: not recorded');
  });

  it('shows nothing about caching for a gateway that does not report it', async () => {
    const { cached_tokens: _c, cache_unrecorded_batches: _u, ...older } = usage;
    vi.mocked(getBatchSummary).mockResolvedValue({
      users: [{ ...older, user_id: 'u-1', last_created_at: 1 }],
      totals: older,
    });
    vi.mocked(listAllBatches).mockResolvedValue(page('any-batch'));
    render(<BatchesTab />);

    const user = await screen.findByTestId('usage-u-1');
    expect(user.textContent).not.toContain('cached');
    expect(within(user).getByRole('button', { name: 'u-1' })).not.toBeNull();
    const row = await screen.findByTestId('batch-any-batch');
    expect(row.textContent).not.toMatch(/cached/i);
  });
});
