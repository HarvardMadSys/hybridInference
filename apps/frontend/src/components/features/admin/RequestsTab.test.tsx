// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminRecentRequestItem } from '@/lib/api/admin';
import { RequestsTab } from './RequestsTab';

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  listRecentRequests: vi.fn(),
  getRequestMetrics: vi.fn(),
  getRecentRequestContent: vi.fn(),
  getRecentRequestsPerformance: vi.fn(),
  clearErrorRequests: vi.fn(),
  exportRequests: vi.fn(),
}));

import {
  exportRequests,
  getRecentRequestContent,
  getRecentRequestsPerformance,
  getRequestMetrics,
  listRecentRequests,
} from '@/lib/api/admin';

function makeRequest(overrides: Partial<AdminRecentRequestItem> = {}): AdminRecentRequestItem {
  return {
    request_id: 'req_abc123',
    user_id: 'user_1',
    user_name: 'Ada',
    user_email: 'ada@example.com',
    model_id: 'gpt-4o-mini',
    provider: 'openai',
    timestamp: '2026-06-30T12:00:00.000Z',
    status_code: 200,
    latency_ms: 1200,
    prompt_tokens: 100,
    completion_tokens: 50,
    ...overrides,
  };
}

type PointerCaptureProto = {
  setPointerCapture: (pointerId: number) => void;
  hasPointerCapture: (pointerId: number) => boolean;
  releasePointerCapture: (pointerId: number) => void;
};

function getScrollContainer(): HTMLElement {
  const el = document.querySelector<HTMLElement>('[aria-label="Recent requests table"]');
  if (!el) throw new Error('request table scroll container not found');
  return el;
}

// jsdom performs no layout, so scrollWidth/clientWidth are both 0 and the
// drag-to-pan path in RequestTableScrollArea no-ops. Force the container to
// report horizontal overflow so the pointer handlers actually engage.
function forceOverflow(el: HTMLElement): void {
  Object.defineProperty(el, 'scrollWidth', { configurable: true, value: 1000 });
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: 200 });
}

