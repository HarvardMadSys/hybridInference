// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { RecentRequestItem } from '@/lib/api/user';
import { RecentRequests } from './RecentRequests';

vi.mock('@/lib/hooks', () => ({
  useRecentRequests: vi.fn(),
}));

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

    fireEvent.click(screen.getByText('claude-sonnet'));

    expect(screen.getByText('Request Details')).toBeInTheDocument();
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

    fireEvent.click(screen.getByText('claude-sonnet'));

    expect(screen.queryByText('Request Details')).not.toBeInTheDocument();
  });
});
