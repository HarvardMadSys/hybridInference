// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { cancelMyBatch, getMyBatch, type BatchDetail } from '@/lib/api/batches';
import { APIError } from '@/lib/utils/errors';

import { BatchDetailView } from './BatchDetailView';

let currentUser = 'alice';
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: currentUser } } }),
}));
vi.mock('@/lib/hooks/queries/useBatchCapabilities', () => ({
  useBatchCapabilities: () => ({
    capabilities: { role_may_submit: true },
    loading: false,
    error: null,
  }),
}));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('@/lib/api/batches', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/batches')>();
  return { ...actual, getMyBatch: vi.fn(), cancelMyBatch: vi.fn() };
});

function detail(overrides: Partial<BatchDetail> = {}): BatchDetail {
  return {
    id: 'batch_1',
    user_id: 'alice',
    api_surface: 'openai_file',
    input: 'file',
    target: 'chat',
    endpoint: '/v1/chat/completions',
    status: 'completed',
    category: 'issues',
    phase: 'sealed',
    models: ['model-z'],
    request_counts: { total: 10, completed: 6, failed: 3 },
    cancelled: 1,
    expired: 1,
    usage: { input_tokens: 100, output_tokens: 50, total_tokens: 150 },
    cost_usd: 0.5,
    cost_unknown_attempts: 2,
    created_at: 1_790_000_000,
    expires_at: null,
    ended_at: null,
    results_expires_at: null,
    input_file_id: 'file-1',
    output_file_id: 'file-out',
    error_file_id: 'file-err',
    results_available: true,
    metadata: null,
    error: null,
    validation_error: null,
    protocol_profile: null,
    name: 'eval-0420',
    created_via: 'console',
    creation: null,
    source: { id: 'file-1', state: 'available', filename: 'requests.jsonl' },
    copy: { state: 'ready', reason: null },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  currentUser = 'alice';
});
afterEach(cleanup);

describe('BatchDetailView', () => {
  it('shows the unified counts, unknown cost and both result files of a file batch', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(detail());
    render(<BatchDetailView batchId="batch_1" />);
    expect(await screen.findByTestId('count-succeeded')).toHaveTextContent('6');
    expect(screen.getByTestId('count-failed')).toHaveTextContent('2');
    expect(screen.getByTestId('count-expired')).toHaveTextContent('1');
    expect(screen.getByTestId('count-cancelled')).toHaveTextContent('1');
    expect(screen.getByTestId('count-unfinished')).toHaveTextContent('0');
    expect(screen.getByText(/2 attempt\(s\) of unknown cost, not included/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download outputs' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download errors' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download results' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export unfinished requests' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Copy as new batch' })).toHaveAttribute(
      'href',
      '/dashboard/batches/new?copy=batch_1',
    );
  });

  it('offers one download for every result of a direct-request batch, and explains why it cannot be copied', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(
      detail({
        input: 'inline',
        api_surface: 'openai_inline',
        output_file_id: null,
        error_file_id: null,
        source: null,
        copy: { state: 'unavailable', reason: 'inline_input' },
      }),
    );
    render(<BatchDetailView batchId="batch_1" />);
    expect(await screen.findByRole('button', { name: 'Download results' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download outputs' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download errors' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Copy as new batch' })).not.toBeInTheDocument();
    expect(screen.getByText(/no input file to reuse/)).toBeInTheDocument();
  });

  it('manages a Messages batch without offering copy or the unfinished export', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(
      detail({ target: 'messages', copy: { state: 'unavailable', reason: 'unsupported_target' } }),
    );
    render(<BatchDetailView batchId="batch_1" />);
    expect(await screen.findByText(/cannot be created in the console yet/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Export unfinished requests' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download outputs' })).toBeInTheDocument();
  });

  it('lists every input problem the gateway returned, not only the first five', async () => {
    const problems = [
      ...Array.from({ length: 6 }, (_, index) => ({
        line: index + 1,
        message: `problem ${index + 1}`,
      })),
      { message: 'file-level problem' },
    ];
    vi.mocked(getMyBatch).mockResolvedValue(
      detail({ status: 'failed', validation_error: { data: problems } }),
    );
    render(<BatchDetailView batchId="batch_1" />);
    const list = await screen.findByTestId('validation-problems');
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(7);
    expect(items[5]).toHaveTextContent('Line 6: problem 6');
    expect(items[6]).toHaveTextContent('file-level problem');
  });

  it('says why a batch is paused', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(
      detail({
        status: 'in_progress',
        phase: 'runnable',
        results_available: false,
        paused_until: 1_900_000_000,
        paused_reason: 'quota',
      }),
    );
    render(<BatchDetailView batchId="batch_1" />);
    expect(await screen.findByText(/Paused: your daily quota is spent/)).toBeInTheDocument();
    expect(
      screen.getByText('Results can be downloaded once the batch has finished.'),
    ).toBeInTheDocument();
  });

  it('never shows an answer naming another account', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(detail({ user_id: 'bob' }));
    render(<BatchDetailView batchId="batch_1" />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText('eval-0420')).not.toBeInTheDocument();
  });

  it('shows the same category and note as the list', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(detail());
    render(<BatchDetailView batchId="batch_1" />);
    const badge = await screen.findByTestId('batch-category');
    expect(badge).toHaveTextContent('With issues');
    expect(badge).toHaveTextContent('Some requests failed');
  });

  it('offers no result download while results are being prepared', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(
      detail({
        status: 'finalizing',
        category: 'processing',
        phase: 'sealing',
        results_available: false,
      }),
    );
    render(<BatchDetailView batchId="batch_1" />);
    const badge = await screen.findByTestId('batch-category');
    expect(badge).toHaveTextContent('Processing');
    expect(badge).toHaveTextContent('Preparing results');
    for (const name of ['Download outputs', 'Download errors', 'Download results']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('says an expired batch ran out of its execution window, not that its files expired', async () => {
    vi.mocked(getMyBatch).mockResolvedValue(detail({ status: 'expired', category: 'issues' }));
    render(<BatchDetailView batchId="batch_1" />);
    expect(await screen.findByTestId('batch-category')).toHaveTextContent(
      'With issues' + 'Execution window ran out',
    );
  });

  it('shows a batch from an older gateway, without a category, by the same rule', async () => {
    const { category: _omitted, ...withoutCategory } = detail();
    vi.mocked(getMyBatch).mockResolvedValue(withoutCategory as BatchDetail);
    render(<BatchDetailView batchId="batch_1" />);
    const badge = await screen.findByTestId('batch-category');
    expect(badge).toHaveTextContent('With issues');
    expect(badge).toHaveTextContent('Some requests failed');
  });
});