describe('RequestsTab row expansion', () => {
  let setPointerCaptureSpy: ReturnType<typeof vi.fn>;
  let releasePointerCaptureSpy: ReturnType<typeof vi.fn>;

  // jsdom does not implement the Pointer Capture API. Stash whatever is there
  // (usually nothing) so we can restore it in afterEach and not leak our
  // stand-ins onto Element.prototype for other test files.
  const protoOriginal = Element.prototype as unknown as Partial<PointerCaptureProto>;
  const original = {
    setPointerCapture: protoOriginal.setPointerCapture,
    hasPointerCapture: protoOriginal.hasPointerCapture,
    releasePointerCapture: protoOriginal.releasePointerCapture,
  };

  beforeEach(() => {
    vi.clearAllMocks();

    // Track captured pointer ids so hasPointerCapture/releasePointerCapture
    // behave realistically — this exercises the component's release path on
    // drag end rather than stubbing it out.
    const captured = new Set<number>();
    setPointerCaptureSpy = vi.fn((pointerId: number) => {
      captured.add(pointerId);
    });
    releasePointerCaptureSpy = vi.fn((pointerId: number) => {
      captured.delete(pointerId);
    });
    const proto = Element.prototype as unknown as PointerCaptureProto;
    proto.setPointerCapture =
      setPointerCaptureSpy as unknown as PointerCaptureProto['setPointerCapture'];
    proto.hasPointerCapture = ((pointerId: number) =>
      captured.has(pointerId)) as PointerCaptureProto['hasPointerCapture'];
    proto.releasePointerCapture =
      releasePointerCaptureSpy as unknown as PointerCaptureProto['releasePointerCapture'];

    vi.mocked(getRequestMetrics).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      windows: [],
    });
    vi.mocked(getRecentRequestContent).mockResolvedValue({
      prompt: 'hello world',
      response: 'hi there',
      reasoning_content: null,
    });
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [makeRequest()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 1,
      groups: [],
      truncated: false,
    });
  });

  afterEach(() => {
    cleanup();
    const proto = Element.prototype as unknown as Partial<PointerCaptureProto>;
    if (original.setPointerCapture) {
      proto.setPointerCapture = original.setPointerCapture;
    } else {
      delete proto.setPointerCapture;
    }
    if (original.hasPointerCapture) {
      proto.hasPointerCapture = original.hasPointerCapture;
    } else {
      delete proto.hasPointerCapture;
    }
    if (original.releasePointerCapture) {
      proto.releasePointerCapture = original.releasePointerCapture;
    } else {
      delete proto.releasePointerCapture;
    }
  });

  it('expands a row on a plain click without capturing the pointer', async () => {
    render(<RequestsTab />);

    const cell = await screen.findByText('gpt-4o-mini');
    forceOverflow(getScrollContainer());

    expect(screen.queryByText('Request ID:')).not.toBeInTheDocument();

    // A real mouse click is pointerdown -> pointerup -> click with no movement.
    fireEvent.pointerDown(cell, { button: 0, pointerId: 1, pointerType: 'mouse', clientX: 40 });
    fireEvent.pointerUp(cell, { pointerId: 1, pointerType: 'mouse', clientX: 40 });
    fireEvent.click(cell);

    // The detail panel opens and the prompt/response content lazy-loads.
    expect(screen.getByText('Request ID:')).toBeInTheDocument();
    expect(await screen.findByText('Prompt:')).toBeInTheDocument();
    expect(getRecentRequestContent).toHaveBeenCalledWith('req_abc123');

    // Regression guard for PR #826: capturing the pointer on pointerdown
    // retargets the trailing click to the scroll container, so the row's
    // onClick never fires and clicking a request stops showing its details.
    expect(setPointerCaptureSpy).not.toHaveBeenCalled();
    expect(releasePointerCaptureSpy).not.toHaveBeenCalled();
  });

  it('pans on mouse drag and swallows the trailing click instead of expanding', async () => {
    render(<RequestsTab />);

    const cell = await screen.findByText('gpt-4o-mini');
    forceOverflow(getScrollContainer());

    fireEvent.pointerDown(cell, { button: 0, pointerId: 1, pointerType: 'mouse', clientX: 40 });
    // Move well past the slop threshold: this is unambiguously a drag, not a tap.
    fireEvent.pointerMove(cell, { pointerId: 1, pointerType: 'mouse', clientX: 140 });
    fireEvent.pointerUp(cell, { pointerId: 1, pointerType: 'mouse', clientX: 140 });
    fireEvent.click(cell);

    // A real drag captures the pointer (so panning keeps tracking even if it
    // leaves the table) and suppresses the click, so no row expands.
    expect(setPointerCaptureSpy).toHaveBeenCalledTimes(1);
    // The capture is released when the drag ends (endDrag's hasPointerCapture
    // guard sees the still-captured pointer).
    expect(releasePointerCaptureSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Request ID:')).not.toBeInTheDocument();
    expect(getRecentRequestContent).not.toHaveBeenCalled();
  });

  it('loads the per-endpoint performance panel with the tab filters', async () => {
    render(<RequestsTab />);

    await screen.findByText('gpt-4o-mini');
    expect(getRecentRequestsPerformance).toHaveBeenCalledWith({
      // The panel's own window, not the tab's 7d default.
      days: 1,
      userId: undefined,
      modelId: undefined,
      requestType: undefined,
      refresh: false,
    });

    // The lookback re-scopes the list but not the summary: TTFT and decode
    // throughput are read over the panel's fixed day whatever the tab shows.
    fireEvent.change(screen.getByLabelText('Lookback window'), { target: { value: '30' } });
    await waitFor(() =>
      // days is the 7th positional argument of listRecentRequests.
      expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[6]).toBe(30),
    );
    expect(getRecentRequestsPerformance).toHaveBeenCalledTimes(1);

    // Refresh reloads the summary and asks the backend for fresh numbers.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
        expect.objectContaining({ days: 1, refresh: true }),
      ),
    );
  });

  it('leaves touch gestures to native scrolling (no JS pan / pointer capture)', async () => {
    render(<RequestsTab />);

    const cell = await screen.findByText('gpt-4o-mini');
    forceOverflow(getScrollContainer());

    // A touch drag must NOT be hijacked by the JS pan handler — touch relies on
    // the browser's native horizontal scrolling so it keeps inertial momentum.
    fireEvent.pointerDown(cell, { button: 0, pointerId: 2, pointerType: 'touch', clientX: 40 });
    fireEvent.pointerMove(cell, { pointerId: 2, pointerType: 'touch', clientX: 140 });
    fireEvent.pointerUp(cell, { pointerId: 2, pointerType: 'touch', clientX: 140 });

    expect(setPointerCaptureSpy).not.toHaveBeenCalled();
  });
});

