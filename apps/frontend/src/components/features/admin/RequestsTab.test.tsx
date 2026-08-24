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
      days: 7,
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
      days: 7,
      userId: undefined,
      modelId: undefined,
      requestType: undefined,
      refresh: false,
    });

    // Changing the lookback re-scopes the summary alongside the list.
    fireEvent.change(screen.getByLabelText('Lookback window'), { target: { value: '30' } });
    await waitFor(() =>
      expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith({
        days: 30,
        userId: undefined,
        modelId: undefined,
        requestType: undefined,
        refresh: false,
      }),
    );

    // Refresh reloads the summary and asks the backend for fresh numbers.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(getRecentRequestsPerformance).toHaveBeenLastCalledWith(
        expect.objectContaining({ days: 30, refresh: true }),
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