describe('BatchDetailView polling', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('follows an expired direct-request batch until its results are sealed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const unsealed = detail({
      api_surface: 'openai_inline',
      input: 'inline',
      status: 'expired',
      phase: 'runnable',
      results_available: false,
    });
    vi.mocked(getMyBatch)
      .mockResolvedValueOnce(unsealed)
      .mockResolvedValueOnce({ ...unsealed, phase: 'sealed', results_available: true })
      .mockResolvedValue(detail({ status: 'expired' }));
    render(<BatchDetailView batchId="batch_1" />);
    await screen.findByText('eval-0420');

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(getMyBatch).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(getMyBatch).toHaveBeenCalledTimes(2);
  });

  it('stops following a batch that was deleted and says so', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getMyBatch)
      .mockResolvedValueOnce(
        detail({ status: 'in_progress', phase: 'runnable', results_available: false }),
      )
      .mockRejectedValue(new APIError('NOT_FOUND', "Batch 'batch_1' not found", 404));
    render(<BatchDetailView batchId="batch_1" />);
    await screen.findByText('eval-0420');

    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(getMyBatch).toHaveBeenCalledTimes(2);
  });
});

describe('BatchDetailView polling with a slow gateway', () => {
  // Every read takes longer to answer than the page waits between polls.
  const SLOW_MS = 6000;
  const POLL_MS = 5000;

  function slow<T>(value: T, ms = SLOW_MS): Promise<T> {
    return new Promise((resolve) => setTimeout(() => resolve(value), ms));
  }

  function running(done: number, overrides: Partial<BatchDetail> = {}): BatchDetail {
    return detail({
      api_surface: 'openai_inline',
      input: 'inline',
      status: 'in_progress',
      phase: 'runnable',
      results_available: false,
      request_counts: { total: 10, completed: done, failed: 0 },
      cancelled: 0,
      expired: 0,
      ...overrides,
    });
  }

  function sealed(overrides: Partial<BatchDetail> = {}): BatchDetail {
    return running(10, {
      status: 'completed',
      phase: 'sealed',
      results_available: true,
      ...overrides,
    });
  }

  const advance = (ms: number) => act(async () => void (await vi.advanceTimersByTimeAsync(ms)));

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('moves forward one poll at a time, then shows the download and stops at sealed results', async () => {
    const answers = [running(0), running(4), sealed()];
    let call = 0;
    vi.mocked(getMyBatch).mockImplementation(() => slow(answers[Math.min(call++, 2)]));
    render(<BatchDetailView batchId="batch_1" />);

    await advance(SLOW_MS);
    expect(screen.getByTestId('count-succeeded')).toHaveTextContent('0');
    // The next read is sent once the previous one answered, never on top of it.
    await advance(POLL_MS + SLOW_MS);
    expect(getMyBatch).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('count-succeeded')).toHaveTextContent('4');
    await advance(POLL_MS + SLOW_MS);
    expect(screen.getByTestId('count-succeeded')).toHaveTextContent('10');
    expect(screen.getByRole('button', { name: 'Download results' })).toBeInTheDocument();

    await advance(4 * (POLL_MS + SLOW_MS));
    expect(getMyBatch).toHaveBeenCalledTimes(3);
  });

  it('stops polling a batch deleted while a slow poll was in flight', async () => {
    vi.mocked(getMyBatch)
      .mockImplementationOnce(() => slow(running(1)))
      .mockImplementationOnce(() =>
        slow(null).then(() => {
          throw new APIError('NOT_FOUND', "Batch 'batch_1' not found", 404);
        }),
      );
    render(<BatchDetailView batchId="batch_1" />);
    await advance(SLOW_MS);
    expect(screen.getByText('eval-0420')).toBeInTheDocument();

    await advance(POLL_MS + SLOW_MS);
    expect(screen.getByRole('alert')).toHaveTextContent('not found');
    await advance(4 * (POLL_MS + SLOW_MS));
    expect(getMyBatch).toHaveBeenCalledTimes(2);
  });

  it('keeps the state a refresh after a cancel read, not the older poll still in flight', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(cancelMyBatch).mockResolvedValue(running(2, { status: 'cancelling' }) as never);
    vi.mocked(getMyBatch)
      .mockResolvedValueOnce(running(2))
      // The poll, sent before the cancel and answered after the refresh.
      .mockImplementationOnce(() => slow(running(2)))
      // The refresh the cancel makes.
      .mockResolvedValueOnce(sealed({ status: 'cancelled' }))
      .mockImplementation(() => slow(running(2)));
    render(<BatchDetailView batchId="batch_1" />);
    await advance(0);
    await advance(POLL_MS + 1000);
    expect(getMyBatch).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await advance(0);
    expect(screen.getByRole('button', { name: 'Download results' })).toBeInTheDocument();

    await advance(SLOW_MS);
    expect(screen.getByRole('button', { name: 'Download results' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
    await advance(4 * (POLL_MS + SLOW_MS));
    expect(getMyBatch).toHaveBeenCalledTimes(3);
  });

  it('drops a slow answer for the batch the page showed before', async () => {
    let firstRead = true;
    vi.mocked(getMyBatch).mockImplementation((id: string) => {
      if (id === 'batch_2') return Promise.resolve(running(7, { id: 'batch_2', name: 'second' }));
      if (firstRead) {
        firstRead = false;
        return Promise.resolve(running(1));
      }
      return slow(running(1));
    });
    const { rerender } = render(<BatchDetailView batchId="batch_1" />);
    await advance(0);
    await advance(POLL_MS + 1000);

    rerender(<BatchDetailView batchId="batch_2" />);
    await advance(0);
    expect(screen.getByText('second')).toBeInTheDocument();
    await advance(SLOW_MS);
    expect(screen.getByText('second')).toBeInTheDocument();
    expect(screen.queryByText('eval-0420')).not.toBeInTheDocument();
    expect(screen.getByTestId('count-succeeded')).toHaveTextContent('7');
  });

  it('drops a slow answer for the account signed in before', async () => {
    vi.mocked(getMyBatch)
      .mockResolvedValueOnce(running(1))
      .mockImplementationOnce(() => slow(running(1)))
      .mockResolvedValue(running(9, { user_id: 'bob', name: 'bobs-batch' }));
    const { rerender } = render(<BatchDetailView batchId="batch_1" />);
    await advance(0);
    await advance(POLL_MS + 1000);

    currentUser = 'bob';
    rerender(<BatchDetailView batchId="batch_1" />);
    await advance(0);
    expect(screen.getByText('bobs-batch')).toBeInTheDocument();
    await advance(SLOW_MS);
    expect(screen.getByText('bobs-batch')).toBeInTheDocument();
    expect(screen.queryByText('eval-0420')).not.toBeInTheDocument();
  });
});