describe('RequestsTab session labelling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getRequestMetrics).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      windows: [],
    });
    vi.mocked(getRecentRequestContent).mockResolvedValue({
      prompt: 'hello world',
      response: 'hi there',
      reasoning_content: null,
    });
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 1,
      groups: [],
      truncated: false,
    });
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [
        makeRequest({
          session_id: '7c6b5a49-3827-1605-f4e3-d2c1b0a99887',
          session_id_source: 'metadata.user_id',
        }),
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });
  });

  afterEach(cleanup);

  it("shows a row's session, shortened, with the full value and its source in the title", async () => {
    render(<RequestsTab />);

    const chip = await screen.findByTitle(/^session 7c6b5a49-3827-1605-f4e3-d2c1b0a99887/);
    // A UUID is too long for a dense row; the head is enough to see that a run
    // of rows belongs to one conversation.
    expect(chip).toHaveTextContent('7c6b5a49…');
    // Where it came from matters: a session read out of a client's composite
    // user id is inferred, not declared.
    expect(chip).toHaveAttribute('title', expect.stringContaining('from metadata.user_id'));
  });

  it('filters the list to one session when its chip is clicked, and clears again', async () => {
    render(<RequestsTab />);

    fireEvent.click(await screen.findByTitle(/^session 7c6b5a49/));

    await waitFor(() =>
      // sessionId is the 8th positional argument of listRecentRequests.
      expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[7]).toBe(
        '7c6b5a49-3827-1605-f4e3-d2c1b0a99887',
      ),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Clear session filter' }));

    await waitFor(() =>
      expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[7]).toBeUndefined(),
    );
  });

  it('scopes the performance summary to the selected session', async () => {
    // The panel sits directly above the table; leaving it unscoped would show
    // one conversation's rows beside every conversation's latency numbers.
    render(<RequestsTab />);

    fireEvent.click(await screen.findByTitle(/^session 7c6b5a49/));

    await waitFor(() =>
      expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
        expect.objectContaining({ sessionId: '7c6b5a49-3827-1605-f4e3-d2c1b0a99887' }),
      ),
    );
  });

  it('exports the selected session rather than every session', async () => {
    vi.mocked(exportRequests).mockResolvedValue(undefined);
    render(<RequestsTab />);

    fireEvent.click(await screen.findByTitle(/^session 7c6b5a49/));
    fireEvent.click(screen.getByRole('button', { name: 'Export JSONL' }));
    fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2026-09-01' } });
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));

    await waitFor(() =>
      expect(exportRequests).toHaveBeenCalledWith(
        expect.objectContaining({ sessionId: '7c6b5a49-3827-1605-f4e3-d2c1b0a99887' }),
      ),
    );
  });

  it('shows the full session and its source in the expanded row', async () => {
    render(<RequestsTab />);

    fireEvent.click(await screen.findByText('gpt-4o-mini'));

    expect(screen.getByText('Session:')).toBeInTheDocument();
    expect(screen.getByText('7c6b5a49-3827-1605-f4e3-d2c1b0a99887')).toBeInTheDocument();
    expect(screen.getByText('(from metadata.user_id)')).toBeInTheDocument();
  });

  it('renders a dash for a request that declared no session', async () => {
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [makeRequest()],
      total: 1,
      limit: 50,
      offset: 0,
    });
    render(<RequestsTab />);

    fireEvent.click(await screen.findByText('gpt-4o-mini'));

    expect(screen.getByText('Session:')).toBeInTheDocument();
    expect(screen.queryByTitle(/^session /)).not.toBeInTheDocument();
  });
});

