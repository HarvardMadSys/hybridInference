// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { RecentRequestItem } from '@/lib/api/user';
import { RecentRequests } from './RecentRequests';

vi.mock('@/lib/hooks', () => ({
  useRecentRequests: vi.fn(),
}));

afterEach(() => {
  cleanup();
});

function makeRequest(overrides: Partial<RecentRequestItem> = {}): RecentRequestItem {
  return {
    request_id: 'req_1234567890abcdefghijklmnop',
    model_id: 'claude-sonnet',
    provider: 'anthropic',
    timestamp: '2026-05-06T12:00:00.000Z',
    status_code: 200,
    latency_ms: 2200,
    ttft_ms: 700,
    stream: true,
    prompt_tokens: 1200,
    completion_tokens: 301,
    reasoning_tokens: 64,
    cache_read_tokens: 80,
    cache_write_tokens: 20,
    total_tokens: 1665,
    cost_usd: 0.0234,
    error: null,
    ...overrides,
  };
}

describe('RecentRequests', () => {
  it('shows a structured detail panel when a row is opened', async () => {
    const { useRecentRequests } = await import('@/lib/hooks');
    vi.mocked(useRecentRequests).mockReturnValue({
      data: {
        requests: [makeRequest()],
        total: 1,
        limit: 20,
        offset: 0,
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useRecentRequests>);

    render(<RecentRequests />);

    expect(screen.queryByText('Request Details')).not.toBeInTheDocument();

    fireEvent.click(screen.getAllByText('claude-sonnet')[0]);

    expect(screen.getByText('Request Details')).toBeInTheDocument();
    // Full model ID is surfaced in the detail panel (row cell + DetailStat value)
    // so touch users can read it even though the row tooltip is hover-only.
    expect(screen.getAllByText('claude-sonnet')).toHaveLength(2);
    expect(screen.getByText('req_1234567890abcdefghijklmnop')).toBeInTheDocument();
    expect(screen.getByText('anthropic')).toBeInTheDocument();
    expect(screen.getByText('Latency')).toBeInTheDocument();
    expect(screen.getByText('2.2s')).toBeInTheDocument();
    expect(screen.getByText('TTFT')).toBeInTheDocument();
    expect(screen.getByText('700ms')).toBeInTheDocument();
    expect(screen.getByText('Streaming')).toBeInTheDocument();
    expect(screen.getByText('Enabled')).toBeInTheDocument();
    expect(screen.getByText('Prompt / Output')).toBeInTheDocument();
    expect(screen.getByText('1.2k / 301')).toBeInTheDocument();

    fireEvent.click(screen.getAllByText('claude-sonnet')[0]);

    expect(screen.queryByText('Request Details')).not.toBeInTheDocument();
  });

  it('shows RouteWise decision metadata in the detail panel', async () => {
    const { useRecentRequests } = await import('@/lib/hooks');
    vi.mocked(useRecentRequests).mockReturnValue({
      data: {
        requests: [
          makeRequest({
            routewise: {
              selected_provider_type: 'on_demand',
              selected_provider: 'openai',
              selected_endpoint_id: 'openai:key-1',
              hedging_triggered: true,
              hedge_backup_provider: 'anthropic',
              backup_won: false,
            },
          }),
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useRecentRequests>);

    render(<RecentRequests />);

    fireEvent.click(screen.getAllByText('claude-sonnet')[0]);

    expect(screen.getByText('RouteWise')).toBeInTheDocument();
    expect(screen.getByText('on_demand: openai; hedge -> anthropic')).toBeInTheDocument();
  });

  it('shows cached tokens in the collapsed token summary', async () => {
    const { useRecentRequests } = await import('@/lib/hooks');
    vi.mocked(useRecentRequests).mockReturnValue({
      data: {
        requests: [makeRequest({ cache_read_tokens: 913, reasoning_tokens: null })],
        total: 1,
        limit: 20,
        offset: 0,
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useRecentRequests>);

    render(<RecentRequests />);

    expect(screen.getByTitle('913 cached tokens')).toHaveTextContent('C 913');
  });

  it('keeps the horizontal scroll wrapper configured for smooth mobile scrolling', async () => {
    const { useRecentRequests } = await import('@/lib/hooks');
    vi.mocked(useRecentRequests).mockReturnValue({
      data: {
        requests: [makeRequest()],
        total: 1,
        limit: 20,
        offset: 0,
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useRecentRequests>);

    const { container } = render(<RecentRequests />);

    // The table scrolls horizontally on mobile; overscroll-x-contain stops the
    // swipe from being hijacked by the browser's back/forward navigation gesture
    // (the "each swipe only moves a bit" symptom). Guard against silent removal.
    const scrollWrapper = container.querySelector('table')?.parentElement;
    expect(scrollWrapper).toHaveClass('overflow-x-auto', 'overscroll-x-contain');
  });

  it('rounds sub-second latencies consistently across metrics', async () => {
    const { useRecentRequests } = await import('@/lib/hooks');
    vi.mocked(useRecentRequests).mockReturnValue({
      data: {
        requests: [
          makeRequest({
            latency_ms: 456.7,
            ttft_ms: 123.4,
          }),
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
      isLoading: false,
      error: null,
    } as ReturnType<typeof useRecentRequests>);

    render(<RecentRequests />);

    fireEvent.click(screen.getAllByText('claude-sonnet')[0]);

    expect(screen.getByText('457ms')).toBeInTheDocument();
    expect(screen.getByText('123ms')).toBeInTheDocument();
  });
});