describe('RequestsTab client disconnect classification', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getRequestMetrics).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      windows: [
        {
          key: '1h',
          label: 'Last 1 hour',
          window_minutes: 60,
          bucket_minutes: 5,
          total_requests: 100,
          success_requests: 80,
          error_requests: 3,
          client_disconnect_requests: 17,
          avg_latency_ms: 950,
          buckets: [
            {
              start_time: '2026-06-30T11:55:00.000Z',
              request_count: 100,
              success_count: 80,
              error_count: 3,
              client_disconnect_count: 17,
              avg_latency_ms: 950,
            },
          ],
        },
      ],
    });
    vi.mocked(getRecentRequestContent).mockResolvedValue({
      prompt: 'hello world',
      response: 'hi there',
      reasoning_content: null,
    });
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 1,
      groups: [],
      truncated: false,
    });
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests: [
        makeRequest({
          request_id: 'req_disconnect',
          status_code: 499,
          terminal_state: 'client_disconnect',
        }),
        // Same status, no cancellation marker: an upstream answered 499, which
        // is a real failure the gateway only relayed.
        makeRequest({ request_id: 'req_upstream_499', status_code: 499 }),
      ],
      total: 2,
      limit: 50,
      offset: 0,
    });
  });

  afterEach(cleanup);

  it('counts the hour’s client disconnects apart from its errors', async () => {
    // 17 abandoned streams and 3 real failures is a very different hour from
    // "20 errors", which is what the card said before the split.
    render(<RequestsTab />);

    const disconnects = await screen.findByTitle(/^Client disconnects: the caller hung up/);
    expect(disconnects).toHaveTextContent('17 disc');
    // The error count no longer carries them: the two are read side by side.
    const card = disconnects.closest('.rounded-xl');
    expect(card).toHaveTextContent('3 err');
    expect(card).toHaveTextContent('Last 1 hour');
  });

  it('filters the list by outcome', async () => {
    render(<RequestsTab />);
    // Two rows share this model, so match all of them.
    await screen.findAllByText('gpt-4o-mini');

    // outcome is the 5th positional argument of listRecentRequests.
    expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[4]).toBe('all');

    fireEvent.change(screen.getByLabelText('Filter by outcome'), {
      target: { value: 'client_disconnect' },
    });
    await waitFor(() =>
      expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[4]).toBe('client_disconnect'),
    );

    fireEvent.change(screen.getByLabelText('Filter by outcome'), {
      target: { value: 'errors_excluding_disconnects' },
    });
    await waitFor(() =>
      expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[4]).toBe(
        'errors_excluding_disconnects',
      ),
    );
  });

  it('carries the outcome filter into the JSONL export', async () => {
    render(<RequestsTab />);
    // Two rows share this model, so match all of them.
    await screen.findAllByText('gpt-4o-mini');

    fireEvent.change(screen.getByLabelText('Filter by outcome'), {
      target: { value: 'client_disconnect' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Export JSONL' }));
    fireEvent.change(screen.getByLabelText(/Start date/i) ?? screen.getByLabelText('Start date'), {
      target: { value: '2026-06-01' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));

    await waitFor(() =>
      expect(exportRequests).toHaveBeenCalledWith(
        expect.objectContaining({ outcome: 'client_disconnect' }),
      ),
    );
  });

  it('badges an abandoned stream apart from a failure', async () => {
    // A column of red 499s reads as an outage; the gateway did not fail, the
    // caller hung up.
    render(<RequestsTab />);

    const badge = await screen.findByTitle('Client disconnected before the stream completed');
    expect(badge).toHaveTextContent('499');
    expect(badge.className).toContain('amber');
    expect(badge.className).not.toContain('red');
  });

  it('leaves an upstream’s own 499 badged as the error it is', async () => {
    // Both failure handlers log whatever status the upstream exception carried,
    // so the status alone cannot say the caller hung up — only the gateway's
    // terminal_state can. Badging this amber would excuse a real failure.
    render(<RequestsTab />);

    await screen.findByTitle('Client disconnected before the stream completed');
    const badges = screen.getAllByText('499');
    expect(badges).toHaveLength(2);
    const unmarked = badges.filter((b) => !b.getAttribute('title'));
    expect(unmarked).toHaveLength(1);
    expect(unmarked[0].className).toContain('red');
    expect(unmarked[0].className).not.toContain('amber');
  });
});

describe('RequestsTab credential state', () => {
  function mockList(requests: AdminRecentRequestItem[]): void {
    vi.mocked(getRequestMetrics).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      windows: [],
    });
    vi.mocked(getRecentRequestsPerformance).mockResolvedValue({
      generated_at: '2026-06-30T12:00:00.000Z',
      days: 1,
      groups: [],
      truncated: false,
    });
    vi.mocked(listRecentRequests).mockResolvedValue({
      requests,
      total: requests.length,
      limit: 50,
      offset: 0,
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(cleanup);

  it('names the account behind a dead key, and labels the key', async () => {
    // What the auth-failure blocklist actually catches: a caller of the
    // deployment's own whose key was rotated away. The account is named from
    // credential_owner_id — it is not in user_id, because the gateway
    // identified this caller without authenticating them — and the label is
    // what stops the row reading as an ordinary request by that user.
    mockList([
      makeRequest({
        status_code: 429,
        error: 'ip_blocked',
        user_id: null,
        user_name: null,
        user_email: null,
        credential_state: 'revoked',
        credential_owner_id: 'user_1',
      }),
    ]);

    render(<RequestsTab />);

    expect(await screen.findByText('revoked key')).toBeInTheDocument();
    expect(screen.getAllByText('user_1').length).toBeGreaterThan(0);
  });

  it('does not offer the named account as a filter chip', async () => {
    // Filtering by a user means "this user's requests". These are not: the
    // row is a refusal of someone holding their key.
    mockList([
      makeRequest({
        status_code: 429,
        error: 'ip_blocked',
        user_id: null,
        user_name: null,
        user_email: null,
        credential_state: 'revoked',
        credential_owner_id: 'user_1',
      }),
    ]);

    render(<RequestsTab />);

    await screen.findByText('revoked key');
    expect(screen.queryByRole('button', { name: /user_1/ })).not.toBeInTheDocument();
  });

  it('reads a suspended owner as an account, not a key', async () => {
    mockList([
      makeRequest({
        status_code: 429,
        error: 'ip_blocked',
        user_id: null,
        user_name: null,
        user_email: null,
        credential_state: 'user_suspended',
        credential_owner_id: 'user_1',
      }),
    ]);

    render(<RequestsTab />);

    expect(await screen.findByText('suspended account')).toBeInTheDocument();
  });

  it('shows no label when the key presented was still live', async () => {
    // A live key refused for *where* it called from is collateral damage, not
    // a credential problem — nothing to tell the operator to go fix.
    mockList([makeRequest({ status_code: 429, error: 'ip_blocked', credential_state: 'active' })]);

    render(<RequestsTab />);

    await screen.findAllByText('Ada');
    expect(screen.queryByText('active key')).not.toBeInTheDocument();
  });
});
